"""Run 5 of the desk: the day in one screen, the source scorecard, "your week", and the SPY marks
the timeline draws."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.dossier import day_timeline, source_record
from news247.personal import week_card, week_push
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import quotes, semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def at(h, m, d=16):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()  # Oct 16 2026 is a Friday


def graded(title, source, created, h1, close, sign=1, sev=None, tickers=("ACME",)):
    from news247.models import Severity

    a = news(title, tickers=tickers, sev=sev or Severity.HIGH)
    a.item.source = source
    a.created = created
    a.edge = {"outcome": {tickers[0]: {"h1": h1, "close": close, "sign": sign}}}
    return a


def test_spy_is_marked_every_five_minutes(tmp_path):
    eng = semis_engine(tmp_path)
    for i, t in enumerate((at(10, 0), at(10, 2), at(10, 6), at(10, 11))):
        eng.live_quotes.update("SPY", 600 + i, 600.0, t, "tape")
        eng.mark_day(t)
    assert [round(p) for _, p in eng.day_marks["SPY"]] == [600, 602, 603]  # 10:02 is too soon after 10:00
    eng.live_quotes.update("SPY", 610, 600.0, at(9, 35, d=19), "tape")
    eng.mark_day(at(9, 35, d=19))
    assert [p for _, p in eng.day_marks["SPY"]] == [610]  # a new day starts a new line


def test_the_day_in_one_screen(tmp_path):
    eng = semis_engine(tmp_path)
    now = at(15, 0)
    eng.storage.add_alert(graded("Acme wins a contract", "globenewswire", at(10, 5), 1.0, 2.0))
    eng.storage.add_alert(graded("Yesterday's news", "globenewswire", at(15, 0, d=15), 1.0, 1.0))
    late = news("Beta guides lower", tickers=("BETA",), direction="down")
    late.created = at(13, 30)
    eng.storage.add_alert(late)
    eng.day_marks["SPY"] = [(at(9, 30), 600.0), (at(9, 35), 601.0)]
    d = day_timeline(eng, now)
    assert d["date"] == "2026-10-16" and [r["title"] for r in d["rows"]] == [
        "Acme wins a contract",
        "Beta guides lower",
    ]
    assert (
        d["rows"][0]["grade"]["close"] == 2.0
        and d["rows"][1]["direction"] == "down"
        and d["rows"][1]["grade"] is None
    )
    assert d["counts"]["HIGH"] == 2 and len(d["spy"]) == 2


def test_source_scorecard(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for i, (h1, close) in enumerate([(1.0, 2.0), (0.5, -1.0), (-0.2, 0.4)]):
        eng.storage.add_alert(graded(f"Wire story {i}", "globenewswire", now - 3600 * (i + 2), h1, close))
    eng.storage.add_alert(graded("Lone post", "bluesky", now - 7200, 1.0, 1.0))
    eng.storage.add_alert(graded("Short call", "sec-8k", now - 7200, -1.0, -2.0, sign=-1))
    rows = {r["source"]: r for r in source_record(eng, now=now, min_n=1)["rows"]}
    assert (
        rows["globenewswire"]["h1"] == {"n": 3, "hit": 0.67, "median": 0.5}
        and rows["globenewswire"]["close"]["hit"] == 0.67
    )
    assert rows["sec-8k"]["close"]["hit"] == 1.0  # a bearish alert whose stock fell went its way
    assert [r["source"] for r in source_record(eng, now=now)["rows"]] == [
        "globenewswire"
    ]  # 3+ graded to show


async def test_your_week(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = at(16, 40)
        assert week_card(eng, DEV, now) is None
        quotes(eng, now, ACME=6.0)
        eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
        p = eng.desk.get(DEV)
        p["closed"] = [{"id": "c1", "symbol": "ACME", "direction": "long", "entry": 100, "exit": 110, "pnl_pct": 10.0, "ts": at(10, 0, d=13), "closed": at(11, 0, d=14)},
                       {"id": "c2", "symbol": "BETA", "direction": "long", "entry": 50, "exit": 49, "pnl_pct": -2.0, "ts": at(10, 0, d=6), "closed": at(11, 0, d=9)}]  # fmt: skip
        eng.desk.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100}, eng.price_of)
        eng.storage.add_alert(graded("Acme contract", "globenewswire", at(10, 0, d=13), 1.0, 2.0))
        eng.trends = SimpleNamespace(
            earnings=SimpleNamespace(
                lookup=lambda s, t=None: (
                    {"date": "2026-10-21", "when": "after the close", "in_days": 5} if s == "BETA" else None
                )
            )
        )
        card = week_card(eng, DEV, now)
        assert (
            card["week_of"] == "2026-10-12"
            and card["closed"]["n"] == 1
            and card["closed"]["best"]["symbol"] == "ACME"
        )  # last week's call is out
        assert card["open"] == {"n": 1, "avg": 6.0} and card["alerts"] == {"graded": 1, "right": 1}
        assert card["next"]["earnings"][0]["symbol"] == "BETA"
        body = week_push(card)["body"].splitlines()
        assert body[:4] == ["Closed 1 call(s): 1 won, avg +10.0% · best ACME +10.0%", "Open: 1 call(s), avg +6.0%",
                            "Alerts on your stocks: 1 of 1 went their way by the close", "Next: BETA reports Wed"]  # fmt: skip
        assert eng._week_due(now) and not eng._week_due(at(15, 0)) and not eng._week_due(at(16, 40, d=15))
        assert await eng.send_week_cards(now) == 1 and not eng._week_due(now + 60)
        assert a.read(server.requests[0]["body"])["title"] == "🗓 Your week"
    finally:
        await eng.http.close()


async def test_run5_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            for path in ("/api/day", "/api/sources", "/api/week"):
                async with s.get(srv.make_url(path), headers=h) as r:
                    assert r.status == 200, path
            async with s.get(srv.make_url("/api/week"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
