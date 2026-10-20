"""Run 78: the playbook's circuit breaker (it stands down while its drawdown is deeper than any in its
record, re-arms on its shadow record, and grades its own trips) and the market's part of the playbook
(its track record split into what SPY gave over each call's own window and what's beyond it)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp.test_utils import TestServer

from news247.autopilot import playbook_pass
from news247.breaker import FLOOR, MIN_N, replay, send, state
from news247.dossier import record_outcomes
from news247.pbcurve import taken
from news247.pbmarket import market_part, split
from news247.pbmorning import morning, playbook_line
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run16 import contract_history
from .test_run26 import CLOSE, T0
from .test_run60 import ET, et, rec
from .test_run64 import pair

# --------------------------------------------------------------------------- the breaker's rules


def seq(xs):
    """Rows as they'd come: one alert every 10 seconds, each known 5 seconds after it went out."""
    return [{"id": f"a{i}", "created": i * 10.0, "known": i * 10.0 + 5, "pb": x} for i, x in enumerate(xs)]


def test_it_trips_on_a_record_drawdown_and_rearms_halfway():
    s = replay(seq([1.0] * 40 + [-1.0] * 6 + [1.0] * 4))
    assert s["on"] is None and len(s["trips"]) == 1
    t = s["trips"][0]
    # 40 winners, then six losers: -6 is past the -5 floor with nothing that deep before it
    assert t["dd"] == -6.0 and t["limit"] == -FLOOR and t["rearm"] == -2.5
    assert [a["id"] for a in t["sat"]] == ["a46", "a47", "a48", "a49"]  # it re-armed at -2 (a49's result)
    assert t["end"] == 495.0


def test_it_needs_a_record_first():
    s = replay(seq([1.0] * 10 + [-2.0] * 10))
    assert s["on"] is None and not s["trips"]  # -20, but only 20 alerts on the record: under 40
    assert MIN_N == 40


def test_only_deeper_than_its_record():
    first = [1.0] * 40 + [-1.0] * 6 + [1.0] * 10  # a -6 drawdown, then a new high
    again = first + [-1.0] * 6  # -6 again: as deep as before, not deeper
    assert len(replay(seq(again))["trips"]) == 1 and replay(seq(again))["on"] is None
    deeper = first + [-1.0] * 7  # -7: deeper than any before it
    assert replay(seq(deeper))["on"]["limit"] == -6.0


# --------------------------------------------------------------------------- on the engine


def weekdays_back(n, end):
    """``n`` weekday mornings (11:00 ET) up to ``end``'s, oldest first."""
    d = datetime.fromtimestamp(end, ET).replace(hour=11, minute=0)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.timestamp())
        d -= timedelta(days=1)
    return out[::-1]


def record(eng, closes, end):
    for i, (t, c) in enumerate(zip(weekdays_back(len(closes), end), closes, strict=True)):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = t
        a.edge = {
            **pair("alert", "close"),
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "close": c}},
        }
        eng.storage.add_alert(a)


def test_the_state_on_the_record(tmp_path):
    eng = semis_engine(tmp_path)
    now = et(14, 18)
    record(eng, [1.0] * 40 + [-1.5] * 6, now)
    st = state(eng, now=now)
    # tripped at -6 (the fourth loser); the last two went out while it stood down
    assert st["on"] and st["dd"] == -9.0 and st["limit"] == -5.0 and st["rearm"] == -2.5
    assert st["sat"] == 2 and st["made"] == -3.0
    assert (
        st["why"]
        == "it's standing down: its drawdown (-9.0%) is past -5% with none that deep in its record; it re-arms at -2.5%"
    )
    assert (
        st["line"].startswith("⛔ The playbook is standing down")
        and "would have made -3.0% (it saved that)" in st["line"]
    )
    calm = semis_engine(tmp_path / "b")
    record(calm, [1.0] * 45, now)
    st2 = state(calm, now=now)
    assert (
        not st2["on"]
        and st2["line"] == "The breaker is armed: it trips below -5.0% (the playbook's drawdown now +0.0%)."
    )


def test_the_shadow_record_and_the_pass(tmp_path):
    a = {"created": 1.0, "edge": {**pair("alert", "close"), "refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 2.0}},
                                  "breaker": {"why": "it's standing down: x", "since": 1.0, "dd": -9.0}}}  # fmt: skip
    assert playbook_pass(a) == "it's standing down: x"
    assert taken(a) == (0.0, 2.0)  # the track record: it sat out
    assert taken(a, strip=("breaker",)) == (2.0, 2.0)  # the breaker's shadow: as if taken
    assert taken(a, counterfactual=True) == (2.0, 2.0)  # the blind-spot judge sees through it too
    assert playbook_line(a["edge"]) == "🚫 The playbook passes: it's standing down: x"


async def test_publish_stamps_it_and_the_autopilot_stands_down(tmp_path):
    eng = semis_engine(tmp_path)
    from .test_run63 import history as pairs

    pairs(eng)
    eng.refresh_base_rates()
    eng.breaker_state = {"on": True, "why": "it's standing down: x", "since": 1.0, "dd": -9.0}
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    got = eng.storage.get_alert(a.id)
    assert (
        got["edge"]["breaker"]["why"] == "it's standing down: x"
        and playbook_pass(got) == "it's standing down: x"
    )

    eng2 = semis_engine(tmp_path / "b")
    contract_history(eng2)
    eng2.refresh_base_rates()
    eng2.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    eng2.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    b = news("Acme wins $2B Pentagon contract")
    await eng2.publish(b, push=False)
    eng2.storage.update_alert_edge(b.id, rec("alert", "d1"))
    eng2.breaker_state = {"on": True}  # tripped since it went out: the live guard holds
    assert await eng2.autopilot_open(eng2.storage.get_alert(b.id)) == 0
    eng2.breaker_state = {"on": False}
    assert await eng2.autopilot_open(eng2.storage.get_alert(b.id)) == 1


async def test_one_push_when_it_trips_and_one_when_it_rearms(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        ph, ph2 = phones(eng, server)
        eng.breaker_state = {"n": 60, "on": False, "dd": -1.0, "trips": []}
        assert await send(eng) == 0  # the first reading, armed: nothing happened
        eng.breaker_state = {
            "n": 60,
            "on": True,
            "dd": -9.0,
            "prior": 0.0,
            "limit": -5.0,
            "rearm": -2.5,
            "trips": [],
        }
        assert await send(eng) == 2
        msg = ph.read(next(r for r in server.requests if r["path"] == "/push/a")["body"])
        assert msg["title"] == "⛔ The playbook stands down (-9.0%)"
        assert "It sits new alerts out (the autopilot too) until it's back to -2.5%" in msg["body"]
        assert await send(eng) == 0  # once
        eng.desk.apply(DEV, "prefs", {"pb_push": False})
        eng.breaker_state = {"n": 60, "on": False, "dd": -2.0, "trips": [{"n": 4, "made": 1.5}]}
        assert await send(eng) == 1  # the other phone; this one switched the playbook's moves off
        back = ph2.read([r for r in server.requests if r["path"] == "/push/b"][-1]["body"])
        assert back["title"] == "✅ The playbook is back"
        assert (
            back["body"]
            == "Its drawdown recovered to -2.0%; it takes new alerts again. The 4 it sat out would have made +1.5%."
        )
    finally:
        await eng.http.close()


def test_the_morning_leads_with_it(tmp_path):
    eng = semis_engine(tmp_path)
    eng.breaker_state = {"on": True, "dd": -9.0, "prior": -6.2, "limit": -6.2, "rearm": -3.1}
    m = morning(eng, et(14, 8, 15))
    assert (
        m["lines"][0]
        == "Standing down: its drawdown (-9.0%) is deeper than any in its record (-6.2%); it re-arms at -3.1%"
    )


# --------------------------------------------------------------------------- the market's part


def graded(edge, row, mkt, **extra):
    return {"created": 1.0, "tickers": ["ACME"], "edge": {"refs": {"ACME": 100.0}, "mkt_ref": 600.0, "outcome": {"ACME": row},
                                                           "mkt_outcome": mkt, **edge, **extra}}  # fmt: skip


def test_the_split_per_call():
    a = graded(pair("alert", "d1"), {"sign": 1, "close": 2.0, "d1": 3.0}, {"close": 1.0, "d1": 1.5})
    assert split(a) == {"pb": 3.0, "pb_mkt": 1.5, "plain": 2.0, "plain_mkt": 1.0}
    short = graded(pair("alert", "close"), {"sign": -1, "close": -2.0}, {"close": 1.0})
    assert split(short) == {
        "pb": 2.0,
        "pb_mkt": -1.0,
        "plain": 2.0,
        "plain_mkt": -1.0,
    }  # a short loses the rise
    at_open = graded(
        pair("open", "close"), {"sign": 1, "open": 1.0, "close": 3.0}, {"open": 0.5, "close": 1.0}
    )
    assert (
        split(at_open)["pb"] == 1.98 and split(at_open)["pb_mkt"] == 0.5
    )  # from the first print, both sides
    unread = graded(pair("open", "close"), {"sign": 1, "open": 1.0, "close": 3.0}, {"close": 1.0})
    assert split(unread)["pb_mkt"] is None  # the market at the open wasn't read: never guessed
    leg2 = graded(
        pair("leg2", "close"), {"sign": 1, "close": 5.0}, {"close": 1.0}, leg2={"px": 102.0, "mkt": 603.0}
    )
    assert split(leg2)["pb"] == 2.94 and split(leg2)["pb_mkt"] == 0.5
    halves = graded(
        pair("alert", "h1+d1"),
        {"sign": 1, "h1": 1.0, "close": 1.0, "d1": 2.0},
        {"h1": 0.2, "close": 0.5, "d1": 1.0},
    )
    assert split(halves)["pb_mkt"] == 0.6
    passed = graded(
        {**pair("alert", "close"), "liq": {"pass": True, "why": "thin"}},
        {"sign": 1, "close": 2.0},
        {"close": 1.0},
    )
    assert split(passed) == {"pb": 0.0, "pb_mkt": 0.0, "plain": 2.0, "plain_mkt": 1.0}  # it held nothing
    assert split(graded({}, {"sign": 1, "close": 2.0}, {"close": 1.0})) is None  # not the playbook's


def test_the_playbook_split_and_its_verdict(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(10):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (20 - i)
        p = pair("alert", "close")
        p["combo_rec"]["kind"] = "contract"
        a.edge = {**p, "refs": {"ACME": 100.0}, "mkt_ref": 600.0, "outcome": {"ACME": {"sign": 1, "close": 2.0}},
                  "mkt_outcome": {"close": 1.5}}  # fmt: skip
        eng.storage.add_alert(a)
    r = market_part(eng)
    assert (
        r["playbook"]["total"] == 20.0 and r["playbook"]["market"] == 15.0 and r["playbook"]["beyond"] == 5.0
    )
    assert (
        r["verdict"] == "tide"
        and r["kinds"][0]["kind"] == "contract"
        and r["kinds"][0]["label"] == "Contract win"
    )
    assert r["line"] == (
        "On 10 alerts with a market reading, the playbook made +20.0%: the market's part +15.0%, beyond it +5.0% "
        "(+0.50% an alert; 100% beat the market). The plain way: +20.0%, the market's part +15.0%, beyond it +5.0%. "
        "Most of it was the market: the playbook rode the tide more than it read the news."
    )


def test_the_market_is_read_at_the_later_ways_in(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract 15")
    a.created = T0
    a.edge = {"refs": {"ACME": 100.0}, "mkt_ref": 600.0}
    eng.storage.add_alert(a)
    eng.live_quotes.update("SPY", 601.2, 600.0, T0 + 900, "tape")
    record_outcomes(eng, T0 + 905)
    assert eng.storage.get_alert(a.id)["edge"]["mkt_outcome"]["m15"] == 0.2
    eng = semis_engine(tmp_path / "b")  # its own quotes: earlier in the day than the one above
    b = news("Acme wins contract overnight")
    b.created = CLOSE - 10 * 3600  # 06:00 ET: before the open
    b.edge = {"refs": {"ACME": 100.0}, "mkt_ref": 600.0}
    eng.storage.add_alert(b)
    op = CLOSE - 6.5 * 3600
    eng.live_quotes.update("SPY", 597.0, 600.0, op + 30, "tape")
    record_outcomes(eng, op + 60)
    assert eng.storage.get_alert(b.id)["edge"]["mkt_outcome"]["open"] == -0.5


# --------------------------------------------------------------------------- the endpoints


async def test_the_endpoints(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    record(eng, [1.0] * 40 + [-1.5] * 6, now - 86400 * 2)
    eng.breaker_state = state(eng, now=now)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            h = {"Authorization": "Bearer tok"}
            async with s.get(srv.make_url("/api/breaker"), headers=h) as r:
                body = await r.json()
                assert body["on"] and body["need"] == MIN_N
            async with s.get(srv.make_url("/api/playbook-curve"), headers=h) as r:
                body = await r.json()
                assert (
                    body["breaker"]["on"] and body["market"]["unread"] == 46
                )  # no market readings: counted apart
                assert body["market"]["line"] == "" and body["market"]["verdict"] == ""
            async with s.get(srv.make_url("/api/playbook-market"), headers=h) as r:
                assert "playbook" in await r.json()
            async with s.get(srv.make_url("/api/playbook-today"), headers=h) as r:
                assert (await r.json())["breaker"]["on"]
    finally:
        await srv.close()
        await eng.http.close()
