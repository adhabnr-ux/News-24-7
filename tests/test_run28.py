"""Run 28: live odds (an hour in, how alerts that had done the same finished the day) and the
attention budget (push only the N best alerts a day, ranked by expected move against 10 days)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news247.desk import Desk
from news247.keptback import settings_cost
from news247.liveodds import h1_table, live_line, live_odds
from news247.manage import nudges
from news247.models import Severity
from news247.precheck import pre_trade
from news247.storage import Storage

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run15 import graded

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 14, 11, 30, tzinfo=ET).timestamp()  # a Wednesday, mid-morning


def history(eng):
    """Contract wins up 1%+ after an hour finished their way 9 times in 10; those down 1%+ after an
    hour, 2 in 10. Upgrades: a few, not enough for their own rows."""
    for i in range(10):
        eng.storage.add_alert(graded(f"Acme wins contract up {i}", 2.0 if i else -0.5, h1=1.5))
        eng.storage.add_alert(graded(f"Acme wins contract down {i}", 1.0 if i < 2 else -2.0, h1=-1.5))
    for i in range(3):
        eng.storage.add_alert(graded(f"Morgan Stanley upgrades Acme {i}", -1.0, h1=1.2))


def fresh(eng, title="Acme wins $900M Navy contract", px=101.5):
    a = news(title)
    a.created = NOW - 90 * 60
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    eng.live_quotes.update("ACME", px, 99.0, NOW - 5, "tape")
    return eng.storage.get_alert(a.id)


# --------------------------------------------------------------------------- live odds


def test_how_the_first_hour_ends_the_day(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    t = h1_table(eng)
    up, down = t["contract"][4], t["contract"][0]
    assert (up["label"], up["n"], up["hit"], up["median"]) == ("up 1%+ its way", 10, 0.9, 2.0)
    assert (down["n"], down["hit"]) == (10, 0.2)
    assert (
        t["upgrade"][4]["n"] == 3 and t["*"][4]["n"] == 13 and t["contract"][2] == {"label": "flat", "n": 0}
    )


def test_an_alert_an_hour_in_gets_its_live_odds(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    a = fresh(eng)  # 90 minutes old, +1.5% its way
    lo = live_odds(eng, a, NOW)
    assert (lo["since"], lo["hours"], lo["p"], lo["n"], lo["from"]) == (
        1.5,
        1.5,
        0.9,
        10,
        "contract win news",
    )
    assert live_line(lo) == ("1.5 hours in, +1.5% its way: contract win news that was up 1%+ its way after an hour "
                             "finished its way 90% of the time (10)")  # fmt: skip
    assert live_odds(eng, a, NOW - 60 * 60) is None  # 30 minutes old: too early
    close = datetime(2026, 10, 14, 16, 1, tzinfo=ET).timestamp()
    assert live_odds(eng, a, close) is None  # the day is done: it's graded, not live
    up = fresh(eng, "Morgan Stanley upgrades Acme to buy")  # its own rows are too thin: every alert's
    assert live_odds(eng, up, NOW)["from"] == "every alert"
    assert live_line(None) == ""


def test_live_odds_in_the_check_and_a_cut_nudge(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    a = fresh(eng, px=98.5)  # 90 minutes in, -1.5% against it
    r = pre_trade(eng, DEV, "ACME", "long", a["id"], NOW)
    live = next(c for c in r["checks"] if c["key"] == "live")
    assert live["tone"] == "bad" and live["text"].endswith("finished their way 20% (10)")
    eng.desk.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100, "alert_id": a["id"]}
    )
    n = next(x for x in nudges(eng, DEV, NOW) if x["kind"] == "cut")
    assert n["title"] == "ACME: 1.5 hours in and -1.5% its way. Alerts like this finished their way 20%"
    assert n["do"]["body"]["lesson"] == "cut on the live odds"
    # fading the alert (short against good news): the collapse is the call working, no nudge
    eng.desk.apply(
        DEV2, "track_add", {"symbol": "ACME", "direction": "short", "entry": 100, "alert_id": a["id"]}
    )
    assert not any(x["kind"] == "cut" for x in nudges(eng, DEV2, NOW))


# --------------------------------------------------------------------------- the attention budget


def trusted_model(eng):
    eng.odds = {"trusted": True, "n": 100, "test": {"skill": 0.2}, "model": {"b": 1.0, "w": {}, "base": 0.6},
                "payoff": {"*": {"win": 2.0, "loss": 1.0, "n": 50}}}  # fmt: skip  # 73% odds, +1.19% expected


def with_ev(eng):
    for i in range(1, 21):  # 20 alerts over the day, expected moves 0.1% to 2.0%
        a = news(f"Acme news {i}")
        a.edge = {"odds": {"p": 0.6, "ev": round(i / 10, 1), "n": 100}}
        eng.storage.add_alert(a)


async def test_alerts_are_ranked_against_ten_days(tmp_path):
    eng = semis_engine(tmp_path)
    with_ev(eng)
    eng.refresh_base_rates()
    assert len(eng.ev_hist[0]) == 20 and eng.ev_hist[1] == 1
    trusted_model(eng)
    a = news("Acme wins $900M Navy contract")
    await eng.publish(a, push=False)
    edge = eng.storage.get_alert(a.id)["edge"]
    assert (
        edge["odds"]["ev"] == pytest.approx(1.19, abs=0.01) and edge["ev_top"] == 9.0
    )  # 1.2 to 2.0: nine a day


def test_the_budget_lets_only_the_best_through():
    d = Desk(Storage(":memory:"))
    a = news("Gamma wins contract", tickers=("GAMA",))
    a.edge = {"ev_top": 9.0}
    d.apply(DEV, "prefs", {"budget": 5})
    b = d.block(DEV, a)
    assert (
        b["code"] == "budget"
        and b["text"] == "it isn't among your 5 best a day (about 9 a day are this good)"
    )
    assert b["fix"] == {"label": "Turn the budget off", "action": "prefs", "body": {"budget": 0}}
    d.apply(DEV, "prefs", {"budget": 10})
    assert d.allows(DEV, a) is True
    d.apply(DEV, "prefs", {"budget": 3})
    a.edge = {"ev_top": 2.5}
    assert d.allows(DEV, a) is True  # among the three best
    a.edge = {"ev_top": 9.0}
    d.apply(DEV, "watch_add", {"symbol": "GAMA"})
    assert d.allows(DEV, a) is True  # your own stocks still come
    d.apply(DEV, "watch_remove", {"symbol": "GAMA"})
    crit = news("Gamma halted", tickers=("GAMA",), sev=Severity.CRITICAL)
    crit.edge = {"ev_top": 50.0}
    assert d.allows(DEV, crit) is True
    assert (
        d.allows(DEV, news("No odds here", tickers=("GAMA",))) is True
    )  # no expected move read: not held back
    with pytest.raises(ValueError, match="budget is 0"):
        d.apply(DEV, "prefs", {"budget": 7})


def test_the_budget_is_graded_with_the_other_settings(tmp_path):
    eng = semis_engine(tmp_path)
    for i, r in enumerate([2.0, 1.5, 3.0, -1.0, 2.5, 1.0]):
        a = graded(f"Gamma headline {i}", r, sym="GAMA")
        a.edge["ev_top"] = 12.0
        eng.storage.add_alert(a)
    eng.desk.apply(DEV, "prefs", {"budget": 3})
    g = settings_cost(eng, DEV)["gates"][0]
    assert g["setting"] == "Your daily budget" and g["n"] == 6 and g["verdict"] == "costing"
