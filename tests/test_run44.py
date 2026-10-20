"""Run 44: follow the call on paper (an autopilot that acts on "act now" at once and enters "wait for
the open" at the open) and moving the alert line on the evidence (applied live, logged, reversible,
kept across restarts)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.autopilot import clean_rule, matches, rule_label, waits_for_open
from news247.engine import Engine
from news247.shadow import apply_line, line_report, set_line
from news247.thecall import the_call
from news247.web.server import WebServer

from .test_desk import DEV
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run16 import contract_history, stored

ET = ZoneInfo("America/New_York")


def et(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- follow the call


def test_what_the_call_waits_for():
    gap = the_call({"odds": {"p": 0.62}, "open_base": {"after": {"n": 22, "hit": 0.36}}}, outside=True)
    assert gap["verb"] == "wait" and gap["wait_for"] == "open"
    dip = the_call(
        {
            "odds": {"p": 0.62},
            "persona": {"symbol": "ACME", "trait": "fades_day", "label": "Fades by the close"},
        }
    )
    assert dip["wait_for"] == "pullback"
    assert "wait_for" not in the_call({"odds": {"p": 0.66}, "clock": {"half": "m5"}})


def test_a_rule_that_follows_the_call():
    rule = clean_rule({"follow": "call", "min_sev": "HIGH", "exit": "close"}, 0)
    assert rule["follow"] == "call" and rule_label(rule).startswith("Following the call (HIGH+)")
    with pytest.raises(ValueError, match="the call decides"):
        clean_rule({"follow": "call", "catalyst": "contract"}, 0)
    with pytest.raises(ValueError, match="follow is call"):
        clean_rule({"follow": "odds"}, 0)
    act = {
        "kind": "news",
        "severity": "HIGH",
        "analysis": {"direction": "up"},
        "edge": {"call": {"verb": "act"}},
    }
    wait = {**act, "edge": {"call": {"verb": "wait", "wait_for": "open"}}}
    dip = {**act, "edge": {"call": {"verb": "wait", "wait_for": "pullback"}}}
    assert matches(rule, act) and not matches(rule, wait) and not matches(rule, dip)
    assert waits_for_open(rule, wait) and not waits_for_open(rule, act) and not waits_for_open(rule, dip)
    assert not matches(rule, {**act, "severity": "MEDIUM"})
    assert not matches(rule, {**act, "edge": {}})  # no call: no entry


async def test_following_the_call_on_paper(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    contract_history(eng)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "autopilot_add", {"follow": "call", "min_sev": "HIGH", "exit": "close"})
        rule = eng.desk.get(DEV)["autopilots"][0]
        pre = et(14, 8, 0)  # before the bell
        eng.live_quotes.update("ACME", 100.5, 99.0, pre - 5, "tape")
        a = stored(eng)
        a["edge"]["catalyst"] = {"key": "contract", "label": "Contract win", "band": "mid"}
        a["edge"]["call"] = {"verb": "wait", "wait_for": "open", "label": "Wait"}
        assert await eng.autopilot_open(a, pre) == 0
        pend = eng.desk.get(DEV)["ap_pending"]
        assert len(pend) == 1 and pend[0]["rule"] == rule["id"] and pend[0]["due"] == et(14, 9, 30)
        assert (
            await eng.autopilot_open(a, pre + 60) == 0 and len(eng.desk.get(DEV)["ap_pending"]) == 1
        )  # once
        assert await eng.autopilot_pending(et(14, 9, 0)) == 0  # not open yet: still waiting
        eng.live_quotes.update("ACME", 101.0, 99.0, et(14, 9, 31) - 5, "tape")
        assert await eng.autopilot_pending(et(14, 9, 31)) == 1
        t = eng.desk.get(DEV)["tracked"][-1]
        assert t["auto"] == rule["id"] and t["entry"] == 101.0 and eng.desk.get(DEV)["ap_pending"] == []
        msg = ph.read(server.requests[-1]["body"])
        assert msg["title"] == "🤖 Autopilot: long ACME at $101.00 (at the open, as the call said)"
        b = stored(eng, title="Acme wins another Navy contract")
        b["edge"]["catalyst"] = {"key": "contract", "label": "Contract win", "band": "mid"}
        b["edge"]["call"] = {"verb": "act", "label": "Act now"}
        assert await eng.autopilot_open(b, et(14, 10, 0)) == 1  # act now: at once
    finally:
        await eng.http.close()


async def test_a_wait_not_entered_in_time_is_dropped(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "autopilot_add", {"follow": "call", "min_sev": "HIGH", "exit": "close"})
    p = eng.desk.get(DEV)
    p["ap_pending"] = [
        {"rule": p["autopilots"][0]["id"], "alert_id": "gone", "due": et(14, 9, 30), "created": et(14, 8)}
    ]
    assert await eng.autopilot_pending(et(14, 10, 0)) == 0  # half an hour late, and the alert is gone
    assert p["ap_pending"] == []


# --------------------------------------------------------------------------- moving the line


def test_moving_the_line(tmp_path):
    eng = semis_engine(tmp_path)
    configured = eng.cfg.scoring.medium
    r = set_line(eng, configured - 5, "headlines just under it did as well")
    assert (
        r["line"] == configured - 5
        and eng.cfg.scoring.medium == configured - 5
        and r["default"] == configured
    )
    assert r["log"][-1]["from"] == configured and r["log"][-1]["to"] == configured - 5
    with pytest.raises(ValueError, match="under the HIGH line"):
        set_line(eng, eng.cfg.scoring.high)
    with pytest.raises(ValueError):
        set_line(eng, 5)
    assert line_report(eng)["line"] == configured - 5 and line_report(eng)["log"][-1]["to"] == configured - 5
    # a headline kept 2 under the old line is 3 over the new one: not a near miss any more
    eng.storage.add_shadow("old", time.time() - 3600, {"symbol": "ACME", "sign": 1, "ref": 100.0,
                           "score": configured - 2, "line": configured, "source": "x", "title": "x", "close": 1.0})  # fmt: skip
    assert sum(b["n"] for b in line_report(eng)["bands"]) == 0
    # a restart keeps it
    eng2 = Engine(eng.cfg, storage=eng.storage)
    assert eng2.cfg.scoring.medium == configured - 5
    eng.line_default = configured
    r = set_line(eng, None)
    assert r["line"] == configured and r["log"][-1]["why"] == "back to the configured line"
    assert apply_line(eng) == configured


async def test_run44_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    configured = eng.cfg.scoring.medium
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                srv.make_url("/api/line"), headers=h, json={"medium": configured - 5, "why": "evidence"}
            ) as r:
                assert (await r.json())["line"] == configured - 5
            async with s.post(srv.make_url("/api/line"), headers=h, json={"medium": 200}) as r:
                assert r.status == 400
            async with s.post(srv.make_url("/api/line"), headers=h, json={"reset": True}) as r:
                assert (await r.json())["line"] == configured
            async with s.post(
                srv.make_url("/api/line"), headers={"X-Device": DEV}, json={"reset": True}
            ) as r:
                assert r.status == 401
    finally:
        await srv.close()
        await eng.http.close()
