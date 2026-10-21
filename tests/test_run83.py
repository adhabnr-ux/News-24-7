"""Run 83: how sure the walk-forward is (an 80% range on the learner's edge over the plain way, in all
and per kind of news) and per-kind trust (a kind whose learner lost to the plain way, with its whole
range below zero, goes out with the plain way instead)."""

from __future__ import annotations

import time

from news247.walkforward import MIN_KIND, edge, fallback, walk_forward

from .test_desk import news
from .test_personal import semis_engine


def res(gaps_by_kind):
    out, i = {}, 0
    for k, gaps in gaps_by_kind.items():
        for g in gaps:
            out[f"a{i}"] = {
                "created": float(i),
                "key": k,
                "pb": 1.0 + g,
                "plain": 1.0,
                "month": 0,
                "pick": "alert|d1",
            }
            i += 1
    return out


def test_the_range_and_each_kinds_verdict():
    e, kinds = edge(
        res(
            {
                "contract": [0.5, 0.3, 0.7] * 12,
                "fda_approval": [-0.4, -0.6, -0.5] * 12,
                "beat": [1.0, -1.0] * 18,
            }
        )
    )
    assert kinds["contract"]["verdict"] == "earned" and kinds["contract"]["lo"] > 0
    assert (
        kinds["fda_approval"]["verdict"] == "plain"
        and kinds["fda_approval"]["hi"] < 0
        and kinds["fda_approval"]["label"] == "FDA approval"
    )
    assert kinds["beat"]["verdict"] == "unclear"
    assert e["n"] == 108
    few, _ = edge(res({"contract": [0.5] * 5}))
    assert few["n"] == 5 and edge(res({"contract": [0.5] * 5}))[1] == {}  # under 30: no verdict
    assert MIN_KIND == 30


def test_the_fallback_reason():
    _, kinds = edge(res({"fda_approval": [-0.4, -0.6, -0.5] * 12}))
    fb = fallback({"kinds": kinds}, "fda_approval")
    assert fb["why"].startswith(
        "walked forward, picking a pair for fda approval news did worse than the plain way (-0.50% an alert"
    )
    assert fb["why"].endswith("36): the plain way it is")
    assert fallback({"kinds": kinds}, "contract") is None and fallback({}, "fda_approval") is None


def test_the_line_says_how_sure(tmp_path):
    eng = semis_engine(tmp_path)
    for m in range(4):
        for i in range(12):
            a = news(f"Acme wins Pentagon contract {m} {i}")
            a.created = time.time() - 86400 * (30 * (4 - m) + 10 - i * 0.1)
            d1 = 2.0 + (i % 3) * 0.5
            a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 1.0, "d1": d1}}}
            eng.storage.add_alert(a)
    r = walk_forward(eng)
    assert r["edge"]["lo"] > 0 and " (a real edge)." in r["line"]


async def test_a_kind_the_learner_lost_on_goes_out_plain(tmp_path):
    eng = semis_engine(tmp_path)
    from .test_run63 import history as pairs

    pairs(eng)
    eng.refresh_base_rates()
    eng._wf_at = time.time()  # keep the verdict above: no recompute this hour
    eng.walkforward = {"kinds": {k: {"kind": k, "label": "Contract win", "n": 40, "avg": -0.6, "lo": -0.9, "hi": -0.3,
                                     "verdict": "plain"} for k in eng.combo_rates}}  # fmt: skip
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    cr = eng.storage.get_alert(a.id)["edge"]["combo_rec"]
    assert cr["best"] == "alert|close" and cr["in"] == "alert" and cr["out"] == "close"
    assert cr["wf"]["pick"] == "alert|d1" and cr["wf"]["why"].endswith("the plain way it is")
