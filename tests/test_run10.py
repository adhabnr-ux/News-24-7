"""Run 10 of the desk: each phone's taste (what it finds useful), follow-ups an hour after alerts
on its stocks, market memory, the priced earnings week, re-arming price alerts and the
watchlist's sparklines."""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.desk import Desk, taste_rows
from news247.dossier import earnings_week, market_memory
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


def test_taste_learns_from_votes_saves_tracks_and_opens():
    st = Storage(":memory:")
    d = Desk(st)
    from news247.models import Alert, Severity

    opt = [
        Alert(
            kind="options",
            severity=Severity.HIGH,
            title=f"ACME calls {i}",
            body="",
            tickers=["ACME"],
            created=time.time() - i,
        )
        for i in range(4)
    ]
    head = [news(f"Headline {i}") for i in range(4)]
    for a in opt + head:
        st.add_alert(a)
    book = lambda s: (time.time(), 100.0, 0.0)  # noqa: E731
    d.apply(DEV, "vote", {"id": opt[0].id, "v": 1})
    d.apply(DEV, "save", {"id": opt[1].id})
    d.apply(DEV, "track_add", {"symbol": "ACME", "alert_id": opt[2].id}, price_of=book)
    d.apply(DEV, "opened", {"id": opt[3].id})
    d.apply(DEV, "opened", {"id": opt[3].id})  # counted once
    for a in head[:3]:
        d.apply(DEV, "vote", {"id": a.id, "v": -1})
    d.apply(DEV, "vote", {"id": head[0].id, "v": 0})  # took one back
    rows = {r["key"]: r for r in d.view(DEV, lambda s: None)["taste"]}
    o, h = rows["options"], rows["news:HIGH"]
    assert (
        (o["up"], o["saved"], o["tracked"], o["opened"]) == (1, 1, 1, 1)
        and o["score"] > 0.5
        and not o["suggest_off"]
    )
    assert h["down"] == 2 and h["score"] < 0 and h["suggest_off"] is False  # two downvotes: not yet
    d.apply(DEV, "vote", {"id": head[3].id, "v": -1})
    assert {r["key"]: r for r in d.view(DEV, lambda s: None)["taste"]}["news:HIGH"]["suggest_off"] is True
    assert taste_rows({}) == []


def test_price_alerts_can_rearm_every_day():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "move", "value": 3, "rearm": True})
    d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "move", "value": 3})
    q = lambda s: (time.time(), 104.0, 4.0)  # noqa: E731
    day1, day2 = at(11, 0), at(11, 0, d=15)
    assert len(d.due_price_alerts(q, day1)) == 2
    assert d.due_price_alerts(q, day1 + 600) == []
    again = d.due_price_alerts(q, day2)
    assert len(again) == 1 and again[0][1]["rearm"] is True  # only the daily one, the next day


async def test_followups_an_hour_after_an_alert(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        alert = news("Acme wins $2B Pentagon contract")
        alert.created = now - 3600
        alert.edge = {"refs": {"ACME": 100.0}}
        eng.storage.add_alert(alert)
        quotes(eng, now, ACME=3.0)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        eng.desk.apply(DEV2, "watch_add", {"symbol": "ACME"})
        assert await eng.check_followups(now) == 0  # nobody turned follow-ups on
        eng.desk.apply(DEV, "prefs", {"followups": True})
        assert await eng.check_followups(now) == 1 and await eng.check_followups(now + 60) == 0
        msg = a.read(server.requests[0]["body"])
        assert (
            msg["title"] == "↪ ACME +3.0% since the alert an hour ago"
            and msg["open"] == f"/app/#a={alert.id}"
        )
    finally:
        await eng.http.close()


def test_market_memory_builds_day_by_day(tmp_path):
    eng = semis_engine(tmp_path)
    assert eng.save_memory(at(15, 0)) is None  # not before the close
    for d, spy in ((13, 0.5), (14, -0.4)):
        quotes(eng, at(16, 20, d=d), SPY=spy, ACME=2.0, BETA=1.0, DLTA=-1.0)
        snap = eng.save_memory(at(16, 20, d=d))
        assert snap["indexes"]["SPY"] == spy
        assert eng.save_memory(at(16, 30, d=d)) is None  # once a day
    m = market_memory(eng)
    assert [s["date"] for s in m["days"]] == ["2026-10-14", "2026-10-13"]
    assert m["sectors"][0]["sector"] == "Technology" and m["sectors"][0]["sessions"] == 2
    assert json.loads(eng.storage.get_setting("memory:2026-10-14"))["regime"] in (
        "risk-on",
        "risk-off",
        "mixed",
        "unknown",
    )


async def test_earnings_week_carries_warmed_reads(tmp_path):
    eng = semis_engine(tmp_path)
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(
            next={"BETA": {}},
            lookup=lambda s, t=None: {"date": "2026-10-15", "when": "after the close", "in_days": 1},
        )
    )
    eng.implied_cache["BETA"] = (
        time.time(),
        {"rows": [{"expiry": "2026-10-16", "move_pct": 6.2, "earnings": True}]},
    )
    eng.earnings_cache["BETA"] = (time.time(), {"avg_abs_move": 8.1})
    row = earnings_week(eng, at(12, 0))["days"][0]["rows"][0]
    assert (row["symbol"], row["implied"], row["usual"]) == ("BETA", 6.2, 8.1)
    warmed = []

    async def fake(*a, **k):
        warmed.append(a[1])
        return {}

    import news247.personal as P

    P.implied_move, real_im = fake, P.implied_move
    try:
        assert (
            eng.warm_earnings_week(at(12, 0)) == ["BETA"] and eng.warm_earnings_week(at(12, 30)) == []
        )  # hourly
    finally:
        P.implied_move = real_im


async def test_desk_carries_sparklines(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        now = time.time()
        for i in range(30):
            eng.live_quotes.update("ACME", 100 + i * 0.1, 100.0, now - 900 + i * 30, "tape")
        async with aiohttp.ClientSession() as s:
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "watch_add", "symbol": "ACME"}, headers=h
            ) as r:
                spark = (await r.json())["watchlist"][0]["spark"]
                assert 10 <= len(spark) <= 24 and spark[-1] > spark[0]
            async with s.get(srv.make_url("/api/memory"), headers=h) as r:
                assert (await r.json())["days"] == []
    finally:
        await srv.close()
        await eng.http.close()
