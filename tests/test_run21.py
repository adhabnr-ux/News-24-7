"""Run 21: divergence (a strong kind of news, and the stock going the other way) and portfolio
event risk (each holding's next report or event, in dollars on the position)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.catalysts import divergence_record
from news247.plans import event_risk
from news247.web.server import WebServer

from .test_desk import DEV, DEV2
from .test_personal import quotes, semis_engine
from .test_run3 import phones
from .test_run14 import market_open
from .test_run19 import contract_alert

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- divergence


async def test_good_news_and_the_stock_falling(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        alert = contract_alert(eng, ago=1200)  # 20 minutes old, contract win: 83% up by the close
        eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 10})
        eng.live_quotes.update("ACME", 99.0, 99.0, now, "tape")
        assert await eng.check_divergence(now) == 0  # -1.0%: not enough (1.5%, or half of 2.75)
        eng.live_quotes.update("ACME", 97.9, 99.0, now + 60, "tape")
        assert await eng.check_divergence(now + 60) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "⚠ ACME -2.1% on good news" and "isn't buying it" in msg["body"]
        assert (
            "Contract win, $2-10B: 83% went their way" in msg["body"] and msg["open"] == f"/app/#a={alert.id}"
        )
        assert await eng.check_divergence(now + 120) == 0  # once
        log = json.loads(eng.storage.get_setting("diverge_log"))
        assert [(e["symbol"], e["against"]) for e in log] == [("ACME", 2.1)]
        close = (
            datetime.fromtimestamp(log[0]["ts"], ET)
            .replace(hour=16, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        eng.live_quotes.update("ACME", 96.0, 99.0, close + 60, "tape")
        assert eng.grade_divergence(close + 60) == 1
        rec = divergence_record(eng, close + 60)
        assert (
            rec["flags"] == 1 and rec["close"]["median"] == 4.0
        )  # kept going against: 4% down from the alert
    finally:
        await eng.http.close()


async def test_divergence_waits_its_window_and_respects_the_phone(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    await eng.http.start()
    try:
        phones(eng, server)
        now = time.time()
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        contract_alert(eng, ago=300)  # 5 minutes: too early to judge
        eng.live_quotes.update("ACME", 97.0, 99.0, now, "tape")
        assert await eng.check_divergence(now) == 0
        eng.storage.db.execute("DELETE FROM alerts")
        contract_alert(eng, ago=2 * 3600)  # two hours: too late to be news
        assert await eng.check_divergence(now) == 0
        eng.storage.db.execute("DELETE FROM alerts")
        contract_alert(eng, ago=1200)
        eng.desk.apply(DEV, "prefs", {"diverge": False})
        assert await eng.check_divergence(now) == 0
        eng.desk.apply(DEV, "prefs", {"diverge": True})
        eng.desk.apply(DEV, "mute", {"symbol": "ACME", "hours": 1})
        assert await eng.check_divergence(now) == 0
        eng.desk.apply(DEV, "unmute", {"symbol": "ACME"})
        eng.calendar = SimpleNamespace(market_status=lambda t=None: {"phase": "closed"}, holidays=set())
        assert await eng.check_divergence(now) == 0
        market_open(eng)
        assert await eng.check_divergence(now) == 1
        # bad news, stock rising: the same, the other way
        eng.storage.db.execute("DELETE FROM alerts")
        contract_alert(eng, "Beta guides lower", sym="BETA", direction="down", ago=1200)
        eng.desk.apply(DEV2, "watch_add", {"symbol": "BETA"})
        eng.live_quotes.update("BETA", 102.0, 99.0, now, "tape")
        assert await eng.check_divergence(now) == 1
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- event risk


def test_each_holdings_next_event_in_dollars(tmp_path):
    eng = semis_engine(tmp_path)
    now = datetime(2026, 10, 14, 12, 0, tzinfo=ET).timestamp()
    quotes(eng, now, ACME=0.0, BETA=0.0, GAMA=0.0)  # all at $100
    eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 50})
    eng.desk.apply(DEV, "hold_set", {"symbol": "BETA", "shares": 20})
    eng.desk.apply(DEV, "hold_set", {"symbol": "GAMA", "shares": 30})
    nxt = {"ACME": ("2026-10-15", 1), "BETA": ("2026-10-30", 16)}
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(
        lookup=lambda s, t=None: {"date": nxt[s][0], "when": "after the close", "in_days": nxt[s][1]} if s in nxt else None))  # fmt: skip
    eng.implied_cache["ACME"] = (now, {"rows": [{"expiry": "2026-10-16", "move_pct": 6.0, "earnings": True}]})
    eng.earnings_cache["ACME"] = (now, {"avg_abs_move": 8.0})
    eng.storage.add_catalyst({"id": "GAMA:2026-10-20:pdufa", "date": "2026-10-20", "time": "", "title": "GAMA · PDUFA date",
                              "kind": "binary", "event": "pdufa", "symbol": "GAMA", "headline": "x"})  # fmt: skip
    r = event_risk(eng, DEV, now)
    assert [(e["symbol"], e["kind"], e["in_days"]) for e in r["events"]] == [
        ("ACME", "earnings", 1),
        ("GAMA", "pdufa", 6),
    ]
    acme = r["events"][0]
    assert (acme["value"], acme["implied_usd"], acme["usual_usd"]) == (5000.0, 300.0, 400.0)
    assert r["events"][1]["implied_usd"] is None and r["events"][1]["title"] == "GAMA · PDUFA date"
    assert (
        r["implied_usd"] == 300.0 and r["exposed_value"] == 8000.0 and r["exposed_pct"] == pytest.approx(80.0)
    )
    assert [e["symbol"] for e in event_risk(eng, DEV, now, days=30)["events"]] == [
        "ACME",
        "GAMA",
        "BETA",
    ]  # BETA: 16 days out
    assert event_risk(eng, DEV2, now)["events"] == []


async def test_earnings_reminders_carry_your_dollars(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = datetime(2026, 10, 14, 15, 40, tzinfo=ET).timestamp()
        quotes(eng, now, ACME=0.0)
        eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 50})  # held, not on the watchlist
        eng.trends = SimpleNamespace(earnings=SimpleNamespace(
            next={"ACME": {}}, lookup=lambda s, t=None: {"date": "2026-10-14", "when": "after the close", "in_days": 0}))  # fmt: skip

        async def implied(engine, sym, now=None):
            return {"rows": [{"expiry": "2026-10-16", "move_pct": 6.0, "earnings": True}]}

        async def reactions(engine, sym, now=None):
            return {}

        import news247.personal as P

        P.implied_move, P.earnings_reactions, real = (
            implied,
            reactions,
            (P.implied_move, P.earnings_reactions),
        )
        try:
            assert await eng.send_earnings_reminders(now) == 1
        finally:
            P.implied_move, P.earnings_reactions = real
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "📅 ACME reports after the close"
        assert msg["body"] == "options price ±6.0% by 10-16 · about ±$300 on your 50 shares"
    finally:
        await eng.http.close()


async def test_run21_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/event-risk"), headers=h) as r:
                assert (await r.json())["events"] == []
            async with s.get(srv.make_url("/api/record"), headers=h) as r:
                assert (await r.json())["diverge"]["flags"] == 0
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "prefs", "diverge": False}, headers=h
            ) as r:
                assert (await r.json())["prefs"]["diverge"] is False
    finally:
        await srv.close()
        await eng.http.close()
