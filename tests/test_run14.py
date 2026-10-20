"""Run 14: sympathy pushes (a peer of your stock jumps on news and yours hasn't moved) with their
own graded record, and the strategy lab (following every alert like this, on paper)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.dossier import grade_sympathy, strategy_lab, sympathy_record
from news247.models import Alert, Severity
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import phones


def market_open(eng):
    eng.calendar = SimpleNamespace(
        market_status=lambda t=None: {"phase": "open"}, holidays=set(), early=set()
    )


# --------------------------------------------------------------------------- sympathy


async def test_a_peer_jumps_and_your_stock_has_not_moved(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        quotes(eng, now, ACME=6.0, BETA=0.5, DLTA=5.0, GAMA=0.0)
        eng.desk.apply(DEV, "watch_add", {"symbol": "BETA"})
        eng.desk.apply(DEV2, "watch_add", {"symbol": "GAMA"})  # health care: not a peer
        lead = news("Acme wins $2B Pentagon contract")
        lead.created = now - 600
        eng.storage.add_alert(lead)
        assert await eng.check_sympathy(now) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "🔗 ACME +6.0%: your BETA hasn't moved (+0.5%)"
        assert (
            "ACME's news: “Acme wins $2B Pentagon contract”" in msg["body"] and "not a signal" in msg["body"]
        )
        assert msg["open"] == "/app/#t=BETA" and [r["path"] for r in server.requests] == ["/push/a"]
        assert await eng.check_sympathy(now + 60) == 0  # once a day per phone, leader and peer
        log = json.loads(eng.storage.get_setting("sympathy_log"))
        assert [(e["lead"], e["lag"], e["sign"]) for e in log] == [("ACME", "BETA", 1)]
        assert log[0]["ref"] == pytest.approx(100.5)
        # graded an hour later: BETA caught up 2%
        quotes(eng, now + 3600, BETA=2.51)
        assert grade_sympathy(eng, now + 3600) == 1
        rec = sympathy_record(eng, now + 3600)
        assert (
            rec["flags"] == 1 and rec["h1"]["n"] == 1 and rec["h1"]["median"] == pytest.approx(2.0, abs=0.01)
        )
    finally:
        await eng.http.close()


async def test_sympathy_is_honest_and_respects_the_phone(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    await eng.http.start()
    try:
        phones(eng, server)
        now = time.time()
        eng.desk.apply(DEV, "watch_add", {"symbol": "BETA"})
        down = news("Acme wins $2B Pentagon contract", direction="down")
        eng.storage.add_alert(down)
        quotes(eng, now, ACME=6.0, BETA=0.5)
        assert await eng.check_sympathy(now) == 0  # moving against its own news: not a lead
        eng.storage.db.execute("DELETE FROM alerts")
        eng.storage.add_alert(news("Acme guides higher", sev=Severity.MEDIUM))
        assert await eng.check_sympathy(now) == 0  # only HIGH alerts lead
        eng.storage.add_alert(news("Acme wins $2B Pentagon contract"))
        quotes(eng, now, ACME=3.0, BETA=0.0)
        assert await eng.check_sympathy(now) == 0  # under 4%: not a jump
        quotes(eng, now, ACME=6.0, BETA=2.0)
        assert await eng.check_sympathy(now) == 0  # BETA already moved a third of it
        quotes(eng, now, ACME=6.0, BETA=0.5)
        eng.desk.apply(DEV, "prefs", {"sympathy": False})
        assert await eng.check_sympathy(now) == 0  # turned off
        eng.desk.apply(DEV, "prefs", {"sympathy": True})
        eng.desk.apply(DEV, "mute", {"symbol": "BETA", "hours": 1})
        assert await eng.check_sympathy(now) == 0  # muted
        eng.desk.apply(DEV, "unmute", {"symbol": "BETA"})
        eng.calendar = SimpleNamespace(market_status=lambda t=None: {"phase": "closed"}, holidays=set())
        assert await eng.check_sympathy(now) == 0  # only in the session
        market_open(eng)
        assert await eng.check_sympathy(now) == 1
        # at most three a phone a day
        for lead in ("DLTA", "GAMA", "ZETA", "OMGA"):
            eng.storage.add_alert(
                Alert(kind="news", severity=Severity.HIGH, title=f"{lead} news", body="", tickers=[lead])
            )
        eng._sympathy_day["count"][DEV] = 3
        assert await eng.check_sympathy(now + 60) == 0
    finally:
        await eng.http.close()


def test_the_record_line_shows_once_there_are_enough_flags(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    log = [{"day": "x", "ts": now - 86400 * i, "lead": "A", "lag": "B", "sign": 1, "ref": 1.0,
            "h1": 0.5, "close": (1.0 if i % 3 else -1.0)} for i in range(1, 10)]  # fmt: skip
    eng.storage.set_setting("sympathy_log", json.dumps(log))
    rec = sympathy_record(eng, now)
    assert rec["close"] == {"n": 9, "hit": 0.67, "median": 1.0} and rec["flags"] == 9
    assert sympathy_record(eng, now, days=1.5)["flags"] == 1


# --------------------------------------------------------------------------- the strategy lab


def graded(title, sev, kind, ret_close, sign=1, sym="ACME", created=None, **edge):
    a = news(title, sev=sev, tickers=(sym,))
    a.kind = kind
    a.created = created or time.time() - 86400
    a.edge = {
        "outcome": {sym: {"h1": ret_close / 2, "close": ret_close * sign, "d1": None, "sign": sign}},
        **edge,
    }
    return a


def test_strategy_lab_follows_every_alert_on_paper(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    rows = [
        graded("win", Severity.HIGH, "news", 4.0, created=now - 5 * 3600),
        graded("loss", Severity.HIGH, "news", -2.0, created=now - 4 * 3600),
        graded("short win", Severity.HIGH, "options", 3.0, sign=-1, sym="BETA", created=now - 3 * 3600),
        graded("medium", Severity.MEDIUM, "news", 10.0, created=now - 2 * 3600),
        graded("fader", Severity.CRITICAL, "news", -5.0, created=now - 3600, base={"fades": True}),
    ]
    for a in rows:
        eng.storage.add_alert(a)
    ungraded = news("no outcome yet")
    eng.storage.add_alert(ungraded)
    lab = strategy_lab(eng, now=now)
    assert lab["n"] == 4 and lab["wins"] == 2 and lab["hit"] == 0.5
    assert lab["pnl"] == pytest.approx(10 * (4 - 2 + 3 - 5)) and lab["avg"] == 0.0
    assert [round(c[1]) for c in lab["curve"]] == [40, 20, 50, 0]  # in time order
    assert lab["max_drawdown"] == -50.0 and lab["best"]["title"] == "win" and lab["worst"]["title"] == "fader"
    assert lab["best"]["ret"] == 4.0 and lab["recent"][0]["title"] == "fader"
    assert {r["kind"]: r["n"] for r in lab["by_kind"]} == {"news": 3, "options": 1}
    assert strategy_lab(eng, now=now, skip_fades=True)["pnl"] == pytest.approx(50.0)
    assert strategy_lab(eng, now=now, min_sev="MEDIUM")["n"] == 5
    assert strategy_lab(eng, now=now, kind="options")["pnl"] == pytest.approx(30.0)
    assert strategy_lab(eng, now=now, horizon="h1")["n"] == 4
    assert strategy_lab(eng, now=now, horizon="d1")["n"] == 0  # not graded that far yet
    assert strategy_lab(eng, now=now, stake=500)["pnl"] == pytest.approx(0.0)
    # only the stocks this phone watches or holds
    eng.desk.apply(DEV, "hold_set", {"symbol": "BETA", "shares": 1})
    assert strategy_lab(eng, DEV, now=now, scope="mine")["n"] == 1
    assert strategy_lab(eng, DEV2, now=now, scope="mine")["n"] == 0
    for bad in ({"kind": "gossip"}, {"horizon": "week"}, {"min_sev": "LOW"}, {"scope": "theirs"}):
        with pytest.raises(ValueError):
            strategy_lab(eng, now=now, **bad)


async def test_run14_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        eng.storage.add_alert(graded("win", Severity.HIGH, "news", 4.0))
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/lab?sev=high&horizon=close&stake=2000"), headers=h) as r:
                body = await r.json()
                assert body["n"] == 1 and body["pnl"] == 80.0 and body["horizon_label"] == "at the close"
            async with s.get(srv.make_url("/api/lab?kind=gossip"), headers=h) as r:
                assert r.status == 400
            async with s.get(srv.make_url("/api/lab?days=abc"), headers=h) as r:
                assert r.status == 400
            async with s.get(srv.make_url("/api/record"), headers=h) as r:
                assert (await r.json())["sympathy"]["flags"] == 0
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "prefs", "sympathy": False}, headers=h
            ) as r:
                assert (await r.json())["prefs"]["sympathy"] is False
    finally:
        await srv.close()
        await eng.http.close()
