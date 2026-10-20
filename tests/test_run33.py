"""Run 33: the plan in the push (stop, target, reward-to-risk and the best hold on the lock screen)
and patient entries (a limit at the winners' median dip, with the adverse selection it brings)."""

from __future__ import annotations

import time

import pytest

from news247.heat import heat_rates, patient, share_at
from news247.plans import plan_line, trade_plan

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run15 import graded
from .test_run16 import contract_history, stored

# --------------------------------------------------------------------------- the plan in the push


def test_the_plan_line():
    plan = {"dir": "up", "symbol": "ACME", "entry": 100.5, "stop": 98.85, "target": 102.75, "rr": 1.36}
    assert plan_line(plan) == "Plan: long $100.50 · stop $98.85 · target $102.75 · R:R 1.4"
    odds = {"best": {"at": "d5", "label": "five sessions on", "ev": 2.6}}
    assert plan_line({**plan, "dir": "down"}, odds).endswith("· R:R 1.4 · hold to five sessions on")
    assert plan_line({**plan, "dir": "down"}).startswith("Plan: short")
    assert plan_line(None) == ""


async def test_a_new_alert_carries_its_plan_to_the_lock_screen(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $900M Navy contract")
    a.edge = {"refs": {"ACME": 100.0}, "pre": {"ACME": 1.0}}
    await eng.publish(a, push=False)
    plan = eng.storage.get_alert(a.id)["edge"]["plan"]
    assert (plan["dir"], plan["symbol"], plan["entry"], plan["target"]) == (
        "up",
        "ACME",
        100.5,
        pytest.approx(102.75),
    )
    body = eng.webpush.payload(a)["body"]
    assert "Plan: long $100.50 · stop $99.01 · target $102.75 · R:R 1.5" in body
    assert body.index("Plan:") < body.index("Contract win, $2-10B:")  # the plan before the record
    b = news("Gamma hosts investor day", tickers=("GAMA",))  # no price, no record: no plan, nothing guessed
    await eng.publish(b, push=False)
    assert "plan" not in eng.storage.get_alert(b.id)["edge"] and "Plan:" not in eng.webpush.payload(b)["body"]


# --------------------------------------------------------------------------- patient entries


def heat_with_losers(eng):
    for i in range(12):  # winners dipped 0.1% to 1.2% first
        a = graded(f"Acme wins contract heat {i}", 2.0)
        a.edge["exc"] = {"mae": -round(0.1 * (i + 1), 1), "mfe": 2.5}
        eng.storage.add_alert(a)
    for i, dip in enumerate([0.2, 0.8, 1.0, 1.5, 2.0, 2.5]):  # losers: most dipped past the winners' median
        a = graded(f"Acme wins contract tease {i}", -1.0)
        a.edge["exc"] = {"mae": -dip, "mfe": 0.4}
        eng.storage.add_alert(a)


def test_the_cost_of_waiting_for_a_dip(tmp_path):
    eng = semis_engine(tmp_path)
    heat_with_losers(eng)
    h = heat_rates(eng)["contract"]
    assert h["dip_median"] == 0.7 and share_at(h["w_dips"], 0.7) == 0.5 and share_at(h["l_dips"], 0.7) == 0.83
    pt = patient(h, 100.0, 100.5, 98.0, 103.0, 1, 0.75)
    assert pt["limit"] == pytest.approx(99.3) and (pt["fills_winners"], pt["fills_losers"]) == (0.5, 0.83)
    assert pt["p_now"] == 0.75 and pt["p_filled"] == pytest.approx(0.64, abs=0.01)  # adverse selection
    assert pt["rr"] == pytest.approx((103 - 99.3) / (99.3 - 98.0), abs=0.01)
    assert patient(h, 100.0, 99.0, 98.0, 103.0, 1, 0.75) is None  # already under the limit: just take it
    assert patient(h, 100.0, 100.5, 99.5, 103.0, 1, 0.75) is None  # the limit would sit under the stop
    short = patient(h, 100.0, 99.5, 102.0, 97.0, -1, 0.6)
    assert short["limit"] == pytest.approx(100.7)  # a short waits for a bounce
    assert patient({**h, "l_dips": [1.0, 2.0]}, 100.0, 100.5, 98.0, 103.0, 1, 0.75) is None  # too few losers


def test_plans_offer_the_patient_entry(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    heat_with_losers(eng)
    eng.refresh_base_rates()
    now = time.time()
    eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
    a = stored(eng)
    plan = trade_plan(eng, DEV, a, now)
    pt = plan["patient"]
    assert pt["limit"] == pytest.approx(99.3) and pt["p_filled"] < pt["p_now"]
    assert plan["stop"] < pt["limit"] < plan["entry"]
