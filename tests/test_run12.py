"""Run 12 of the desk: why an alert did (or didn't) reach a phone, each phone's push log, the
"is it working?" check, and halt resumptions."""

from __future__ import annotations

import time
from types import SimpleNamespace

import aiohttp
from aiohttp.test_utils import TestServer

from news247.desk import Desk
from news247.dossier import halts_board
from news247.models import Analysis, Severity
from news247.pushcheck import phone_health, push_log, why_push
from news247.sources.halts import TradingHaltsSource
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import item, phones

# --------------------------------------------------------------------------- the reasons


def test_block_names_the_setting_and_the_fix():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "watch_add", {"symbol": "ACME"})
    assert d.block(DEV, news()) is None and d.block_reason(DEV2, news()) is None  # no profile: everything
    d.apply(DEV, "mute", {"symbol": "ACME", "hours": 2})
    b = d.block(DEV, news())
    assert b["code"] == "muted" and b["text"] == "you muted ACME"
    assert b["fix"] == {"label": "Unmute ACME", "action": "unmute", "body": {"symbol": "ACME"}}
    d.apply(DEV, b["fix"]["action"], b["fix"]["body"])  # the fix works as given
    d.apply(DEV, "neg_add", {"keyword": "contract"})
    assert d.block_reason(DEV, news()) == "it has your never-push word “contract”"
    d.apply(DEV, "neg_remove", {"keyword": "contract"})
    d.apply(DEV, "prefs", {"min_sev": "CRITICAL"})
    b = d.block(DEV, news())
    assert b["text"] == "it's HIGH, under your CRITICAL floor"
    d.apply(DEV, b["fix"]["action"], b["fix"]["body"])
    d.apply(DEV, "prefs", {"cats": {"news": False}})
    assert d.block(DEV, news())["fix"]["body"] == {"cats": {"news": True}}
    assert (
        d.allows(DEV, news()) is False and d.allows(DEV, news(sev=Severity.CRITICAL)) is False
    )  # category off
    d.apply(DEV, "prefs", {"cats": {"news": True}, "mute_source": "globenewswire"})
    assert d.block(DEV, news())["text"] == "you muted its source (globenewswire)"
    assert d.allows(DEV, news(sev=Severity.CRITICAL)) is True  # CRITICAL ignores a source mute


# --------------------------------------------------------------------------- the push log


async def test_push_log_records_sent_and_kept_back(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (410, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        eng.desk.apply(DEV2, "prefs", {"cats": {"news": True}})
        a = news()
        eng.storage.add_alert(a)
        await eng.webpush.send(a)
        assert [(r["id"], r["status"]) for r in push_log(eng, DEV)["rows"]] == [(a.id, "sent")]
        gone = push_log(eng, DEV2)["rows"][0]
        assert gone["status"] == "gone" and "expired" in gone["detail"]
        assert len(eng.webpush.subs) == 1  # the dead subscription was dropped

        eng.desk.apply(DEV, "mute", {"symbol": "ACME", "hours": 1})
        b = news("Acme raises guidance")
        eng.storage.add_alert(b)
        await eng.webpush.send(b)
        top = push_log(eng, DEV)["rows"][0]
        assert (top["id"], top["status"], top["detail"]) == (b.id, "filtered", "you muted ACME")
        assert push_log(eng, DEV)["today"] == {"filtered": 1, "sent": 1}

        # personal pushes (price alerts, digests...) are logged too
        await eng._to_device(DEV, {"id": "pa-1", "title": "🔔 ACME is above $105.00"})
        assert push_log(eng, DEV)["rows"][0]["title"] == "🔔 ACME is above $105.00"

        # and the log survives a restart
        assert eng.webpush.save_log() is True and eng.webpush.save_log() is False
        eng.webpush.log = {}
        eng.webpush.load_log()
        assert len(push_log(eng, DEV)["rows"]) == 3
    finally:
        await eng.http.close()


async def test_watcher_pushes_are_logged_with_their_reason(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        for d in (DEV, DEV2):
            eng.desk.apply(d, "watch_add", {"symbol": "ACME"})
        eng.desk.apply(DEV2, "prefs", {"digest": 60})
        m = news("Acme files 8-K", sev=Severity.MEDIUM)
        await eng._push_watchers(m)
        assert push_log(eng, DEV)["rows"][0]["status"] == "sent"
        assert push_log(eng, DEV2)["rows"][0]["status"] == "held"
        eng.storage.add_alert(m)
        w = why_push(eng, DEV2, m.id)
        assert w["verdict"].startswith("It was kept for your digest at ") and w["fix"]["body"] == {
            "digest": 0
        }
        eng.desk.apply(DEV2, w["fix"]["action"], w["fix"]["body"])
        assert not eng.desk.get(DEV2)["prefs"].get("digest")
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- why did / didn't I get it


async def test_why_push_explains_each_case(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        a = news()
        eng.storage.add_alert(a)
        await eng.webpush.send(a)
        w = why_push(eng, DEV, a.id)
        assert w["got"] is True and w["verdict"].startswith("It reached this phone at ") and w["fix"] is None
        assert w["settings_now"] is False and all(s["ok"] for s in w["steps"])

        # a MEDIUM alert on a stock this phone doesn't watch never pushed: the fix is to watch it
        m = news("Acme files 8-K", sev=Severity.MEDIUM)
        eng.storage.add_alert(m)
        w = why_push(eng, DEV, m.id)
        assert w["got"] is False and "MEDIUM alerts on stocks you watch (ACME isn't one)" in w["verdict"]
        assert w["fix"] == {"label": "Watch ACME", "action": "watch_add", "body": {"symbol": "ACME"}}
        assert w["settings_now"] is True
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        w = why_push(eng, DEV, m.id)
        assert w["steps"][1] == {"ok": True, "text": "It's MEDIUM, but it's about ACME, which you watch"}
        assert w["got"] is None and "isn't in this phone's push log" in w["verdict"]

        # kept back by a setting, logged with the reason and the fix
        eng.desk.apply(DEV, "prefs", {"min_sev": "CRITICAL"})
        h = news("Acme raises guidance")
        eng.storage.add_alert(h)
        await eng.webpush.send(h)
        w = why_push(eng, DEV, h.id)
        assert w["got"] is False and "under your CRITICAL floor" in w["verdict"]
        assert w["fix"]["body"] == {"min_sev": None}

        # a phone that never turned alerts on
        assert why_push(eng, "dev_nopush00001", a.id)["fix"]["app"] == "push-on"
        assert why_push(eng, DEV, "nope") is None
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- is it working?


def test_phone_health(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=1.0)
    eng.calendar = SimpleNamespace(market_status=lambda t=None: {"phase": "open"})
    eng.sources = [
        SimpleNamespace(
            enabled=True, describe=lambda: {"name": "sec", "status": "ok", "last_item_at": now - 30}
        ),
        SimpleNamespace(enabled=True, describe=lambda: {"name": "rss", "status": "error", "last_item_at": 0}),
        SimpleNamespace(enabled=True, describe=lambda: {"name": "halts", "status": "ok", "last_item_at": 0}),
    ]
    h = phone_health(eng, DEV, now)
    by = {c["key"]: c for c in h["checks"]}
    assert (
        by["feeds"]["ok"] is True
        and "2 of 3" in by["feeds"]["text"]
        and "Not reading: rss" in by["feeds"]["text"]
    )
    assert by["prices"]["ok"] is True and by["server"]["ok"] is True
    assert by["phone"]["ok"] is False and by["phone"]["fix"]["app"] == "push-on"
    assert h["ok"] is False and h["summary"].startswith("This phone:")
    eng.desk.apply(DEV, "prefs", {"quiet": {"start": "00:00", "end": "23:59"}, "digest": 15})
    eng.desk.apply(DEV, "mute", {"symbol": "ACME", "hours": 2})
    s = {c["key"]: c for c in phone_health(eng, DEV, now)["checks"]}["settings"]
    assert "1 stock muted" in s["text"] and "digest mode" in s["text"]


# --------------------------------------------------------------------------- halt resumptions


def _entry(**kw):
    e = {"ndaq_issuesymbol": "ACME", "ndaq_issuename": "Acme Corp", "ndaq_reasoncode": "T1",
         "ndaq_market": "NASDAQ", "ndaq_haltdate": "10/14/2026", "ndaq_halttime": "10:02:11.123"}  # fmt: skip
    e.update(kw)
    return e


def test_halt_feed_emits_a_resumption_once_a_time_is_posted():
    src = TradingHaltsSource({"name": "nasdaq-halts"}, SimpleNamespace(http=None))
    halt = src.entry_to_item(_entry())
    assert src.resume_item(_entry(), halt) is None
    e = _entry(
        ndaq_resumptiondate="10/14/2026",
        ndaq_resumptionquotetime="10:30:00",
        ndaq_resumptiontradetime="10:35:00",
    )
    halt = src.entry_to_item(e)
    r = src.resume_item(e, halt)
    assert r.title == "▶ ACME resumes trading at 10:35:00 ET" and r.tickers == ["ACME"]
    assert (
        r.summary
        == "Halted 10:02:11 ET for News Pending [T1]. Quotes from 10:30:00 ET, trades from 10:35:00 ET."
    )
    assert r.uid == "resume:ACME:10/14/2026:10:02:11.123" and r.extra["halt_resume"] == "T1"
    assert "halt_code" not in r.extra  # never counted as a second halt
    mwc = _entry(ndaq_reasoncode="MWC1", ndaq_resumptiontradetime="10:15:00")
    assert src.resume_item(mwc, src.entry_to_item(mwc)) is None  # market-wide: no stock to resume


async def test_resumptions_reach_watchers_and_fill_the_board(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        halt = item("TRADING HALT ACME (Acme) — News Pending [T1]", source="nasdaq-halts",
                    summary="Market: NASDAQ. Halted 10/14/2026 10:02:11 ET.", halt_code="T1")  # fmt: skip
        eng.storage.add_item(halt, Analysis(severity=Severity.MEDIUM, score=40, tickers=["ACME"]))
        res = item("▶ ACME resumes trading at 10:35:00 ET", source="nasdaq-halts",
                   summary="Halted 10:02:11 ET for News Pending [T1].", halt_resume="T1")  # fmt: skip
        an = Analysis(severity=Severity.MEDIUM, score=60, tickers=["ACME"])
        eng.storage.add_item(res, an)
        assert await eng._halt_watch(res, an) == 1  # a resumption goes out even when it scored high
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "▶ ACME resumes trading at 10:35:00 ET" and msg["tag"] == "halt-ACME"
        rows = halts_board(eng)["rows"]
        assert len(rows) == 1 and rows[0]["resume"] == "10:35:00"  # the resumption fills the halt's row
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- over the API


async def test_run12_over_the_api(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        phones(eng, server)
        a = news()
        eng.storage.add_alert(a)
        await eng.webpush.send(a)
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url(f"/api/why-push/{a.id}"), headers=h) as r:
                assert (await r.json())["got"] is True
            async with s.get(srv.make_url("/api/why-push/nope"), headers=h) as r:
                assert r.status == 404
            async with s.get(srv.make_url("/api/push-log"), headers=h) as r:
                assert (await r.json())["rows"][0]["id"] == a.id
            async with s.get(srv.make_url("/api/phone-health"), headers=h) as r:
                body = await r.json()
                assert {c["key"] for c in body["checks"]} >= {"feeds", "server", "phone", "settings"}
            async with s.get(srv.make_url("/api/push-log"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()


# --------------------------------------------------------------------------- the shared-list link


async def test_shared_list_preview_and_import(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
        code = eng.desk.apply(DEV, "share_list", {"name": "Semis"})["share"]["code"]
        eng.desk.apply(DEV2, "watch_add", {"symbol": "ACME"})
        h = {"Authorization": "Bearer tok", "X-Device": DEV2}
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url(f"/api/list/{code[:4]}-{code[4:].lower()}"), headers=h) as r:
                body = await r.json()
                assert body["name"] == "Semis" and body["new"] == 1
                assert [(x["symbol"], x["mine"]) for x in body["rows"]] == [("ACME", True), ("BETA", False)]
                assert body["rows"][0]["name"].startswith("Acme")
            async with s.get(srv.make_url("/api/list/NOPE1234"), headers=h) as r:
                assert r.status == 404
        eng.desk.apply(DEV2, "import_list", {"code": code})
        v = eng.desk.view(DEV2, lambda s: None)
        assert v["imported"] == 1 and [w["symbol"] for w in v["watchlist"]] == ["ACME", "BETA"]
    finally:
        await srv.close()
        await eng.http.close()
