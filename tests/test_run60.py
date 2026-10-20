"""Run 60: the playbook autopilot (each kind of news entered and exited its own best way, on paper) and
the morning game plan (the 9:20 push names each gapper's best way in and out, its float and dilution
flags, and yesterday's tape)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news247.autopilot import clean_rule, exit_time, matches, playbook_exit, signal_matches, waits_for_open

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run16 import contract_history

ET = ZoneInfo("America/New_York")


def et(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET).timestamp()


def rec(best_in, best_out="close"):
    return {"entry_rec": {"kind": "contract", "label": "Contract win", "best": best_in, "n": 30, "styles": {}},
            "exit_rec": {"kind": "contract", "label": "Contract win", "best": best_out, "n": 30, "styles": {}}}  # fmt: skip


def test_the_playbook_rule():
    rule = clean_rule({"follow": "playbook", "min_sev": "MEDIUM"}, 0)
    assert (
        rule["label"]
        == "Following the playbook (MEDIUM+): each kind of news in and out its own best way here"
    )
    with pytest.raises(ValueError):
        clean_rule({"follow": "playbook", "catalyst": "contract"}, 0)
    a = news("Acme wins $2B Pentagon contract").to_dict()
    assert matches(rule, {**a, "edge": rec("alert")})
    assert not matches(rule, {**a, "edge": rec("leg2")}) and not matches(rule, a)  # no record: no call
    assert waits_for_open(rule, {**a, "edge": rec("open")}) and not waits_for_open(
        rule, {**a, "edge": rec("alert")}
    )
    assert signal_matches(rule, {**a, "edge": rec("leg2")}, "leg2")
    assert not signal_matches(rule, {**a, "edge": rec("leg2")}, "turn")
    assert signal_matches(rule, {**a, "edge": rec("vol15")}, "vol15")


def test_exits_the_playbook_way():
    assert playbook_exit({"edge": rec("alert", "bracket")}) == ("close", True)
    assert playbook_exit({"edge": rec("alert", "d1")}) == ("d1", False)
    assert playbook_exit({"edge": {}}) == ("close", False)
    t0 = et(14, 10)  # Wednesday
    assert exit_time("d5", t0) == et(21, 16)  # five closes on: next Wednesday
    assert exit_time("d1", t0) == et(15, 16) and exit_time("close", t0) == et(14, 16)


async def test_a_playbook_call_rides_its_kinds_exit(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    eng.storage.update_alert_edge(a.id, rec("alert", "d1"))
    assert await eng.autopilot_open(eng.storage.get_alert(a.id)) == 1
    t = eng.desk.get(DEV)["tracked"][-1]
    assert t["stop"] is None and t["target"] is None  # a time exit: no bracket, as its record was graded
    from news247.desk import session_edge

    assert t["exit_at"] == session_edge(
        "close", session_edge("close", time.time(), eng.calendar), eng.calendar
    )


async def test_a_playbook_call_waits_for_its_patient_entry(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    now = time.time()
    eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    edge = {**rec("patient"), "plan": {"dir": "up", "symbol": "ACME", "entry": 100.5, "stop": 98.0, "target": 104.0,
                                       "patient": {"limit": 99.8, "dip": 0.7}}}  # fmt: skip
    eng.storage.update_alert_edge(a.id, edge)
    assert await eng.autopilot_open(eng.storage.get_alert(a.id)) == 0
    lims = eng.desk.get(DEV)["ap_limits"]
    assert len(lims) == 1 and lims[0]["limit"] == 99.8
    assert await eng.autopilot_limits(now + 60) == 0  # not there yet
    eng.live_quotes.update("ACME", 99.75, 99.0, now + 100, "tape")
    assert await eng.autopilot_limits(now + 120) == 1
    assert eng.desk.get(DEV)["ap_limits"] == [] and eng.desk.get(DEV)["tracked"][-1]["auto"]


async def test_volume_confirmation_opens_a_playbook_call(tmp_path):
    from news247 import volconfirm

    from .test_run54 import Tape

    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.ignition = Tape({"ACME": 8.0})
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    now = time.time()
    eng.live_quotes.update("ACME", 100.2, 99.0, now - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    a.created = now - 960
    a.edge = {**rec("vol15"), "refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    await volconfirm.send_confirms(eng, now)
    assert len(eng.desk.get(DEV)["tracked"]) == 1


async def test_the_morning_game_plan(tmp_path, server):
    from .test_run37 import morning

    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    a = morning(eng)
    eng.storage.update_alert_edge(
        a.id, {**rec("leg2", "d1"), "float": {"fband": "tiny"}, "dilution": {"state": "selling"}}
    )
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        assert await eng.send_open_plans(et(14, 9, 21)) >= 1
        body = ph.read(server.requests[0]["body"])["body"]
        assert "· in on the second leg, out the next day, tiny float, ⚠ selling shares" in body
    finally:
        await eng.http.close()
