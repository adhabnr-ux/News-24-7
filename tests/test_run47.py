"""Run 47: filling the record from the exchange's daily bars (closes a sleeping server missed) and the
uptime ledger (when the desk slept through market hours, a push on waking, misses marked asleep)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247 import bars
from news247.recall import movers
from news247.uptime import BEAT_KEY, asleep_at, ledger, on_start, persist, restore, session_overlap, wake_push
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def et(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- daily bars


def test_reading_daily_bars():
    alp = {
        "bars": {
            "ACME": [{"t": "2026-10-13T04:00:00Z", "c": 104.0}, {"t": "2026-10-14T04:00:00Z", "c": 106.5}]
        }
    }
    assert bars.parse_alpaca(alp, "ACME") == {"2026-10-13": 104.0, "2026-10-14": 106.5}
    assert bars.parse_alpaca({}, "ACME") == {}
    yh = {"chart": {"result": [{"timestamp": [int(et(10, 13, 9, 30)), int(et(10, 14, 9, 30))],
                                "indicators": {"quote": [{"close": [104.0, None]}]}}]}}  # fmt: skip
    assert bars.parse_yahoo(yh) == {"2026-10-13": 104.0}
    assert bars.parse_yahoo({}) == {}


def asleep_alert(eng):
    a = news("Acme wins contract")
    a.created = et(10, 12, 10)  # Monday
    a.edge = {
        "refs": {"ACME": 100.0},
        "mkt_ref": 600.0,
        "outcome": {"ACME": {"h1": 1.0, "sign": 1}},
        "mkt_outcome": {"h1": 0.1},
    }
    eng.storage.add_alert(a)
    return a


def test_what_the_bars_can_fill(tmp_path):
    eng = semis_engine(tmp_path)
    a = asleep_alert(eng)
    need = bars.needs(eng, et(10, 21, 12))  # the next Wednesday: close, d1, d5 are past by a day
    j = need["ACME"][0]
    assert j["alert"]["id"] == a.id and j["ref"] == 100.0
    assert j["missing"] == {"close": "2026-10-12", "d1": "2026-10-13", "d5": "2026-10-19"}
    assert bars.needs(eng, et(10, 13, 12)) == {}  # Monday's close is less than a day ago


async def test_the_gaps_are_filled(tmp_path, monkeypatch):
    eng = semis_engine(tmp_path)
    a = asleep_alert(eng)
    book = {"ACME": {"2026-10-12": 102.0, "2026-10-13": 103.0, "2026-10-19": 99.0},
            "SPY": {"2026-10-12": 606.0, "2026-10-13": 603.0, "2026-10-19": 612.0}}  # fmt: skip

    async def fake(engine, sym, first, last):
        return {d: c for d, c in book[sym].items() if first <= d <= last}

    monkeypatch.setattr(bars, "fetch", fake)
    assert await bars.fill(eng, et(10, 21, 12)) == 3
    e = eng.storage.get_alert(a.id)["edge"]
    row = e["outcome"]["ACME"]
    assert (row["close"], row["d1"], row["d5"], row["h1"]) == (2.0, 3.0, -1.0, 1.0)
    assert row["filled"] == ["close", "d1", "d5"]
    assert e["mkt_outcome"] == {"h1": 0.1, "close": 1.0, "d1": 0.5, "d5": 2.0}
    assert await bars.fill(eng, et(10, 21, 12)) == 0  # nothing left to fill
    cov = {h["horizon"]: h for h in bars.coverage(eng, 30, et(10, 21, 12))["horizons"]}
    assert cov["close"] == {"horizon": "close", "due": 1, "graded": 1, "filled": 1, "share": 1.0}
    assert cov["d20"]["due"] == 0


async def test_alpaca_bars_are_asked_for(tmp_path):
    eng = semis_engine(tmp_path)
    eng.cfg.options.alpaca_key, eng.cfg.options.alpaca_secret = "k", "s"
    seen = {}

    class Resp:
        def json(self):
            return {"bars": {"ACME": [{"t": "2026-10-12T04:00:00Z", "c": 102.0}]}}

    async def get(url, **kw):
        seen.update(url=url, **kw)
        return Resp()

    eng.http.get = get
    assert await bars.fetch(eng, "ACME", "2026-10-12", "2026-10-19") == {"2026-10-12": 102.0}
    assert seen["url"].endswith("/v2/stocks/bars") and seen["params"]["timeframe"] == "1Day"
    assert seen["params"]["start"] == "2026-10-12" and seen["params"]["end"] == "2026-10-20"
    assert seen["headers"]["APCA-API-KEY-ID"] == "k"


# --------------------------------------------------------------------------- the uptime ledger


def test_market_hours_in_a_gap():
    assert session_overlap(et(10, 14, 9), et(10, 14, 10)) == 1800  # 9:30-10:00
    assert session_overlap(et(10, 16, 15), et(10, 19, 10)) == 3600 + 1800  # Fri 15:00-16:00 + Mon 9:30-10:00
    assert session_overlap(et(10, 17, 9), et(10, 18, 17)) == 0  # a weekend
    assert session_overlap(et(10, 14, 17), et(10, 15, 9)) == 0  # overnight


def test_an_outage_is_kept_on_start(tmp_path):
    eng = semis_engine(tmp_path)
    assert on_start(eng, et(10, 14, 9)) is None  # the first start: nothing to measure
    assert on_start(eng, et(10, 14, 9, 1)) is None  # a restart: under three minutes
    eng.storage.set_setting(BEAT_KEY, str(et(10, 14, 10)))
    out = on_start(eng, et(10, 14, 11, 30))
    assert out["seconds"] == 5400 and out["market_s"] == 5400
    assert asleep_at(eng, et(10, 14, 10, 30)) and not asleep_at(eng, et(10, 14, 12))
    led = ledger(eng, 20, et(10, 14, 15))
    assert led["asleep_h"] == 1.5 and led["outages"][0]["market_s"] == 5400 and 0.9 < led["awake"] < 1.0


async def test_the_push_on_waking(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        now = et(10, 14, 11, 30)
        eng.live_quotes.update("BETA", 47.0, 50.0, now - 30, "tape")  # -6%, no alert
        outage = {"from": et(10, 14, 10), "to": now, "seconds": 5400, "market_s": 5400}
        assert await wake_push(eng, {**outage, "market_s": 300}, now) == 0  # five minutes: not worth a push
        assert await wake_push(eng, outage, now) >= 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "😴 Foretape was asleep during the session"
        assert (
            msg["body"]
            == "Asleep Wed 10:00–11:30 ET: 90 min of the session went unread. Moving now with no alert: BETA -6.0%"
        )
    finally:
        await eng.http.close()


def test_a_miss_while_asleep_is_marked(tmp_path):
    eng = semis_engine(tmp_path)
    eng.storage.set_setting(
        "uptime:outages",
        json.dumps([{"from": et(10, 14, 9, 30), "to": et(10, 14, 12), "seconds": 9000, "market_s": 9000}]),
    )
    eng.live_quotes.update("DLTA", 10.0, 10.0, et(10, 14, 9, 40), "tape")
    eng.live_quotes.update("DLTA", 10.7, 10.0, et(10, 14, 10, 30), "tape")  # its high while asleep
    eng.live_quotes.update("DLTA", 10.6, 10.0, et(10, 14, 13), "tape")
    rows = {r["symbol"]: r for r in movers(eng, et(10, 14, 13, 1))}
    assert rows["DLTA"]["why"] == "asleep"


async def test_the_ledger_survives_a_fresh_disk(tmp_path):
    eng = semis_engine(tmp_path)

    class State:
        blob = None

        async def put(self, key, value):
            State.blob = value

        async def get(self, key):
            return State.blob

    eng.desk.state = State()
    eng.storage.set_setting(BEAT_KEY, str(et(10, 14, 10)))
    await persist(eng)
    fresh = semis_engine(tmp_path / "fresh")
    fresh.desk.state = State()
    await restore(fresh)
    assert fresh.storage.get_setting(BEAT_KEY) == str(et(10, 14, 10))
    assert on_start(fresh, et(10, 14, 11))["market_s"] == 3600


async def test_run47_over_the_api(tmp_path, monkeypatch):
    eng = semis_engine(tmp_path)

    async def fake(engine, sym, first, last):
        return {}

    monkeypatch.setattr(bars, "fetch", fake)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/uptime"), headers=h) as r:
                body = await r.json()
                assert "ledger" in body and [x["horizon"] for x in body["coverage"]["horizons"]] == [
                    "close",
                    "d1",
                    "d5",
                    "d20",
                ]
            async with s.post(srv.make_url("/api/bars/fill"), headers=h, json={}) as r:
                assert (await r.json())["filled"] == 0
    finally:
        await srv.close()
        await eng.http.close()


def test_stamps_dont_need_a_clock(tmp_path):
    eng = semis_engine(tmp_path)
    assert bars.needs(eng, time.time()) == {}
