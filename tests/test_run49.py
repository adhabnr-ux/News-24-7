"""Run 49: the wait is over (a push when the call's wait ends, at the open or at the patient entry, and
whether waiting paid) and the intraday record filled from one-minute bars (the news clock, the hour, the
first print, heat, the market's hour, each peer's hour and close)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247 import intrafill
from news247.intrafill import first_print, heat, parse_alpaca, parse_yahoo, price_at
from news247.waits import graded, open_due, pullback_due, send_waits, wait_line, wait_rates
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def et(day, hour, minute=0, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=ET).timestamp()


def waited(eng, title, wait_for, created, row, *, direction="up", **edge):
    a = news(title, direction=direction)
    a.created = created
    a.edge = {"call": {"verb": "wait", "label": "Wait", "wait_for": wait_for}, "refs": {"ACME": 100.0},
              "outcome": {"ACME": row}, **edge}  # fmt: skip
    eng.storage.add_alert(a)
    return a


# --------------------------------------------------------------------------- did waiting pay


def test_a_wait_is_graded_against_acting():
    def a(wait_for, row, **edge):
        return {"edge": {"call": {"verb": "wait", "wait_for": wait_for}, "outcome": {"ACME": row}, **edge}}

    # waited for the open: it gapped against the news, then went its way
    g = graded(a("open", {"sign": 1, "open": -1.0, "close": 2.0}))
    assert g == {"for": "open", "filled": True, "act": 2.0, "wait": 3.03, "gain": 1.03}
    # waited for a pullback to 1% under the alert price: the dip reached it
    pt = {"plan": {"patient": {"limit": 99.0, "dip": 1.0}}}
    g = graded(a("pullback", {"sign": 1, "close": 2.0}, exc={"mae": -1.5}, **pt))
    assert g == {"for": "pullback", "filled": True, "act": 2.0, "wait": 3.03, "gain": 1.03}
    # it never came back: the wait missed a 3% move
    g = graded(a("pullback", {"sign": 1, "close": 3.0}, exc={"mae": -0.5}, **pt))
    assert g == {"for": "pullback", "filled": False, "act": 3.0, "wait": 0.0, "gain": -3.0}
    # a short waits for a bounce 1% over: filled, and it fell 2% from the alert
    g = graded(a("pullback", {"sign": -1, "close": -2.0}, exc={"mae": -2.0}, **pt))
    assert g["filled"] and g["act"] == 2.0 and g["wait"] == 2.97
    assert graded(a("open", {"sign": 1, "close": 2.0})) is None  # no first print on record
    assert graded({"edge": {"call": {"verb": "act"}, "outcome": {"ACME": {"sign": 1, "close": 1.0}}}}) is None


def test_the_wait_record(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for i in range(6):  # the gap went the wrong way first: waiting paid
        waited(eng, f"Acme news {i}", "open", now - 86400 * (i + 2), {"sign": 1, "open": -1.0, "close": 2.0})
    for i in range(2):  # the gap faded: waiting still beat acting at the alert
        waited(eng, f"Acme fades {i}", "open", now - 86400 * (i + 9), {"sign": 1, "open": 2.0, "close": 1.0})
    pt = {"plan": {"patient": {"limit": 99.0, "dip": 1.0}}}
    waited(eng, "Acme dips", "pullback", now - 86400 * 3, {"sign": 1, "close": 2.0}, exc={"mae": -1.5}, **pt)
    waited(eng, "Acme runs", "pullback", now - 86400 * 4, {"sign": 1, "close": 3.0}, exc={"mae": -0.5}, **pt)
    r = wait_rates(eng, 30, now)
    o = r["open"]
    assert (o["n"], o["filled"], o["paid"], o["verdict"]) == (8, 8, 0.75, "paid")
    assert o["gain"] == pytest.approx((6 * 1.03 + 2 * -1.98) / 8, abs=0.01)
    assert o["wait_hit"] == 0.75 and o["act_hit"] == 1.0
    assert wait_line(o).startswith(
        "Waiting for the open beat acting at the alert 75% of the time here (+0.3% on average, 8)"
    )
    p = r["pullback"]
    assert (p["n"], p["filled"], p["missed"], p["missed_act"], p["verdict"]) == (2, 1, 1, 3.0, "thin")
    assert wait_line(p) == ""  # too few to say


# --------------------------------------------------------------------------- the pushes


async def test_the_wait_for_the_open_ends(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    eng.open_rates = {"*|mid": {"key": "*|mid", "kind": "*", "gap": "mid", "n": 10, "label": "News that gapped 2-5% its way",
                                "gap_median": 3.0, "after": {"n": 10, "hit": 0.3, "median": -0.8}}}  # fmt: skip
    a = waited(eng, "Acme wins $2B Pentagon contract", "open", et(14, 8), {"sign": 1, "open": 2.5})
    waited(eng, "Acme old news", "open", et(13, 8), {"sign": 1})  # yesterday's: its wait ended yesterday
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        eng.live_quotes.update("ACME", 101.0, 99.0, et(14, 9, 38), "tape")
        assert open_due(eng, et(14, 9, 33)) == []  # before 9:35 the first print may not be on the alert yet
        rows = open_due(eng, et(14, 9, 40))
        assert [r["alert"]["id"] for r in rows] == [a.id] and rows[0]["gap"] == 2.5 and rows[0]["its"] == 1.0
        assert rows[0]["read"] == "the record from here still says no rush"
        assert await send_waits(eng, et(14, 9, 40)) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "⏰ ACME at the open: +1.0% from the alert"
        assert (
            "The call said wait for the open. ACME opened +2.5% its way, now +1.0% from the alert price."
            in msg["body"]
        )
        assert (
            "News that gapped 2-5% its way: kept going after the open 30% (median -0.8% open to close, 10)"
            in msg["body"]
        )
        assert "Now: the record from here still says no rush." in msg["body"]
        assert eng.storage.get_alert(a.id)["edge"]["wait"]["for"] == "open"
        assert await send_waits(eng, et(14, 9, 41)) == 0  # once
        assert open_due(eng, et(14, 10, 1)) == []  # the window is the first half hour
    finally:
        await eng.http.close()


async def test_the_wait_for_a_pullback_ends(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    eng.wait_rates = {
        "pullback": {"for": "pullback", "n": 12, "filled": 9, "paid": 0.67, "gain": 0.6, "missed": 3}
    }
    pt = {"plan": {"patient": {"limit": 99.0, "dip": 1.0, "p_now": 0.58, "p_filled": 0.52}}}
    a = waited(eng, "Acme wins $2B Pentagon contract", "pullback", et(14, 10), {"sign": 1}, **pt)
    await eng.http.start()
    try:
        ph, other = phones(eng, server)
        eng.webpush.note(DEV, {"id": a.id, "title": "x"}, "sent")  # the alert reached this phone
        eng.live_quotes.update("ACME", 99.6, 99.0, et(14, 10, 50), "tape")
        assert pullback_due(eng, et(14, 10, 51)) == []  # not back to the level yet
        eng.live_quotes.update("ACME", 98.95, 99.0, et(14, 10, 59), "tape")
        assert [r["alert"]["id"] for r in pullback_due(eng, et(14, 11))] == [a.id]
        assert await send_waits(eng, et(14, 11)) == 1  # only the phone that got the alert
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "⏸ ACME back to the patient entry: $99.00"
        assert (
            "ACME is back to $99.00, the patient entry (1.0% from the alert, the winners' median dip)."
            in msg["body"]
        )
        assert "Filled there, the odds read 52% (from 58% at the alert): losers dip too." in msg["body"]
        assert (
            "Waiting for the pullback beat acting at the alert 67% of the time here (+0.6% on average, 12); 3 never came back to the level."
            in msg["body"]
        )
        assert eng.storage.get_alert(a.id)["edge"]["wait"] == {
            "for": "pullback",
            "ts": et(14, 11),
            "px": 98.95,
            "limit": 99.0,
        }
        assert pullback_due(eng, et(14, 11, 5)) == []  # its wait has ended
        assert pullback_due(eng, et(14, 16, 5)) == []  # and none after the close
    finally:
        await eng.http.close()


def test_weekend_news_waits_for_monday(tmp_path):
    from news247.waits import prev_close

    assert prev_close(et(19, 9, 30), set()) == et(16, 16)  # Monday's open: Friday's close
    assert prev_close(et(14, 9, 30), {"2026-10-13"}) == et(12, 16)  # past a holiday
    eng = semis_engine(tmp_path)
    eng.live_quotes.update("ACME", 101.0, 99.0, et(19, 9, 38), "tape")
    a = waited(
        eng, "Acme wins $2B Pentagon contract", "open", et(17, 11), {"sign": 1, "open": 1.0}
    )  # Saturday
    assert [r["alert"]["id"] for r in open_due(eng, et(19, 9, 40))] == [a.id]


def test_a_phone_can_turn_waits_off(tmp_path):
    from news247.waits import _devices

    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    a = {"id": "x1"}
    assert _devices(eng, a, "ACME") == {DEV}
    eng.desk.apply(DEV, "prefs", {"waits": False})
    assert _devices(eng, a, "ACME") == set()


def test_plans_carry_the_patient_entry_on_the_alert(tmp_path):
    import asyncio

    from .test_run16 import contract_history
    from .test_run33 import heat_with_losers

    eng = semis_engine(tmp_path)
    contract_history(eng)
    heat_with_losers(eng)
    eng.refresh_base_rates()
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    asyncio.run(eng.publish(a, push=False))
    pt = a.edge["plan"]["patient"]
    assert pt["limit"] == pytest.approx(
        100.5 * (1 - 0.007), abs=0.001
    )  # the winners' median dip from the live price
    assert set(pt) == {"limit", "dip", "p_now", "p_filled"} and pt["p_filled"] < pt["p_now"]


# --------------------------------------------------------------------------- minute bars


def bars_for(base, start, n, drift=0.0, dip_at=None, dip=0.0):
    """n one-minute bars from ``start``: the price walks ``drift`` a minute; one bar dips."""
    out = []
    for i in range(n):
        px = round(base + drift * i, 4)
        lo = px - dip if dip_at == i else px - 0.01
        out.append((start + 60 * i, px, px + 0.02, lo, px))
    return out


def test_reading_minute_bars():
    doc = {"bars": {"ACME": [{"t": "2026-10-14T14:00:00Z", "o": 100, "h": 101, "l": 99.5, "c": 100.5}]}}
    assert parse_alpaca(doc, "ACME") == [(et(14, 10), 100.0, 101.0, 99.5, 100.5)]
    yh = {"chart": {"result": [{"timestamp": [int(et(14, 10)), int(et(14, 10, 1))],
                                "indicators": {"quote": [{"open": [100, None], "high": [101, None], "low": [99, None], "close": [100.5, None]}]}}]}}  # fmt: skip
    assert parse_yahoo(yh) == [(et(14, 10), 100.0, 101.0, 99.0, 100.5)]
    assert parse_yahoo({}) == [] and parse_alpaca({}, "ACME") == []
    bars = bars_for(100.0, et(14, 10), 120, drift=0.01)
    assert price_at(bars, et(14, 10, 5)) == pytest.approx(100.04)  # the bar ending 10:05
    assert price_at(bars, et(14, 13)) is None  # no bar near it
    assert first_print(bars_for(50.0, et(14, 9, 30), 10), et(14, 9, 30)) == 50.0
    assert first_print(bars, et(14, 9, 30)) is None
    h = heat(bars_for(100.0, et(14, 10), 60, dip_at=30, dip=1.5), 100.0, 1, et(14, 10), et(14, 16))
    assert h == {"mfe": 0.02, "mae": -1.5}
    short = heat(bars_for(100.0, et(14, 10), 60, dip_at=30, dip=1.5), 100.0, -1, et(14, 10), et(14, 16))
    assert short == {"mfe": 1.5, "mae": -0.02}


def asleep_day(eng):
    """An alert at 10:00 and one before the bell, on a day the server slept: no intraday stamps."""
    a = news("Acme wins $2B Pentagon contract")
    a.created = et(14, 10)
    a.edge = {"refs": {"ACME": 100.0}, "mkt_ref": 600.0, "peer_refs": {"BETA": 50.0},
              "outcome": {"ACME": {"close": 2.0, "sign": 1}}}  # fmt: skip
    eng.storage.add_alert(a)
    b = news("Acme guides higher")
    b.created = et(14, 8)
    b.edge = {"refs": {"ACME": 99.0}, "outcome": {"ACME": {"close": 2.0, "sign": 1}}}
    eng.storage.add_alert(b)
    return a, b


async def test_the_minutes_are_filled(tmp_path, monkeypatch):
    eng = semis_engine(tmp_path)
    a, b = asleep_day(eng)
    book = {"ACME": bars_for(98.0, et(14, 4), 16 * 60, drift=0.005, dip_at=360 + 20, dip=1.0),
            "SPY": bars_for(600.0, et(14, 4), 16 * 60, drift=0.01),
            "BETA": bars_for(50.0, et(14, 4), 16 * 60, drift=0.001)}  # fmt: skip
    seen = []

    async def fake(engine, sym, start, end):
        seen.append(sym)
        return [x for x in book[sym] if start <= x[0] <= end]

    monkeypatch.setattr(intrafill, "fetch", fake)
    now = et(15, 12)
    assert {t["alert"]["id"] for t in intrafill.needs(eng, now)} == {a.id, b.id}
    assert intrafill.needs(eng, et(14, 16, 10)) == []  # the session's live stamps get their chance first
    n = await intrafill.fill(eng, now)
    assert sorted(set(seen)) == ["ACME", "BETA", "SPY"] and seen.count("ACME") == 1  # one request per symbol
    e = eng.storage.get_alert(a.id)["edge"]
    row = e["outcome"]["ACME"]
    px = lambda t: 98.0 + 0.005 * ((t - et(14, 4)) / 60 - 1)  # noqa: E731 - the close of the bar ending at t
    assert row["m5"] == pytest.approx(px(et(14, 10, 5)) - 100, abs=0.01)
    assert row["h1"] == pytest.approx(px(et(14, 11)) - 100, abs=0.01)
    assert row["filled"] == ["h1", "m15", "m30", "m5"] and "open" not in row  # news inside the session
    assert e["exc"]["filled"] and e["exc"]["mae"] < -0.8  # the dip at 10:20 is in its heat
    assert e["mkt_outcome"]["h1"] == pytest.approx((600 + 0.01 * 419) / 600 * 100 - 100, abs=0.01)
    assert set(e["peer_out"]["BETA"]) == {"h1", "close", "filled"}
    eb = eng.storage.get_alert(b.id)["edge"]
    assert eb["outcome"]["ACME"]["open"] == pytest.approx((98.0 + 0.005 * 330) / 99.0 * 100 - 100, abs=0.01)
    assert n >= 10
    assert await intrafill.fill(eng, now) == 0  # nothing left


async def test_bars_that_cant_be_had_are_tried_twice(tmp_path, monkeypatch):
    eng = semis_engine(tmp_path)
    asleep_day(eng)
    calls = []

    async def empty(engine, sym, start, end):
        calls.append(sym)
        return []

    monkeypatch.setattr(intrafill, "fetch", empty)
    now = et(15, 12)
    assert await intrafill.fill(eng, now) == 0
    assert await intrafill.fill(eng, now) == 0
    before = len(calls)
    assert await intrafill.fill(eng, now) == 0 and len(calls) == before  # left alone after two tries
    assert intrafill.needs(eng, now) == []


async def test_alpaca_minute_bars_are_asked_for(tmp_path):
    eng = semis_engine(tmp_path)
    eng.cfg.options.alpaca_key, eng.cfg.options.alpaca_secret = "k", "s"
    seen = {}

    class Resp:
        def json(self):
            return {"bars": {"ACME": [{"t": "2026-10-14T14:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1}]}}

    async def get(url, **kw):
        seen.update(url=url, **kw)
        return Resp()

    eng.http.get = get
    assert len(await intrafill.fetch(eng, "ACME", et(14, 9), et(14, 17))) == 1
    p = seen["params"]
    assert (
        p["timeframe"] == "1Min"
        and p["start"] == "2026-10-14T13:00:00Z"
        and p["end"] == "2026-10-14T21:00:00Z"
    )


def test_coverage_counts_the_minutes(tmp_path):
    eng = semis_engine(tmp_path)
    a, b = asleep_day(eng)
    eng.storage.update_alert_edge(a.id, {"outcome": {"ACME": {"close": 2.0, "sign": 1, "h1": 1.0, "filled": ["h1"]}},
                                         "exc": {"mfe": 2.5, "mae": -0.5, "filled": True}})  # fmt: skip
    cov = {h["horizon"]: h for h in intrafill.coverage(eng, 30, et(15, 12))}
    assert cov["h1"] == {"horizon": "h1", "due": 2, "graded": 1, "filled": 1, "share": 0.5}
    assert cov["open"]["due"] == 1 and cov["heat"] == {
        "horizon": "heat",
        "due": 2,
        "graded": 1,
        "filled": 1,
        "share": 0.5,
    }


async def test_run49_over_the_api(tmp_path, monkeypatch):
    from news247 import bars

    eng = semis_engine(tmp_path)

    async def nothing(*a, **k):
        return {}

    async def none(*a, **k):
        return []

    monkeypatch.setattr(bars, "fetch", nothing)
    monkeypatch.setattr(intrafill, "fetch", none)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/call"), headers=h) as r:
                assert "waits" in await r.json()
            async with s.get(srv.make_url("/api/uptime"), headers=h) as r:
                hz = [x["horizon"] for x in (await r.json())["coverage"]["horizons"]]
                assert hz == ["h1", "m15", "open", "heat", "close", "d1", "d5", "d20"]
            async with s.post(srv.make_url("/api/bars/fill"), headers=h, json={}) as r:
                body = await r.json()
                assert (body["filled"], body["daily"], body["minutes"]) == (0, 0, 0)
    finally:
        await srv.close()
        await eng.http.close()
