"""Run 30: heat (how far winners dipped and losers ran before the close, recorded each minute,
and stops placed from it) and odds by horizon (the next day and five sessions on, each tested, and
the best exit)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news247.dossier import record_outcomes
from news247.heat import heat_for, heat_rates
from news247.odds import fit_odds, predict, report
from news247.plans import trade_plan

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run15 import graded
from .test_run16 import contract_history, stored
from .test_run23 import noon

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 14, 14, 0, tzinfo=ET).timestamp()
CLOSE = datetime(2026, 10, 14, 16, 0, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- heat


def test_each_alert_keeps_its_best_run_and_worst_dip_until_the_close(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract")
    a.created = T0
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    for t, px in ((T0 + 600, 99.4), (T0 + 1200, 101.2), (T0 + 1800, 100.5)):
        eng.live_quotes.update("ACME", px, 99.0, t - 5, "tape")
        record_outcomes(eng, t)
    assert eng.storage.get_alert(a.id)["edge"]["exc"] == {"mfe": 1.2, "mae": -0.6}
    eng.live_quotes.update("ACME", 95.0, 99.0, CLOSE + 60, "tape")
    record_outcomes(eng, CLOSE + 120)  # after the close: graded, the excursion is done
    e = eng.storage.get_alert(a.id)["edge"]
    assert e["exc"] == {"mfe": 1.2, "mae": -0.6} and e["outcome"]["ACME"]["close"] == -5.0
    short = news("Beta downgraded to sell", tickers=("BETA",), direction="down")
    short.created = T0
    short.edge = {"refs": {"BETA": 100.0}}
    eng.storage.add_alert(short)
    eng.live_quotes.update("BETA", 101.0, 99.0, T0 + 595, "tape")
    record_outcomes(eng, T0 + 600)
    assert eng.storage.get_alert(short.id)["edge"]["exc"] == {
        "mfe": -1.0,
        "mae": -1.0,
    }  # up 1%: against a short


def heated(eng):
    for i in range(12):  # winners that dipped 0.1% to 1.2% first
        a = graded(f"Acme wins contract heat {i}", 2.0)
        a.edge["exc"] = {"mae": -round(0.1 * (i + 1), 1), "mfe": 2.5}
        eng.storage.add_alert(a)
    for i, run in enumerate([0.2, 0.5, 0.8, 1.0]):  # losers that teased first
        a = graded(f"Acme wins contract tease {i}", -1.0)
        a.edge["exc"] = {"mae": -1.5, "mfe": run}
        eng.storage.add_alert(a)


def test_the_heat_of_a_kind_of_news(tmp_path):
    eng = semis_engine(tmp_path)
    heated(eng)
    h = heat_rates(eng)
    keys = ("winners", "dip80", "dip_median", "losers", "run_median")
    assert {k: h["contract"][k] for k in keys} == {
        "winners": 12,
        "dip80": 1.0,
        "dip_median": 0.7,
        "losers": 4,
        "run_median": 0.8,
    }
    assert h["contract"]["l_dips"] == [1.5, 1.5, 1.5, 1.5]
    assert heat_for(h, news("Acme wins contract").to_dict(), eng.universe)["from"] == "contract win news"
    assert heat_for(h, news("Acme hosts investor day").to_dict(), eng.universe)["from"] == "every alert"
    assert heat_for({}, news("x").to_dict()) is None


def test_plans_take_their_stop_from_the_heat(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    heated(eng)
    eng.refresh_base_rates()
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    p = trade_plan(eng, DEV, stored(eng), time.time())
    assert p["stop"] == pytest.approx(98.85)  # 1.0% (80% of winners) + 0.15%
    assert (
        p["stop_basis"]
        == "past where 80% of winners on contract win news dipped before working (−1.00%, 12 winners)"
    )
    assert p["heat"]["winners"] == 12


# --------------------------------------------------------------------------- odds by horizon


def horizons(eng, n=160):
    """Contract wins: 75% by the close, 90% five sessions on. Upgrades: 25% and 20%."""
    for i in range(n):
        contract = i % 2 == 0
        k = i // 2
        close_good = (k % 4 != 0) if contract else (k % 4 == 0)
        d5_good = (k % 10 != 0) if contract else (k % 10 < 2)
        title = f"Acme wins contract number {i}" if contract else f"Morgan Stanley upgrades Acme {i}"
        a = graded(title, 2.0 if close_good else -1.5)
        row = a.edge["outcome"]["ACME"]
        row["d1"] = row["close"]
        row["d5"] = (3.0 if d5_good else -1.0) if contract else (1.0 if d5_good else -2.0)
        a.created = noon(n - i + 8)
        eng.storage.add_alert(a)


def test_odds_by_horizon_and_the_best_exit(tmp_path):
    eng = semis_engine(tmp_path)
    horizons(eng)
    st = fit_odds(eng)
    assert st["trusted"] and st["horizons"]["d5"]["trusted"] and st["horizons"]["d1"]["trusted"]
    assert st["horizons"]["d5"]["payoff"]["contract"] == {"win": 3.0, "loss": 1.0, "n": 80}
    a = news("Acme wins $900M Navy contract")
    a.created = noon()
    o = predict(st, a.to_dict(), None)
    d5 = o["by"]["d5"]
    assert d5["label"] == "five sessions on" and d5["p"] > o["p"] and d5["ev"] > o["ev"]
    assert o["best"] == {"at": "d5", "label": "five sessions on", "ev": d5["ev"]}
    up = news("Morgan Stanley upgrades Acme to buy")
    up.created = noon()
    uo = predict(st, up.to_dict(), None)
    assert uo["by"]["d5"]["ev"] < 0
    rep = report(st)
    assert "model" not in rep["horizons"]["d5"] and rep["horizons"]["d5"]["test"]["n"] == 40


def test_no_horizon_until_it_has_its_own_history(tmp_path):
    eng = semis_engine(tmp_path)
    from .test_run23 import history

    history(eng)  # closes only: nothing graded the next day or five sessions on
    st = fit_odds(eng)
    assert st["trusted"] and st["horizons"] == {
        "d1": {"n": 0, "trusted": False},
        "d5": {"n": 0, "trusted": False},
    }
    a = news("Acme wins contract")
    a.created = noon()
    o = predict(st, a.to_dict(), None)
    assert "by" not in o and "best" not in o
