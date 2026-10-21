"""Run 81: the tide rule (kinds whose playbook result is all market get sat out, graded like every
sit-out rule) and the record after real fills (the mirror's measured fill costs charged to the
playbook's calls, and bands whose fills cost more than its edge sat out)."""

from __future__ import annotations

import json
import time

from news247.autopilot import pass_rules, playbook_pass
from news247.fillcost import after, cost_rates
from news247.fillcost import stamp as cost_stamp
from news247.pbcurve import curve
from news247.pbrules import free, rule_rates
from news247.tide import stamp as tide_stamp
from news247.tide import tide_rates

from .test_desk import news
from .test_personal import semis_engine
from .test_run64 import pair


def kind_pair(pin="alert", pout="close"):
    p = pair(pin, pout)
    p["combo_rec"]["kind"] = "contract"
    return p


def graded(eng, n, close, mkt_close=None, band=None, **edge):
    ids = []
    for i in range(n):
        a = news(f"Acme wins Pentagon contract {i} {close} {band}")
        a.created = time.time() - 86400 * (n + 2 - i)
        a.edge = {
            **kind_pair(),
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "close": close}},
            **edge,
        }
        if mkt_close is not None:
            a.edge.update({"mkt_ref": 600.0, "mkt_outcome": {"close": mkt_close}})
        if band:
            a.edge["liq"] = {"dv": 5e6, "band": band}
        eng.storage.add_alert(a)
        ids.append(a.id)
    return ids


# --------------------------------------------------------------------------- the tide rule


def test_a_kind_that_only_rides_the_market(tmp_path):
    eng = semis_engine(tmp_path)
    graded(eng, 24, 2.0, mkt_close=2.5)  # +2% a call, while SPY rose 2.5% over the same windows
    r = tide_rates(eng)["contract"]
    assert (
        r["tide"]
        and r["avg"] == 2.0
        and r["beyond"] == -0.5
        and r["n"] == 24
        and r["label"] == "Contract win"
    )
    eng.tide_rates = tide_rates(eng)
    assert tide_stamp(eng, "contract")["why"] == (
        "its kind rides the market here: +2.00% a call, but -0.50% beyond SPY over the same windows (24), "
        "in both halves of its record"
    )
    own = semis_engine(tmp_path / "b")
    graded(own, 24, 2.0, mkt_close=0.5)  # +1.5% beyond the market: its own edge
    assert not tide_rates(own)["contract"]["tide"] and tide_stamp(own, "contract") is None


def test_judged_without_sitting_it_out(tmp_path):
    eng = semis_engine(tmp_path)
    graded(eng, 24, 2.0, mkt_close=2.5, tide={"why": "rides the market"})  # already sat out: still judged
    assert tide_rates(eng)["contract"]["tide"]


def test_the_new_rules_in_the_list():
    e = {
        **kind_pair(),
        "tide": {"why": "its kind rides the market here"},
        "costly": {"why": "fills cost more"},
    }
    assert [r for r, _ in pass_rules({"edge": e})] == ["tide", "costly"]
    assert playbook_pass({"edge": e}) == "its kind rides the market here"
    assert playbook_pass({"edge": {**e, "pb_muted": ["tide"]}}) == "fills cost more"
    assert not {"tide", "costly"} & set(free({"edge": e})["edge"])


def test_the_tide_rule_is_graded_like_the_others(tmp_path):
    eng = semis_engine(tmp_path)
    graded(eng, 24, 1.5, tide={"why": "rides the market"})  # the kinds it sat out kept paying
    r = rule_rates(eng)
    assert r["tide"]["label"] == "Kinds that only ride the market" and r["tide"]["verdict"] == "costs"


async def test_publish_stamps_a_tide_kind(tmp_path):
    eng = semis_engine(tmp_path)
    from .test_run63 import history as pairs

    pairs(eng)
    eng.refresh_base_rates()
    rec = {"tide": True, "avg": 2.0, "beyond": -0.5, "n": 24}
    eng.tide_rates = {k: rec for k in eng.combo_rates}
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    got = eng.storage.get_alert(a.id)
    assert got["edge"]["tide"]["why"].startswith("its kind rides the market here") and playbook_pass(
        got
    ).startswith("its kind rides the market")


# --------------------------------------------------------------------------- the record after real fills


def mirrored(eng, ids, cost):
    """A mirror log: one round trip per alert, filled ``cost`` worse in (and level out)."""
    log = {}
    for i, aid in enumerate(ids):
        log[f"t{i}"] = {"symbol": "ACME", "direction": "long", "qty": 10, "alert_id": aid, "opened": time.time() - i,
                        "desk_entry": 100.0, "desk_exit": 101.0, "desk_pnl": 1.0,
                        "entry": {"status": "filled", "fill": 100.0 * (1 + cost / 100)}, "exit": {"status": "filled", "fill": 101.0}}  # fmt: skip
    eng.storage.set_setting("mirror_log", json.dumps(log))


def test_a_band_whose_fills_cost_more_than_its_edge(tmp_path):
    eng = semis_engine(tmp_path)
    ids = graded(eng, 12, 0.3, band="light")  # the playbook makes +0.3% a call in light stocks...
    mirrored(eng, ids, 0.4)  # ...and fills there cost 0.4% a round trip
    r = cost_rates(eng)["light"]
    assert r["n"] == 12 and r["cost"] == 0.4 and r["edge"] == 0.3 and r["costly"]
    eng.cost_rates = cost_rates(eng)
    assert cost_stamp(eng, "light")["why"] == (
        "fills in light stocks cost +0.40% a round trip on the mirror (12), at least what the playbook makes there (+0.30%)"
    )
    assert cost_stamp(eng, "deep") is None  # not measured: no guess


def test_nothing_without_the_mirror(tmp_path):
    eng = semis_engine(tmp_path)
    graded(eng, 12, 0.3, band="light")
    assert cost_rates(eng) == {}
    few = semis_engine(tmp_path / "b")
    mirrored(few, graded(few, 5, 0.3, band="light"), 0.4)
    assert cost_rates(few) == {}  # 5 round trips: under 10


def test_the_record_after_fills(tmp_path):
    eng = semis_engine(tmp_path)
    eng.cost_rates = {"light": {"cost": 0.4}}
    r = after(eng, [(1, 2.0, "light"), (2, 1.0, "light"), (3, 0.0, "light"), (4, 3.0, "deep")])
    assert r["costed"] == 2 and r["paper"] == 3.0 and r["net"] == 2.2 and r["unmeasured"] == 1
    assert r["line"] == (
        "After real fills (measured on the mirror): on the 2 calls in measured bands, the playbook's +3.0% becomes "
        "+2.2%; 1 more in bands not measured yet, not charged."
    )
    assert after(semis_engine(tmp_path / "b"), [(1, 2.0, "light")])["line"] == ""
    graded(eng, 3, 1.0, band="light")
    assert curve(eng)["fills"]["costed"] == 3
