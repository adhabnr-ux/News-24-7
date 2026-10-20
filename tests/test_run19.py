"""Run 19: second chance (a strong kind of news back at its alert price) and what's working today
(today's alerts graded live by kind of news)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.catalysts import retest_record, working_today
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run14 import market_open
from .test_run15 import graded

ET = ZoneInfo("America/New_York")


def contract_alert(eng, title="Acme wins $900M Navy contract", sym="ACME", direction="up", ago=600):
    a = news(title, tickers=(sym,), direction=direction)
    a.created = time.time() - ago
    a.edge = {"refs": {sym: 100.0}, "catalyst": {"key": "contract", "label": "Contract win", "band": "mid"},
              "cat_base": {"label": "Contract win", "band_label": "$2-10B", "typical": 2.75,
                           "close": {"n": 6, "hit": 0.83, "median": 2.75}}}  # fmt: skip
    eng.storage.add_alert(a)
    return a


# --------------------------------------------------------------------------- what's working today


def test_whats_working_today(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for i, sym in enumerate(("ACME", "BETA", "DLTA")):
        contract_alert(eng, f"{sym} wins contract", sym=sym)
        eng.live_quotes.update(sym, 100 + (2.0, 3.0, -1.0)[i], 99.0, now, "tape")
    up = news("Morgan Stanley upgrades Gamma", tickers=("GAMA",))
    up.edge = {"refs": {"GAMA": 50.0}}
    eng.storage.add_alert(up)
    eng.live_quotes.update("GAMA", 49.0, 50.0, now, "tape")
    plain = news("Acme hosts investor day")  # no kind of news: not counted
    eng.storage.add_alert(plain)
    w = working_today(eng, now)
    row = w["rows"][0]
    assert (row["label"], row["n"], row["avg"], row["hit"]) == ("Contract win", 3, 1.33, 0.67)
    assert [x["symbol"] for x in row["alerts"]] == ["BETA", "ACME", "DLTA"]
    assert [t["label"] for t in w["thin"]] == ["Analyst upgrade"] and w["thin"][0]["avg"] == -2.0
    assert w["graded"] == 4
    assert working_today(eng, now, min_n=1)["rows"][-1]["label"] == "Analyst upgrade"


# --------------------------------------------------------------------------- second chance


async def test_second_chance_when_a_strong_kind_comes_back(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        alert = contract_alert(eng)
        eng.desk.apply(DEV, "watch_add", {"symbol": "BETA"})  # not ACME...
        eng.desk.apply(DEV, "opened", {"id": alert.id})  # ...but it opened the alert
        eng.live_quotes.update("ACME", 100.6, 99.0, now, "tape")
        assert await eng.check_retests(now) == 0  # hasn't run yet
        eng.live_quotes.update("ACME", 102.4, 99.0, now + 60, "tape")
        assert await eng.check_retests(now + 60) == 0  # running: not back
        eng.live_quotes.update("ACME", 100.2, 99.0, now + 120, "tape")
        assert await eng.check_retests(now + 120) == 1  # ran +2.4%, back within 0.3%
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "↩ ACME is back at its alert price ($100.20)"
        assert "It ran +2.4% its way" in msg["body"] and "83% went their way by the close" in msg["body"]
        assert msg["open"] == f"/app/#a={alert.id}" and [r["path"] for r in server.requests] == ["/push/a"]
        assert await eng.check_retests(now + 180) == 0  # once per phone and alert
        log = json.loads(eng.storage.get_setting("retest_log"))
        assert [(e["symbol"], e["ran"], e["sign"]) for e in log] == [("ACME", 2.4, 1)]
        # graded at the close
        close = (
            __import__("datetime")
            .datetime.fromtimestamp(
                log[0]["ts"], eng.calendar and __import__("zoneinfo").ZoneInfo("America/New_York")
            )
            .replace(hour=16, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        eng.live_quotes.update("ACME", 101.5, 99.0, close + 60, "tape")
        assert eng.grade_retests(close + 60) == 1 and eng.grade_retests(close + 120) == 0
        rec = retest_record(eng, close + 60)
        assert rec["flags"] == 1 and rec["close"] == {"n": 1, "hit": 1.0, "median": 1.5}
    finally:
        await eng.http.close()


async def test_second_chance_needs_a_strong_record_and_respects_the_phone(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    await eng.http.start()
    try:
        phones(eng, server)
        now = time.time()
        weak = contract_alert(eng)
        weak.edge["cat_base"]["close"]["hit"] = 0.5  # a coin flip: no second chance
        eng.storage.add_alert(weak)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        for t, px in ((0, 102.5), (60, 100.1)):
            eng.live_quotes.update("ACME", px, 99.0, now + t, "tape")
            assert await eng.check_retests(now + t) == 0
        eng.storage.db.execute("DELETE FROM alerts")
        eng._retest_best.clear()
        strong = contract_alert(eng)
        eng.desk.apply(DEV, "prefs", {"retest": False})
        for t, px in ((120, 102.5), (180, 100.1)):
            eng.live_quotes.update("ACME", px, 99.0, now + t, "tape")
            assert await eng.check_retests(now + t) == 0  # turned off
        eng.desk.apply(DEV, "prefs", {"retest": True})
        eng.desk.apply(DEV, "cat_mute", {"key": "contract"})
        assert await eng.check_retests(now + 200) == 0  # its kind of news is off on this phone
        eng.desk.apply(DEV, "cat_mute", {"key": "contract", "on": False})
        eng.calendar = SimpleNamespace(market_status=lambda t=None: {"phase": "closed"}, holidays=set())
        assert await eng.check_retests(now + 220) == 0  # only in the session
        market_open(eng)
        assert await eng.check_retests(now + 240) == 1
        assert eng.desk.get(DEV2)["watchlist"] == [] and strong.id
    finally:
        await eng.http.close()


async def test_run19_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    contract_alert(eng)
    contract_alert(eng, "Beta wins contract", sym="BETA")
    eng.live_quotes.update("ACME", 101.0, 99.0, time.time(), "tape")
    eng.live_quotes.update("BETA", 102.0, 99.0, time.time(), "tape")
    eng.storage.add_alert(graded("Acme wins contract number 1", 2.0))
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/working"), headers=h) as r:
                assert (await r.json())["rows"][0]["avg"] == 1.5
            async with s.get(srv.make_url("/api/record"), headers=h) as r:
                assert (await r.json())["retest"]["flags"] == 0
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "prefs", "retest": False}, headers=h
            ) as r:
                assert (await r.json())["prefs"]["retest"] is False
    finally:
        await srv.close()
        await eng.http.close()
