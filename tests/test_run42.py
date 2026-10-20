"""Run 42: what we caught, what we missed (every big move of the session against the alerts on it)
and the call replayed on history (its rule on every graded alert, and which facts sorted winners
from losers)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.models import Analysis, NewsItem, Severity
from news247.recall import movers, recall_report, snapshot
from news247.thecall import replay, the_call
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 10, 14, 15, 0, tzinfo=ET).timestamp()  # Wednesday, 15:00 ET


def tape(eng, now):
    q = eng.live_quotes
    q.update("ACME", 100.0, 100.0, now - 4 * 3600, "tape")
    q.update("ACME", 108.0, 100.0, now - 3600, "tape")  # its high, an hour ago
    q.update("ACME", 107.0, 100.0, now - 60, "tape")
    q.update("BETA", 47.0, 50.0, now - 60, "tape")  # -6%
    q.update("DLTA", 10.55, 10.0, now - 60, "tape")  # +5.5%
    q.update("GAMA", 20.4, 20.0, now - 60, "tape")  # +2%: not a mover
    a = news("Acme wins $900M Navy contract")
    a.created = now - 2 * 3600  # an hour before its high
    eng.storage.add_alert(a)
    it = NewsItem(
        source="seekingalpha",
        title="Beta Chips slides on soft guidance chatter",
        url="https://x.test/beta",
        detected=now - 3 * 3600,
    )
    eng.storage.add_item(it, Analysis(score=41.5, severity=Severity.LOW, tickers=["BETA"], direction="down"))
    return a


def test_every_big_move_is_checked(tmp_path):
    eng = semis_engine(tmp_path)
    a = tape(eng, DAY)
    rows = {r["symbol"]: r for r in movers(eng, DAY)}
    assert set(rows) == {"ACME", "BETA", "DLTA"}  # GAMA's 2% isn't a big move
    acme = rows["ACME"]
    assert acme["caught"] and acme["alert_id"] == a.id and acme["lead_min"] == 60.0 and acme["chg"] == 7.0
    beta = rows["BETA"]
    assert (beta["caught"], beta["why"], beta["top_score"]) == (False, "under the line", 41.5)
    assert beta["top_title"] == "Beta Chips slides on soft guidance chatter"
    assert (rows["DLTA"]["caught"], rows["DLTA"]["why"]) == (False, "no headline")
    assert [r["symbol"] for r in movers(eng, DAY)] == ["ACME", "BETA", "DLTA"]  # biggest first


def test_each_session_is_kept_and_read(tmp_path):
    eng = semis_engine(tmp_path)
    tape(eng, DAY)
    assert snapshot(eng, DAY) == 0  # 15:00: the session isn't over
    close = DAY + 3900  # 16:05
    assert snapshot(eng, close) == 3 and snapshot(eng, close + 60) == 0  # once
    kept = json.loads(eng.storage.get_setting("recall:2026-10-14"))
    assert kept["day"] == "2026-10-14" and len(kept["rows"]) == 3
    eng.storage.set_setting("recall:2026-10-13", json.dumps({"day": "2026-10-13", "rows": [
        {"symbol": "GAMA", "chg": 9.0, "band": "small", "sector": "Health Care", "caught": True, "lead_min": -5.0, "why": "alerted"}]}))  # fmt: skip
    r = recall_report(eng, now=close)
    assert r["sessions"] == 2 and r["all"] == {"n": 4, "caught": 2, "rate": 0.5}
    assert r["lead"] == {"n": 2, "median": 27.5, "before": 0.5}
    assert r["why"] == {"under the line": 1, "no headline": 1} and r["under_score"] == 41.5
    assert [m["symbol"] for m in r["missed"]] == ["BETA", "DLTA"] and r["missed"][0]["day"] == "2026-10-14"
    assert {b["band"] for b in r["bands"]} >= {"small"}


# --------------------------------------------------------------------------- the call, replayed


def graded(eng, n, close, **edge):
    for i in range(n):
        a = news(f"Acme item {close} {i} {len(edge)}")
        a.created = time.time() - 86400 * 5 - i * 600
        a.edge = {**edge, "outcome": {"ACME": {"close": close, "sign": 1}}}
        eng.storage.add_alert(a)


def test_the_rule_on_every_graded_alert(tmp_path):
    eng = semis_engine(tmp_path)
    graded(eng, 10, 1.5, odds={"p": 0.66})  # odds for it: went its way
    graded(eng, 10, -1.0, odds={"p": 0.40})  # odds against it: didn't
    graded(eng, 8, 0.5)  # nothing graded on them: no call
    r = replay(eng)
    assert r["all"]["n"] == 28
    verbs = {v["verb"]: v for v in r["verbs"]}
    assert verbs["skip"] == {
        "verb": "skip",
        "label": "Skip",
        "icon": "✋",
        "n": 10,
        "hit": 0.0,
        "median": -1.0,
    }
    assert verbs["watch"]["n"] == 10 and verbs["watch"]["hit"] == 1.0  # one fact for, nothing else: watch
    odds = next(f for f in r["facts"] if f["key"] == "odds")
    assert odds["for"]["hit"] == 1.0 and odds["against"]["hit"] == 0.0 and odds["sorts"] == 1.0
    assert the_call({"odds": {"p": 0.4}})["facts"] == [["odds", "-"]]


async def test_run42_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    graded(eng, 10, 1.5, odds={"p": 0.66})
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/call"), headers=h) as r:
                body = await r.json()
                assert body["replay"]["verbs"][0]["verb"] == "watch"
            async with s.get(srv.make_url("/api/recall"), headers=h) as r:
                body = await r.json()
                assert body["sessions"] == 0 and body["all"] is None and body["today"] == []
    finally:
        await srv.close()
        await eng.http.close()
