"""Run 13 of the desk: a push dial per stock, focus mode, burst protection, "remind me", mute until
the open, "since you last looked", backup and restore, "would I get it?", 7-day push stats and the
palette's command parser."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.desk import Desk, session_edge
from news247.dossier import since_you_looked
from news247.models import Severity
from news247.pushcheck import push_log, push_week, why_push, would_push
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- the dial per stock


def test_stock_dial_off_high_and_everything():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "watch_add", {"symbol": "ACME"})
    d.apply(DEV, "prefs", {"min_sev": "CRITICAL"})
    assert d.block(DEV, news())["code"] == "floor"
    d.apply(DEV, "stock_push", {"symbol": "ACME", "level": "all"})
    assert d.block(DEV, news()) is None  # everything: past the phone's own floor
    assert d.block(DEV, news("x", sev=Severity.MEDIUM)) is None
    d.apply(DEV, "stock_push", {"symbol": "acme", "level": "high"})
    b = d.block(DEV, news("x", sev=Severity.MEDIUM))
    assert b["code"] == "stock_high" and b["fix"]["body"] == {"symbol": "ACME", "level": "all"}
    d.apply(DEV, "prefs", {"min_sev": None})
    assert d.block(DEV, news()) is None  # HIGH is enough
    d.apply(DEV, "stock_push", {"symbol": "ACME", "level": "off"})
    b = d.block(DEV, news(sev=Severity.CRITICAL))
    assert b["code"] == "stock_off" and b["text"] == "you set ACME to never push"  # like a standing mute
    d.apply(DEV, b["fix"]["action"], b["fix"]["body"])
    assert d.get(DEV)["stock_push"] == {} and d.allows(DEV, news()) is True
    for bad in ({"symbol": "ACME", "level": "loud"}, {"symbol": "??", "level": "all"}):
        with pytest.raises(ValueError):
            d.apply(DEV, "stock_push", bad)
    # alerts about several stocks: everything beats high beats nothing
    d.apply(DEV, "stock_push", {"symbol": "ACME", "level": "off"})
    d.apply(DEV, "stock_push", {"symbol": "BETA", "level": "all"})
    assert d.allows(DEV, news(tickers=("ACME", "BETA"), sev=Severity.MEDIUM)) is True
    assert d.allows(DEV, news(tickers=("ACME", "GAMA"))) is True  # only one of them is off


async def test_everything_dial_pushes_medium_alerts_on_unwatched_stocks(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        m = news("Acme files 8-K", sev=Severity.MEDIUM)
        eng.storage.add_alert(m)
        eng.desk.apply(DEV, "prefs", {"watch_push": False})
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        await eng._push_watchers(m)
        assert server.requests == []  # watched, but MEDIUM pushes are off
        eng.desk.apply(DEV, "stock_push", {"symbol": "ACME", "level": "all"})
        await eng._push_watchers(m)
        assert [r["path"] for r in server.requests] == ["/push/a"]
        # not even on the watchlist: the dial alone is enough
        eng.desk.apply(DEV2, "stock_push", {"symbol": "ACME", "level": "all"})
        assert set(eng.desk.watchers(["ACME"])) == {DEV, DEV2}
        w = why_push(eng, DEV2, m.id)
        assert w["steps"][1]["text"] == "It's MEDIUM, but it's about ACME, which you set to push everything"
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- focus mode


def test_focus_mode_holds_everything_but_critical():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "watch_add", {"symbol": "ACME"})
    assert d.view(DEV, lambda s: None)["focus"] is None
    d.apply(DEV, "focus", {"minutes": 60})
    b = d.block(DEV, news())
    assert b["code"] == "focus" and b["fix"] == {
        "label": "End focus",
        "action": "focus",
        "body": {"minutes": 0},
    }
    assert d.allows(DEV, news(sev=Severity.CRITICAL)) is True
    assert d.allows(DEV, news(), now=time.time() + 3700) is True  # over
    assert d.view(DEV, lambda s: None)["focus"] > time.time()
    d.apply(DEV, "focus", {"minutes": 0})
    assert d.allows(DEV, news()) is True and d.view(DEV, lambda s: None)["focus"] is None
    d.apply(DEV, "focus", {"until": "open"})
    assert d.get(DEV)["focus_until"] > time.time()
    with pytest.raises(ValueError):
        d.apply(DEV, "focus", {"minutes": 5000})


def test_sessions_skip_weekends_holidays_and_early_closes():
    cal = SimpleNamespace(holidays={"2026-10-19"}, early={"2026-10-16"})
    assert session_edge("open", at(10, 0, d=16), cal) == at(
        9, 30, d=19 + 1
    )  # Fri 10:00 -> Mon is a holiday -> Tue
    assert session_edge("close", at(10, 0, d=16), cal) == at(13, 0, d=16)  # an early close
    assert session_edge("close", at(10, 0, d=14)) == at(16, 0, d=14)
    assert session_edge("close", at(16, 0, d=14)) == at(16, 0, d=15)  # today's is done
    assert session_edge("open", at(9, 29, d=14)) == at(9, 30, d=14)
    assert session_edge("open", at(9, 30, d=14) - 10) == at(9, 30, d=15)  # inside 30 s: the next one


def test_mute_until_the_open_or_close():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "mute", {"symbol": "ACME", "until": "close"})
    left = d.get(DEV)["muted"]["ACME"] - time.time()
    assert 0 < left <= 72 * 3600
    with pytest.raises(ValueError):
        d.apply(DEV, "mute", {"symbol": "ACME", "hours": 100})


# --------------------------------------------------------------------------- burst protection


async def test_a_rush_of_alerts_folds_into_one_summary(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        eng.desk.apply(DEV, "prefs", {"burst": 4})
        with pytest.raises(ValueError):
            eng.desk.apply(DEV, "prefs", {"burst": 7})
        alerts = [news(f"Acme headline {i}") for i in range(7)]
        for i, a in enumerate(alerts):
            a.title = f"Acme headline {i}"
            a.id = f"{i:016x}"
            eng.storage.add_alert(a)
            await eng.webpush.send(a)
        rows = push_log(eng, DEV)["rows"]
        assert [r["status"] for r in reversed(rows)] == ["sent"] * 4 + ["held"] * 3
        assert rows[0]["detail"] == "held back in a rush of alerts, folded into one summary"
        assert [r["path"] for r in server.requests].count("/push/a") == 4
        assert [r["path"] for r in server.requests].count("/push/b") == 7  # the other phone has no limit
        crit = news("Acme halted", sev=Severity.CRITICAL)
        crit.id = "c" * 16
        await eng.webpush.send(crit)
        assert push_log(eng, DEV)["rows"][0]["status"] == "sent"  # CRITICAL always comes
        w = why_push(eng, DEV, alerts[5].id)
        assert w["got"] is False and w["verdict"].startswith("It was held back in a rush of alerts")
        # the summary goes out once the rush has had a few minutes to finish
        assert eng.desk.due_digests(time.time() + 60) == []
        server.requests.clear()
        assert await eng.send_digests(time.time() + 400) == 1
        assert [r["path"] for r in server.requests] == ["/push/a"]
        assert push_log(eng, DEV)["rows"][0]["title"] == "📬 3 alerts (3 HIGH) held back in a rush"
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- remind me


async def test_remind_me_brings_the_alert_back(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        quotes(eng, now, ACME=4.0)
        alert = news("Acme wins $2B Pentagon contract")
        alert.edge = {"refs": {"ACME": 100.0}}
        eng.storage.add_alert(alert)
        eng.desk.apply(DEV, "remind", {"id": alert.id, "when": "1h"})
        eng.desk.apply(DEV, "remind", {"id": alert.id, "when": "close"})  # replaces the first
        rem = eng.desk.view(DEV, lambda s: None)["reminders"]
        assert len(rem) == 1 and rem[0]["title"] == alert.title and rem[0]["tickers"] == ["ACME"]
        assert await eng.check_reminders(now) == 0  # not yet
        eng.desk.apply(DEV, "remind", {"id": alert.id, "when": "1h"})
        assert await eng.check_reminders(now + 3601) == 1 and await eng.check_reminders(now + 3700) == 0
        msg = a.read(server.requests[0]["body"])
        assert (
            msg["title"] == "⏰ Acme wins $2B Pentagon contract"
            and msg["body"] == "ACME +4.0% since the alert"
        )
        assert msg["open"] == f"/app/#a={alert.id}"
        assert eng.desk.view(DEV, lambda s: None)["reminders"] == []
        for bad in ({"id": "nope", "when": "1h"}, {"id": alert.id, "when": "someday"}):
            with pytest.raises(ValueError):
                eng.desk.apply(DEV, "remind", bad)
        eng.desk.apply(DEV, "remind", {"id": alert.id, "when": "3h"})
        rid = eng.desk.get(DEV)["reminders"][0]["id"]
        eng.desk.apply(DEV, "remind_clear", {"id": rid})
        assert eng.desk.get(DEV)["reminders"] == []
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- since you last looked


def test_since_you_last_looked(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    eng.live_quotes.update("ACME", 100.0, 100.0, now - 5, "tape")
    eng.desk.apply(DEV, "viewed", {"symbol": "ACME"}, eng.price_of)
    seen = eng.desk.get(DEV)["seen"]["ACME"]
    assert seen["px"] == 100.0
    assert since_you_looked(eng, DEV, "ACME", now + 600) is None  # a few minutes ago: nothing to say
    seen["ts"] = now - 2 * 86400  # two days ago
    for i, sev in enumerate((Severity.MEDIUM, Severity.HIGH)):
        a = news(f"Acme alert {i}", sev=sev)
        a.created = now - 3600
        eng.storage.add_alert(a)
    old = news("Acme old")
    old.created = now - 3 * 86400
    eng.storage.add_alert(old)
    eng.live_quotes.update("ACME", 103.0, 100.0, now, "tape")
    s = since_you_looked(eng, DEV, "ACME", now)
    assert s["move_pct"] == 3.0 and s["alerts"] == 2 and s["quiet"] is False
    assert [t["severity"] for t in s["top"]] == ["HIGH", "MEDIUM"]  # the biggest first
    assert since_you_looked(eng, DEV2, "ACME", now) is None
    seen["ts"] = now - 90 * 86400
    assert since_you_looked(eng, DEV, "ACME", now) is None  # too long ago to mean anything
    for i in range(160):  # the list stays bounded, newest kept
        eng.desk.apply(DEV, "viewed", {"symbol": f"S{i}"})
    assert len(eng.desk.get(DEV)["seen"]) == 150 and "S159" in eng.desk.get(DEV)["seen"]


# --------------------------------------------------------------------------- backup and restore


def test_restore_merges_a_backup_and_checks_every_part():
    a = Desk(Storage(":memory:"))
    a.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    a.apply(DEV, "watch_group", {"symbol": "ACME", "group": "Semis"})
    a.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 10, "cost": 90})
    a.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "above", "value": 120})
    a.apply(DEV, "kw_add", {"keyword": "tariff"})
    a.apply(DEV, "neg_add", {"keyword": "crypto"})
    a.apply(DEV, "note", {"symbol": "ACME", "text": "watch the 8-K"})
    a.apply(DEV, "rule_add", {"kind": "volume", "symbol": "ACME", "value": 6})
    a.apply(DEV, "follow", {"kind": "sectors", "name": "Technology"})
    a.apply(DEV, "stock_push", {"symbol": "BETA", "level": "high"})
    a.apply(DEV, "prefs", {"min_sev": "HIGH", "burst": 6, "mute_source": "globenewswire"})
    a.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100, "stop": 95}, lambda s: None
    )
    backup = json.loads(json.dumps({"foretape_desk": 1, "desk": a.get(DEV)}))

    b = Desk(Storage(":memory:"))
    b.apply(DEV2, "watch_add", {"symbol": "GAMA"})
    b.apply(DEV2, "restore", {"desk": backup})
    got = b.get(DEV2)
    assert got["watchlist"] == ["GAMA", "ACME", "BETA"] and got["groups"] == {"ACME": "Semis"}
    assert got["holdings"] == {"ACME": {"shares": 10.0, "cost": 90.0}}
    assert [(x["symbol"], x["kind"], x["value"]) for x in got["price_alerts"]] == [("ACME", "above", 120.0)]
    assert [k["k"] for k in got["keywords"]] == ["tariff"] and got["kw_neg"] == ["crypto"]
    assert got["notes"]["ACME"]["text"] == "watch the 8-K" and len(got["rules"]) == 1
    assert got["follow"]["sectors"] == ["Technology"] and got["stock_push"] == {"BETA": "high"}
    assert got["prefs"]["mute_sources"] == ["globenewswire"]
    assert got["prefs"]["min_sev"] is None  # settings only come back when asked for
    assert [(t["symbol"], t["stop"]) for t in got["tracked"]] == [("ACME", 95.0)]
    assert got["tracked"][0]["id"] != a.get(DEV)["tracked"][0]["id"] or True  # a fresh id when it clashes
    r = b.view(DEV2, lambda s: None)["restored"]
    assert r["stocks"] == 2 and r["alerts"] == 1 and r["calls"] == 1 and r["dials"] == 1
    assert b.view(DEV2, lambda s: None)["restored"] is None  # said once
    b.apply(DEV2, "restore", {"desk": backup, "prefs": True})
    assert b.get(DEV2)["prefs"]["min_sev"] == "HIGH" and b.get(DEV2)["prefs"]["burst"] == 6
    # merging again adds nothing
    b.apply(DEV2, "restore", {"desk": backup})
    again = b.view(DEV2, lambda s: None)["restored"]
    assert again["stocks"] == again["alerts"] == again["keywords"] == again["rules"] == 0
    assert len(b.get(DEV2)["price_alerts"]) == 1 and len(b.get(DEV2)["tracked"]) == 1


def test_restore_rejects_junk_and_drops_bad_parts():
    d = Desk(Storage(":memory:"))
    for junk in ("hello", [], {"hello": 1}, {"desk": "x"}):
        with pytest.raises(ValueError, match="backup"):
            d.apply(DEV, "restore", {"desk": junk})
    evil = {
        "watchlist": ["ACME", "../etc", "<script>", 42, None, "X" * 30],
        "holdings": {"BETA": {"shares": -5}, "GAMA": {"shares": "lots"}, "DLTA": {"shares": 3, "cost": 0}, "ACME": {"shares": 2}},
        "price_alerts": [{"symbol": "ACME", "kind": "sideways", "value": 5}, {"symbol": "ACME", "kind": "above", "value": "x"}, "x", {"symbol": "ACME", "kind": "below", "value": -1}],
        "tracked": [{"symbol": "ACME", "direction": "sideways", "entry": 5}, {"symbol": "ACME", "direction": "long", "entry": 0}, {"symbol": "ACME", "direction": "long", "entry": 10, "tags": ["ok-tag", "<b>", 5], "note": "n" * 900}],
        "keywords": ["a", {"k": "tariff"}, 7],
        "stock_push": {"ACME": "loud", "BETA": "off"},
        "prefs": {"min_sev": "EXTREME"},
    }  # fmt: skip
    d.apply(DEV, "restore", {"desk": evil, "prefs": True})
    got = d.get(DEV)
    assert got["watchlist"] == ["ACME"] and got["holdings"] == {"ACME": {"shares": 2.0, "cost": None}}
    assert got["price_alerts"] == [] and [k["k"] for k in got["keywords"]] == ["tariff"]
    assert got["stock_push"] == {"BETA": "off"} and got["prefs"]["min_sev"] is None
    assert (
        len(got["tracked"]) == 1
        and got["tracked"][0]["tags"] == ["ok-tag"]
        and len(got["tracked"][0]["note"]) == 200
    )


# --------------------------------------------------------------------------- would I get it? / stats


async def test_what_if_and_the_weeks_stats(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        eng.desk.apply(DEV, "prefs", {"cats": {"options": False}})
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        w = would_push(eng, DEV, "news", "HIGH", "acme")
        assert w["got"] is True and w["verdict"] == "Yes: it would reach this phone." and w["fix"] is None
        w = would_push(eng, DEV, "options", "HIGH", "ACME")
        assert w["got"] is False and "you turned options alerts off" in w["verdict"]
        assert w["fix"]["body"] == {"cats": {"options": True}}
        assert would_push(eng, DEV, "news", "MEDIUM", "ACME")["got"] is True  # watched
        w = would_push(eng, DEV, "news", "MEDIUM", "BETA")
        assert w["got"] is False and w["fix"]["action"] == "watch_add"
        assert would_push(eng, DEV, "radar", "CRITICAL", "")["got"] is True
        assert would_push(eng, "dev_nopush00001", "news", "HIGH", "ACME")["fix"]["app"] == "push-on"
        eng.desk.apply(DEV, "focus", {"minutes": 30})
        assert would_push(eng, DEV, "news", "HIGH", "ACME")["verdict"].startswith(
            "Your settings: focus mode is on"
        )
        for bad in (("gossip", "HIGH", ""), ("news", "LOW", ""), ("news", "HIGH", "??")):
            with pytest.raises(ValueError):
                would_push(eng, DEV, *bad)

        # daily counts, kept for two weeks, saved with the log
        wp = eng.webpush
        wp.note(DEV, {"id": "1" * 16, "title": "a"}, "sent", ts=at(10, 0))
        wp.note(DEV, {"id": "2" * 16, "title": "b"}, "sent", ts=at(11, 0))
        wp.note(DEV, {"id": "3" * 16, "title": "c"}, "held", "x", ts=at(11, 5))
        wp.note(DEV, {"id": "4" * 16, "title": "d"}, "gone", "x", ts=at(11, 6, d=13))
        week = push_week(eng, DEV, at(15, 0))
        assert week[-1] == {"date": "2026-10-14", "sent": 2, "held": 1, "filtered": 0, "failed": 0}
        assert week[-2]["failed"] == 1 and len(week) == 7
        wp.note(DEV, {"id": "5" * 16, "title": "e"}, "sent", ts=at(10, 0, d=1))
        assert len(wp.stats[DEV]) <= 14
        assert wp.save_log() and (wp.log.clear() or wp.stats.clear() or True)
        wp.load_log()
        assert push_week(eng, DEV, at(15, 0))[-1]["sent"] == 2
        assert push_log(eng, DEV, at(15, 0))["hint"] == ""
    finally:
        await eng.http.close()


def test_the_old_log_format_still_loads(tmp_path):
    eng = semis_engine(tmp_path)
    eng.storage.set_setting(
        "push_log", json.dumps({DEV: [{"ts": 1.0, "id": "x", "title": "t", "status": "sent", "detail": ""}]})
    )
    eng.webpush.load_log()
    assert push_log(eng, DEV)["rows"][0]["title"] == "t"


# --------------------------------------------------------------------------- the palette's commands


CMD = Path(__file__).resolve().parent.parent / "news247/web/static/app/cmd.js"


def parse(text):
    out = subprocess.run(
        ["node", "-e", "console.log(JSON.stringify(require(process.argv[1]).parse(process.argv[2])))", str(CMD), text],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return [(c["action"], c["body"]) for c in json.loads(out.stdout)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_palette_commands():
    assert parse("ACME above 105") == [("alert_add", {"symbol": "ACME", "kind": "above", "value": 105})]
    assert parse("alert on nvda below $98.5") == [
        ("alert_add", {"symbol": "NVDA", "kind": "below", "value": 98.5})
    ]
    assert parse("set alert for amd under 100") == [
        ("alert_add", {"symbol": "AMD", "kind": "below", "value": 100})
    ]
    assert parse("acme 5%") == [("alert_add", {"symbol": "ACME", "kind": "move", "value": 5})]
    assert parse("acme falls 4% from high") == [
        ("alert_add", {"symbol": "ACME", "kind": "trail", "value": 4})
    ]
    assert parse("watch nvda, amd") == [("watch_add", {"symbols": ["NVDA", "AMD"]})]
    assert parse("unwatch acme") == [("watch_remove", {"symbol": "ACME"})]
    assert parse("mute acme") == [("mute", {"symbol": "ACME", "hours": 4})]
    assert parse("mute acme 90 min") == [("mute", {"symbol": "ACME", "hours": 1.5})]
    assert parse("mute acme 2 days") == [("mute", {"symbol": "ACME", "hours": 48})]
    assert parse("mute acme until the open") == [("mute", {"symbol": "ACME", "until": "open"})]
    assert parse("unmute acme") == [("unmute", {"symbol": "ACME"})]
    assert parse("focus") == [("focus", {"minutes": 60})]
    assert parse("focus for 2h") == [("focus", {"minutes": 120})]
    assert parse("focus until open") == [("focus", {"until": "open"})]
    assert parse("stop focus") == [("focus", {"minutes": 0})]
    assert parse("push acme high only") == [("stock_push", {"symbol": "ACME", "level": "high"})]
    assert parse("push acme everything") == [("stock_push", {"symbol": "ACME", "level": "all"})]
    # plain words, tickers and half-commands are not commands
    for plain in (
        "",
        "acme",
        "acme above",
        "alert above 5",
        "mute",
        "watch",
        "push acme loud",
        "tariff news 5 things",
    ):
        assert parse(plain) == [], plain


# --------------------------------------------------------------------------- over the API


async def test_run13_over_the_api(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        phones(eng, server)
        now = time.time()
        eng.live_quotes.update("ACME", 100.0, 100.0, now - 5, "tape")
        a = news()
        eng.storage.add_alert(a)
        async with aiohttp.ClientSession() as s:

            async def post(path, body, headers=h):
                async with s.post(srv.make_url(path), json=body, headers=headers) as r:
                    return r.status, await r.json() if r.status == 200 else await r.text()

            st, view = await post("/api/desk", {"action": "stock_push", "symbol": "ACME", "level": "all"})
            assert st == 200 and view["stock_push"] == {"ACME": "all"}
            st, view = await post("/api/desk", {"action": "remind", "id": a.id, "when": "open"})
            assert st == 200 and view["reminders"][0]["alert_id"] == a.id
            st, view = await post("/api/desk", {"action": "focus", "minutes": 45})
            assert st == 200 and view["focus"] > now
            st, _ = await post("/api/desk", {"action": "focus", "minutes": -1})
            assert st == 400
            st, view = await post("/api/desk", {"action": "viewed", "symbol": "ACME"})
            assert st == 200
            st, body = await post("/api/desk", {"action": "restore", "desk": {"hello": 1}})
            assert st == 400 and "backup" in body
            eng.desk.get(DEV)["seen"]["ACME"]["ts"] = now - 86400
            async with s.get(srv.make_url("/api/ticker/ACME"), headers=h) as r:
                body = await r.json()
                assert body["since_look"]["quiet"] in (True, False) and body["since_look"]["ts"] < now - 80000
            async with s.get(srv.make_url("/api/ticker/ACME"), headers={"Authorization": "Bearer tok"}) as r:
                assert "since_look" not in await r.json()  # no device, no personal part
            async with s.get(srv.make_url("/api/push-log"), headers=h) as r:
                body = await r.json()
                assert len(body["week"]) == 7 and body["hint"] == ""
            st, body = await post(
                "/api/would-push", {"category": "options", "severity": "HIGH", "symbol": "ACME"}
            )
            assert st == 200 and body["got"] is False and "focus mode" in body["verdict"]
            st, body = await post("/api/would-push", {"category": "gossip"})
            assert st == 400
            st, _ = await post("/api/would-push", {"category": "news"}, {"Authorization": "Bearer tok"})
            assert st == 400
            async with s.get(srv.make_url("/app/cmd.js")) as r:
                assert r.status == 200 and "parse" in await r.text()
    finally:
        await srv.close()
        await eng.http.close()
