"""Run 6 of the desk: the unusual-volume board, market breadth, next-day and 5-day grades, the
buzz board, skipping what fades, category snoozes, saved alerts, export, and the automatic why
for watched stocks moving without news."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.config import build_config
from news247.desk import Desk
from news247.dossier import breadth, buzz, record_outcomes, session_closes, track_record, volume_leaders
from news247.market.ignition import IgnitionScanner, Tick
from news247.models import Analysis, Severity
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import quotes, semis_engine
from .test_run3 import item

ET = ZoneInfo("America/New_York")


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- unusual volume


def test_volume_board_reads_relative_volume_for_the_time_of_day():
    sc = IgnitionScanner(build_config({}).smallcap)
    now = at(11, 0)
    ticks = [
        Tick(
            "LOUD", now, 10.5, 10.0, volume=4_000_000, base_volume=2_000_000, name="Loud Co", market_cap=2e9
        ),
        Tick("CALM", now, 20.0, 20.0, volume=500_000, base_volume=2_000_000, name="Calm Co", market_cap=5e9),
    ]
    sc.observe(ticks, now)
    board = sc.volume_board(now, min_rvol=2.0)
    assert [r["symbol"] for r in board] == ["LOUD"]
    r = board[0]
    assert r["rvol"] > 3 and r["chg"] == 5.0 and r["dollar_volume"] == 42_000_000.0 and r["name"] == "Loud Co"
    assert sc.volume_board(now + 3600, min_rvol=2.0) == []  # a stale read drops off


def test_volume_leaders_carry_the_reason(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    eng.ignition = SimpleNamespace(
        volume_board=lambda now, m: [
            {
                "symbol": "ACME",
                "rvol": 6.2,
                "chg": 4.0,
                "price": 104.0,
                "dollar_volume": 9e7,
                "name": "",
                "market_cap": None,
                "ts": now,
            }
        ]
    )
    eng._remember(
        item("Acme wins $2B Pentagon contract"), Analysis(score=70, severity=Severity.HIGH, tickers=["ACME"])
    )
    v = volume_leaders(eng, now)
    assert (
        v["rows"][0]["name"] == "Acme Semiconductor"
        and v["rows"][0]["cap"] == "$5.0B"
        and v["rows"][0]["why"]["title"].startswith("Acme wins")
    )
    eng.ignition = None
    assert volume_leaders(eng, now) == {"enabled": False, "rows": []}


# --------------------------------------------------------------------------- breadth


def test_breadth(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    assert breadth(eng, now)["n"] == 0
    quotes(eng, now, ACME=6.0, BETA=-1.0, DLTA=0.0, GAMA=2.0)
    b = breadth(eng, now)
    assert (b["n"], b["up"], b["down"], b["flat"], b["up5"], b["down5"]) == (4, 2, 1, 1, 1, 0)
    assert b["pct_up"] == 50.0 and b["equal_weighted"] == 1.75
    assert b["cap_weighted"] == pytest.approx((6 * 5 - 1 * 20 + 0 * 1 + 2 * 8) / 34, abs=0.01)
    assert [x["band"] for x in b["bands"]] == ["large cap", "mid cap", "small cap"]


# --------------------------------------------------------------------------- multi-day grades


def test_session_closes_skip_weekends_and_holidays():
    friday_after = at(17, 0, d=16)  # after Friday's close: the next session is Monday
    closes = [
        datetime.fromtimestamp(t, ET).date().isoformat()
        for t in session_closes(friday_after, 3, {"2026-10-20"})
    ]
    assert closes == ["2026-10-19", "2026-10-21", "2026-10-22"]


def test_alerts_get_next_day_and_five_day_grades(tmp_path):
    eng = semis_engine(tmp_path)
    created = at(10, 0)
    a = news()
    a.created = created
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    closes = session_closes(created, 6, eng.calendar.holidays)
    for ts, px in (
        (created + 3700, 101.0),
        (closes[0] - 60, 102.0),
        (closes[1] - 60, 103.0),
        (closes[5] - 60, 110.0),
    ):
        eng.live_quotes.update("ACME", px, 100.0, ts, "tape")
        record_outcomes(eng, ts + 120)
    out = eng.storage.get_alert(a.id)["edge"]["outcome"]["ACME"]
    assert (out["h1"], out["close"], out["d1"], out["d5"], out["sign"]) == (1.0, 2.0, 3.0, 10.0, 1)
    rows = {r["kind"]: r for r in track_record(eng, 30, closes[5] + 60)["rows"]}
    assert rows["news:HIGH"]["d5"] == {"n": 1, "hit": 1.0, "median": 10.0}


# --------------------------------------------------------------------------- buzz


def test_buzz_board_finds_sudden_coverage(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for i, src in enumerate(["globenewswire", "gnews", "yahoo", "cnbc"]):
        it = item(f"Acme story {i}", source=src)
        it.detected = now - 600 - i
        eng.storage.add_item(it, Analysis(score=30 + i, severity=Severity.MEDIUM, tickers=["ACME"]))
    for i in range(3):  # BETA: three posts from one source is not buzz
        it = item(f"Beta post {i}", source="bluesky", tickers=("BETA",))
        it.detected = now - 300
        eng.storage.add_item(it, Analysis(score=10, severity=Severity.LOW, tickers=["BETA"]))
    old = item("Acme last week", source="gnews")
    old.detected = now - 3 * 86400
    eng.storage.add_item(old, Analysis(score=20, severity=Severity.LOW, tickers=["ACME"]))
    rows = buzz(eng, now)["rows"]
    assert [r["symbol"] for r in rows] == ["ACME"]
    assert (
        rows[0]["n"] == 4
        and rows[0]["sources"] == 4
        and rows[0]["top"]["title"] == "Acme story 3"
        and rows[0]["ratio"] > 10
    )


# --------------------------------------------------------------------------- skip what fades, snooze, saved


def test_skip_fades_and_snooze():
    d = Desk(Storage(":memory:"))
    fading = news()
    fading.edge = {"base": {"fades": True}}
    assert d.allows(DEV, fading) is True
    d.apply(DEV, "prefs", {"skip_fades": True})
    assert d.allows(DEV, fading) is False and d.allows(DEV, news()) is True
    crit = news(sev=Severity.CRITICAL)
    crit.edge = {"base": {"fades": True}}
    assert d.allows(DEV, crit) is True  # CRITICAL always comes
    with pytest.raises(ValueError):
        d.apply(DEV, "snooze", {"cat": "nope", "hours": 1})
    d.apply(DEV, "snooze", {"cat": "news", "hours": 1})
    assert d.allows(DEV, news()) is False and d.allows(DEV, news(), time.time() + 3700) is True
    assert set(d.view(DEV, lambda s: None)["snooze"]) == {"news"}
    d.apply(DEV, "snooze", {"cat": "news", "hours": 0})
    assert d.allows(DEV, news()) is True


def test_saved_alerts():
    d = Desk(Storage(":memory:"))
    for aid in ("a1", "a2", "a1"):
        d.apply(DEV, "save", {"id": aid})
    assert d.get(DEV)["saved"] == ["a1", "a2"]
    d.apply(DEV, "unsave", {"id": "a2"})
    assert d.view(DEV, lambda s: None)["saved"] == ["a1"]


# --------------------------------------------------------------------------- auto why


async def test_auto_why_for_watched_movers_without_news(tmp_path):
    eng = semis_engine(tmp_path)
    hunted = []

    async def find_why(sym, now=None):
        hunted.append(sym)
        return {}

    eng.why = SimpleNamespace()
    eng.find_why = find_why
    now = at(11, 2)
    quotes(eng, now, ACME=6.0, BETA=-5.0, DLTA=1.0)
    eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA", "DLTA"]})
    beta = item("Beta cuts guidance", tickers=("BETA",))
    beta.detected = now - 600
    eng._remember(beta, Analysis(score=70, severity=Severity.HIGH, tickers=["BETA"]))
    assert await eng.auto_why(at(10, 0)) == []  # not one of its two windows
    assert await eng.auto_why(now) == ["ACME"]  # BETA has its reason; DLTA isn't moving
    assert await eng.auto_why(now + 60) == [] and hunted == ["ACME"]  # once a day


# --------------------------------------------------------------------------- the API


async def test_run6_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            quotes(eng, time.time(), ACME=6.0, BETA=-1.0)
            eng.desk.apply(
                DEV,
                "track_add",
                {"symbol": "ACME", "direction": "long", "entry": 100, "note": "contract #news"},
                eng.price_of,
            )
            for path in ("/api/breadth", "/api/volume", "/api/buzz"):
                async with s.get(srv.make_url(path), headers=h) as r:
                    assert r.status == 200, path
            async with s.get(srv.make_url("/api/export"), headers=h) as r:
                body = json.loads(await r.text())
                assert (
                    body["foretape_desk"] == 1
                    and body["desk"]["tracked"][0]["symbol"] == "ACME"
                    and "digest_q" not in body["desk"]
                )
            async with s.get(srv.make_url("/api/export?format=csv"), headers=h) as r:
                rows = list(csv.reader(io.StringIO(await r.text())))
                assert (
                    r.content_type == "text/csv"
                    and rows[0][:3] == ["status", "symbol", "direction"]
                    and rows[1][:2] == ["open", "ACME"]
                    and rows[1][10] == "news"
                )
            async with s.get(srv.make_url("/api/export"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
