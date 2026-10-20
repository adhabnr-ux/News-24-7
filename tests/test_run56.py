"""Run 56: the run-up into FDA decisions, readouts and panels (graded from daily bars two days after,
pushed ten sessions before an event on your stock) and paper autopilots that follow the live signals
(second legs, the call turning to act)."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247 import bars, runup
from news247.autopilot import clean_rule, matches, signal_matches
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run16 import contract_history

ET = ZoneInfo("America/New_York")


def sessions(first: date, n: int) -> list[str]:
    out, d = [], first
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- into the event


def test_the_path_into_an_event():
    days = sessions(date(2026, 9, 1), 16)
    closes = {d: 10.0 + i * 0.1 for i, d in enumerate(days)}  # a steady climb
    ev = days[12]
    closes[ev] = 9.0  # the event: a drop
    p = runup.path(closes, ev)
    assert p == {"run": round((11.1 / 10.1 - 1) * 100, 2), "day": round((9.0 / 11.1 - 1) * 100, 2),
                 "hold": round((9.0 / 10.1 - 1) * 100, 2), "extra": round((9.0 / 10.1 - 1) * 100 - (11.1 / 10.1 - 1) * 100, 2)}  # fmt: skip
    assert runup.path(closes, days[5]) is None  # not eleven sessions of bars before it


async def test_events_are_graded_from_daily_bars(tmp_path, monkeypatch):
    eng = semis_engine(tmp_path)
    days = sessions(date(2026, 9, 1), 16)
    for i in range(6):
        eng.storage.add_catalyst({"id": f"ACME:{days[12]}:pdufa:{i}", "date": days[12], "kind": "binary", "event": "pdufa",
                                  "symbol": "ACME", "title": "ACME · FDA decision"})  # fmt: skip
    eng.storage.add_catalyst(
        {"id": "ZZZ:x", "date": days[12], "kind": "binary", "event": "pdufa", "symbol": "ZZZ", "title": "x"}
    )
    seen = []

    async def fake(engine, sym, first, last):
        seen.append(sym)
        if sym == "ZZZ":
            return {}
        return {d: (10.0 + i * 0.2 if d != days[12] else 10.5) for i, d in enumerate(days)}

    monkeypatch.setattr(bars, "fetch", fake)
    now = datetime(2026, 10, 1, 12, tzinfo=ET).timestamp()
    assert await runup.grade(eng, now) == 6
    assert await runup.grade(eng, now) == 0 and await runup.grade(eng, now) == 0  # done; ZZZ tried twice
    assert seen.count("ZZZ") == 2
    r = runup.runup_rates(eng)
    g = r["pdufa|*"]
    assert g["label"] == "Into FDA decisions, stocks" and g["n"] == 6 and g["run"]["up"] == 1.0
    assert r["pdufa|large"]["label"] == "Into FDA decisions, larger companies"  # ACME is $5B
    assert runup.runup_for(r, "readout", "small") is r["*|*"]
    assert runup.runup_line(g).startswith(
        "Into FDA decisions, stocks: rose 100% of the time in the 10 sessions before"
    )


async def test_ten_sessions_out(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    eng.runup_rates = {"*|*": {"key": "*|*", "event": "*", "size": "*", "label": "Into events, stocks", "n": 9,
                               "run": {"n": 9, "up": 0.67, "median": 6.2}, "on": {"n": 9, "up": 0.4, "median": -3.0},
                               "extra": {"n": 9, "up": 0.33, "median": -2.5}}}  # fmt: skip
    now = datetime(2026, 10, 14, 9, 40, tzinfo=ET).timestamp()  # a Wednesday
    day10 = sessions(date(2026, 10, 15), 10)[-1]  # ten sessions out
    eng.storage.add_catalyst({"id": f"ACME:{day10}:pdufa", "date": day10, "kind": "binary", "event": "pdufa",
                              "symbol": "ACME", "title": "ACME · FDA decision on Acmezumab"})  # fmt: skip
    assert runup.sessions_until(day10, now, set()) == 10
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        assert await runup.send_runups(eng, now) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "📅 ACME: 10 sessions to its FDA decision"
        assert msg["body"].endswith("Into events, stocks: rose 67% of the time in the 10 sessions before (median +6.2%); "
                                    "holding through the event added -2.5% (median, 9).")  # fmt: skip
        assert await runup.send_runups(eng, now + 60) == 0  # once
        assert await runup.send_runups(eng, now + 3 * 3600) == 0  # the window is the morning
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- autopilots for the live signals


def test_rules_that_follow_a_signal():
    leg = clean_rule({"follow": "leg2", "min_sev": "MEDIUM", "exit": "close"}, 0)
    assert leg["label"] == "Following second legs (MEDIUM+): in at the breakout; out at the close"
    turn = clean_rule({"follow": "turn", "exit": "h1"}, 0)
    assert turn["label"].startswith("Following the call when it turns to act (HIGH+)")
    with pytest.raises(ValueError):
        clean_rule({"follow": "leg2", "catalyst": "contract"}, 0)
    with pytest.raises(ValueError):
        clean_rule({"follow": "moon"}, 0)
    a = news("Acme wins $2B Pentagon contract").to_dict()
    assert not matches(leg, a)  # not when the alert goes out: at its signal
    assert signal_matches(leg, a, "leg2") and not signal_matches(leg, a, "turn")
    assert not signal_matches(turn, {**a, "severity": "MEDIUM"}, "turn")  # under the rule's severity


async def test_a_second_leg_opens_a_paper_call(tmp_path):
    from news247 import secondleg

    eng = semis_engine(tmp_path)
    contract_history(eng)  # a record to plan from
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "autopilot_add", {"follow": "leg2", "min_sev": "MEDIUM", "exit": "close"})
    t0 = datetime(2026, 10, 14, 10, tzinfo=ET).timestamp()
    a = news("Acme wins $2B Pentagon contract")
    a.created = t0
    a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1}}}
    eng.storage.add_alert(a)
    eng.live_quotes.update("ACME", 101.8, 99.0, t0 + 1200, "tape")
    eng.live_quotes.update("ACME", 101.0, 99.0, t0 + 3900, "tape")
    await secondleg.send_legs(eng, t0 + 3900)
    eng.live_quotes.update("ACME", 102.2, 99.0, t0 + 6000, "tape")  # +2.2%: room left to its target
    await secondleg.send_legs(eng, t0 + 6000)
    tr = eng.desk.get(DEV)["tracked"]
    assert len(tr) == 1 and tr[0]["symbol"] == "ACME" and tr[0]["direction"] == "long" and tr[0].get("auto")


async def test_a_turn_to_act_opens_a_paper_call(tmp_path):
    from news247 import callnow

    from .test_run55 import heavy, watched

    eng = semis_engine(tmp_path)
    contract_history(eng)  # a record to plan from
    eng.refresh_base_rates()
    heavy(eng)
    eng.desk.apply(DEV, "autopilot_add", {"follow": "turn", "min_sev": "MEDIUM", "exit": "close"})
    eng.live_quotes.update("ACME", 102.0, 99.0, time.time() - 5, "tape")
    watched(eng, 7.5)
    await callnow.send_turns(eng)
    tr = eng.desk.get(DEV)["tracked"]
    assert len(tr) == 1 and "turns to act" in tr[0]["note"]


async def test_run56_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s, s.get(srv.make_url("/api/runups"), headers=h) as r:
            assert (await r.json())["sessions"] == 10
    finally:
        await srv.close()
        await eng.http.close()


def test_a_signal_rule_isnt_a_duplicate_of_a_plain_one(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "autopilot_add", {"min_sev": "MEDIUM", "exit": "close"})
    eng.desk.apply(DEV, "autopilot_add", {"follow": "leg2", "min_sev": "MEDIUM", "exit": "close"})
    eng.desk.apply(DEV, "autopilot_add", {"follow": "turn", "min_sev": "MEDIUM", "exit": "close"})
    assert [r.get("follow", "") for r in eng.desk.get(DEV)["autopilots"]] == ["", "leg2", "turn"]
    with pytest.raises(ValueError):
        eng.desk.apply(DEV, "autopilot_add", {"follow": "leg2", "min_sev": "MEDIUM", "exit": "close"})
