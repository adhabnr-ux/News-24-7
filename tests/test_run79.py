"""Run 79: what each of the playbook's rules adds (every sit-out rule graded on the alerts it flagged,
with the ones that keep sitting out winners muted) and conviction sizing (full size when the pair's
range clears zero, half otherwise, graded on the same alerts)."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.autopilot import pass_rules, playbook_pass
from news247.pbcurve import curve
from news247.pbmorning import playbook_line
from news247.pbrules import board, free, rule_rates
from news247.sizing import grade, label, size
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run16 import contract_history
from .test_run60 import rec
from .test_run64 import pair

THIN = {"pass": True, "why": "a thin stock: thin names here lost even 5 minutes late (-1.9%, 16)"}

# --------------------------------------------------------------------------- the rules


def test_every_rule_that_flags_an_alert():
    e = {**pair("alert", "close"), "liq": THIN, "persona": {"symbol": "ACME", "trait": "fights", "hit": 0.3, "n": 12}}
    a = {"edge": e}
    assert [r for r, _ in pass_rules(a)] == ["liq", "persona"]
    assert playbook_pass(a) == THIN["why"]
    muted = {"edge": {**e, "pb_muted": ["liq"]}}
    assert playbook_pass(muted).startswith("this stock fights its news")  # the next rule still holds
    assert playbook_pass({"edge": {**e, "pb_muted": ["liq", "persona"]}}) == ""  # both muted: taken
    f = free({"edge": {**e, "blind": {"why": "x"}, "breaker": {"why": "y"}, "pb_muted": ["liq"]}})["edge"]
    assert not {"liq", "persona", "blind", "breaker", "pb_muted"} & set(f) and pass_rules({"edge": f}) == []


def flagged(eng, n, close, **edge):
    for i in range(n):
        a = news(f"Acme wins Pentagon contract {i} {close}")
        a.created = time.time() - 86400 * (n + 2 - i)
        a.edge = {
            **pair("alert", "close"),
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "close": close}},
            **edge,
        }
        eng.storage.add_alert(a)


def test_a_rule_that_costs_is_muted(tmp_path):
    eng = semis_engine(tmp_path)
    flagged(eng, 24, 1.5, liq=THIN)  # thin stocks it sat out kept making money, in both halves
    flagged(eng, 22, -1.0, persona={"symbol": "ACME", "trait": "fights", "hit": 0.3, "n": 12})  # these it was right to skip
    r = rule_rates(eng)
    assert r["liq"]["verdict"] == "costs" and r["liq"]["n"] == 24 and r["liq"]["avg"] == 1.5
    assert r["persona"]["verdict"] == "earns" and r["muted"] == ["liq"]
    rows = {x["rule"]: x for x in board(r)["rows"]}
    assert rows["liq"]["line"] == (
        "Thin stocks: the 24 alerts it flagged would have made +1.50% on average (+36.0% in all), in both halves "
        "(+1.50%, then +1.50%): it keeps sitting out winners, so it's muted."
    )
    assert rows["persona"]["line"].endswith("it saved 22.0 points.")


def test_too_few_to_judge(tmp_path):
    eng = semis_engine(tmp_path)
    flagged(eng, 8, 2.0, liq=THIN)
    r = rule_rates(eng)
    assert r["liq"]["verdict"] == "unclear" and r["muted"] == []
    assert board(r)["rows"][0]["line"].endswith("too few to judge (20 needed).")


async def test_publish_carries_the_muted_rules(tmp_path):
    eng = semis_engine(tmp_path)
    from .test_run63 import history as pairs

    pairs(eng)
    eng.refresh_base_rates()
    eng.rule_rates = {"muted": ["liq"]}
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert eng.storage.get_alert(a.id)["edge"]["pb_muted"] == ["liq"]


# --------------------------------------------------------------------------- conviction sizing


def ranged(lo, hi, proven=True):
    p = pair("alert", "d1")
    p["combo_rec"].update(
        {"proven": proven, "styles": {"alert|d1": {"avg": 1.0, "n": 30, "lo": lo, "hi": hi}}}
    )
    return p


def test_the_size():
    assert size(ranged(0.4, 2.0)) == (1.0, "full size: its range clears zero (+0.4% to +2.0%)")
    assert size(ranged(-0.3, 2.0)) == (0.5, "half size: its range reaches below zero (-0.3% to +2.0%)")
    assert size(ranged(0.4, 2.0, proven=False))[0] == 0.5
    assert size(pair("alert", "d1")) == (1.0, "")  # stamped before ranges were kept: unsized
    assert label(size(ranged(-0.3, 2.0))[1]) == "half" and label("") == ""
    assert playbook_line(ranged(-0.3, 2.0)).endswith("(+1.0% on average, 30) · half size")
    assert not playbook_line(ranged(0.4, 2.0)).endswith("half size")


def test_sized_against_equal():
    g = grade(
        [
            (1, 2.0, 1.0, 1.0, "full"),
            (2, -1.0, 0.0, 0.5, "half"),
            (3, 1.0, 0.0, 0.5, "half"),
            (4, 0.0, 1.0, 1.0, ""),
        ]
    )
    assert g["full"] == {"n": 1, "avg": 2.0} and g["half"] == {"n": 2, "avg": 0.0}
    assert g["equal"] == 2.0 and g["sized"] == 2.0
    assert (
        g["line"]
        == "Sized by conviction: full-size calls made +2.00% on average (1), half-size +0.00% (2): it helps."
    )


def test_the_curve_grades_it(tmp_path):
    eng = semis_engine(tmp_path)
    for i, (d1, sure) in enumerate(((3.0, True), (2.0, True), (-1.0, False), (0.5, False))):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (10 - i)
        a.edge = {**(ranged(0.4, 2.0) if sure else ranged(-0.5, 1.5)), "refs": {"ACME": 100.0},
                  "outcome": {"ACME": {"sign": 1, "close": 1.0, "d1": d1}}}  # fmt: skip
        eng.storage.add_alert(a)
    c = curve(eng, capital=10_000.0)
    s = c["sizing"]
    assert s["full"] == {"n": 2, "avg": 2.5} and s["half"] == {"n": 2, "avg": -0.25} and s["sized"] == 4.75
    # a tenth of the balance in each call, at its size: half of -1% and of +0.5% for the unsure two
    assert s["money"] == round(10_000 * 1.003 * 1.002 * 0.9995 * 1.00025, 2)
    assert "Sized, the account ends $" in s["line"]


async def test_the_autopilot_sizes_its_call(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "prefs", {"account": 10000, "risk_pct": 1})
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    e = rec("alert", "d1")
    e.update(ranged(-0.3, 2.0))
    eng.storage.update_alert_edge(a.id, e)
    assert await eng.autopilot_open(eng.storage.get_alert(a.id)) == 1
    t = [t for t in eng.desk.get(DEV)["tracked"] if t.get("alert_id") == a.id][0]
    assert t["size"] == 0.5 and "half size: its range reaches below zero" in t["note"]


# --------------------------------------------------------------------------- the endpoint


async def test_the_rules_endpoint(tmp_path):
    eng = semis_engine(tmp_path)
    flagged(eng, 24, 1.5, liq=THIN)
    eng.refresh_base_rates()
    assert eng.rule_rates["muted"] == ["liq"]
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with (
            aiohttp.ClientSession() as s,
            s.get(srv.make_url("/api/playbook-rules"), headers={"Authorization": "Bearer tok"}) as r,
        ):
            body = await r.json()
            assert (
                body["muted"] == ["liq"] and body["rows"][0]["label"] == "Thin stocks" and body["need"] == 20
            )
    finally:
        await srv.close()
        await eng.http.close()
