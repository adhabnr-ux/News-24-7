"""Run 70: the playbook for every kind of alert (alerts with no kind of news read get pairs by what
raised them: options flow, confluence, price moves, other news) and the playbook reads the stock (it
sits out stocks that fight their news, and takes the first hour on stocks that fade by the close)."""

from __future__ import annotations

import time

from news247.autopilot import clean_rule, matches, playbook_in, playbook_out, playbook_pass
from news247.combos import combo_rates, key_of, label_of
from news247.today import state
from news247.youvs import playbook_result

from .test_desk import news
from .test_personal import semis_engine
from .test_run60 import et
from .test_run64 import alert, pair

# --------------------------------------------------------------------------- every kind of alert


def test_the_key_and_its_label():
    assert key_of({"kind": "options"}, None) == "kind:options" and key_of({}, None) == "kind:news"
    assert key_of({"kind": "news"}, {"key": "contract"}) == "contract"
    assert label_of("kind:options") == "Options flow" and label_of("kind:news") == "Other news"
    assert (
        label_of("contract") == "Contract win"
        and label_of("*") == "Any news"
        and label_of("kind:zzz") == "Zzz"
    )


def flow_history(eng):
    """Options-flow alerts with no kind of news read: they work best held to the next day."""
    for i in range(12):
        a = news(f"Unusual call buying in ACME {i}")
        a.kind = "options"
        a.created = time.time() - 86400 * (20 - i)
        a.edge = {"outcome": {"ACME": {"sign": 1, "close": 0.5, "d1": 2.0 if i % 4 else -1.0}}}
        eng.storage.add_alert(a)


def test_options_flow_gets_its_own_pair(tmp_path):
    eng = semis_engine(tmp_path)
    flow_history(eng)
    r = combo_rates(eng)["kind:options"]
    assert r["label"] == "Options flow" and r["best"] == "alert|d1" and r["n"] == 12


async def test_the_pair_rides_an_options_alert(tmp_path):
    eng = semis_engine(tmp_path)
    flow_history(eng)
    eng.refresh_base_rates()
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Unusual call buying in ACME")
    a.kind = "options"
    await eng.publish(a, push=False)
    cr = eng.storage.get_alert(a.id)["edge"]["combo_rec"]
    assert cr["kind"] == "kind:options" and cr["best"] == "alert|d1" and cr["label"] == "Options flow"
    rule = clean_rule({"follow": "playbook", "min_sev": "MEDIUM"}, 0)
    assert matches(rule, eng.storage.get_alert(a.id))  # the playbook autopilot can take it now


# --------------------------------------------------------------------------- the playbook reads the stock


def test_it_sits_out_a_stock_that_fights_its_news():
    a = {
        **news("Acme wins contract").to_dict(),
        "edge": {**pair("alert", "d1"), "persona": {"trait": "fights", "hit": 0.22, "n": 9}},
    }
    assert playbook_pass(a) == "this stock fights its news here (22% of its 9 alerts went their way)"
    assert playbook_in(a) is None
    t0 = et(14, 10)
    s = state(
        alert(t0, **pair("alert", "d1"), persona={"trait": "fights", "hit": 0.22, "n": 9}), 1.0, t0 + 600
    )
    assert s["status"] == "pass" and s["why"].startswith("this stock fights its news")
    graded = {
        "edge": {
            **a["edge"],
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "close": 2.0, "d1": 3.0}},
        }
    }
    assert playbook_result(graded) == (0.0, "pass")


def test_it_takes_the_first_hour_on_a_fader():
    fades = {"trait": "fades_day", "hit": 0.5, "n": 10}
    a = {"edge": {**pair("alert", "d1"), "persona": fades}}
    assert playbook_out(a) == "h1"  # in at the alert: out an hour in
    b = {"edge": {**pair("leg2", "d1"), "persona": fades}}
    assert playbook_out(b) == "close"  # a second leg can't be sold an hour in: no overnight hold at least
    c = {"edge": {**pair("alert", "h1"), "persona": fades}}
    assert playbook_out(c) == "h1"
    plain = {"edge": pair("alert", "d1")}
    assert playbook_out(plain) == "d1" and playbook_pass(plain) == ""
