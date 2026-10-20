"""Run 63: the whole playbook backtested together (every way in paired with every way out, from its
own entry price to its own exit, checked out of sample, traded by the playbook autopilot) and the
second headline graded (repeat news on a stock within a week, against first headlines)."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.autopilot import playbook_exit, playbook_in, playbook_out
from news247.combos import DEFAULT, combo_line, combo_rates, combos
from news247.forward import promise
from news247.odds import features
from news247.repeat import line, previous, repeat_rates, stamp
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run16 import contract_history
from .test_run60 import rec

# --------------------------------------------------------------------------- the whole playbook, together


def test_one_alert_every_pair():
    a = {"edge": {"refs": {"ACME": 100.0}, "leg2": {"px": 102.0},
                  "outcome": {"ACME": {"sign": 1, "h1": 1.0, "close": 3.0, "d1": 5.0, "d5": 0.0, "open": 0.5}}}}  # fmt: skip
    c = combos(a)
    assert c["alert|h1"] == 1.0 and c["alert|close"] == 3.0 and c["alert|d1"] == 5.0
    assert c["leg2|close"] == round((1.03 / 1.02 - 1) * 100, 2) and c["leg2|d1"] == round(
        (1.05 / 1.02 - 1) * 100, 2
    )
    assert "leg2|h1" not in c and "leg2|bracket" not in c  # a second leg can't be sold an hour in
    assert c["open|d5"] == round((1.0 / 1.005 - 1) * 100, 2)
    short = {"edge": {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": -1, "close": -2.0, "d1": -4.0}}}}
    assert combos(short) == {"alert|close": 2.0, "alert|d1": 4.0, "alert|close+d1": 3.0}  # and half at each
    assert combos({"edge": {}}) == {}


def history(eng, n=12):
    """Contract wins that keep going into the next day, more so from the second leg."""
    for i in range(n):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (n + 3 - i)
        a.edge = {"refs": {"ACME": 100.0}, "leg2": {"px": 101.0},
                  "outcome": {"ACME": {"sign": 1, "h1": 0.5, "close": 1.0, "d1": 3.0 if i % 4 else -1.0}}}  # fmt: skip
        eng.storage.add_alert(a)


def test_the_best_pair_per_kind(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    r = combo_rates(eng)["contract"]
    assert (
        r["best"] == "alert|d1"
        and r["styles"][DEFAULT]["avg"] == 1.0
        and r["styles"]["alert|d1"]["avg"] == 2.0
    )
    assert r["styles"]["leg2|d1"]["avg"] < r["styles"]["alert|d1"]["avg"]  # paid up for the second leg
    assert (
        r["holdout"]["held"] is True and r["proven"] is True
    )  # its range starts above the plain way's +1.0%
    assert combo_line(r) == (
        "The playbook for contract win news here, in and out together: at the alert, out the next day "
        "(+2.0% on average, 75% its way, 12), likely +1.3% to +2.7%; the plain way (at the alert, out at the close) +1.0%."
    )


def test_the_autopilot_trades_the_pair():
    e = {**rec("leg2", "close"), "combo_rec": {"best": "alert|d1", "in": "alert", "out": "d1", "styles": {"alert|d1": {"avg": 2.0}},
                                               "kind": "contract", "label": "Contract win"}}  # fmt: skip
    assert playbook_in({"edge": e}) == "alert" and playbook_out({"edge": e}) == "d1"
    assert playbook_exit({"edge": e}) == ("d1", False)
    assert playbook_in({"edge": rec("leg2", "close")}) == "leg2"  # no pair graded: its kind's marginal picks
    p = promise(e)
    assert p["promise"] == 2.0 and p["in"] == "alert" and p["out"] == "d1"


async def test_the_pair_rides_the_alert(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    history(eng)
    eng.refresh_base_rates()
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    cr = eng.storage.get_alert(a.id)["edge"]["combo_rec"]
    assert cr["best"] == f"{cr['in']}|{cr['out']}" and cr["kind"] == "contract"
    from news247.combos import NAMES

    assert cr["name"] == NAMES[cr["best"]]  # the sheet reads it without the Watch board loaded
    from news247.playbook import playbook

    assert any(
        r.startswith("The playbook for contract win news here, in and out together")
        for r in playbook(eng, "contract")["rules"]
    )


# --------------------------------------------------------------------------- the second headline


def headline(eng, title, hours_ago, close=None, sign=1):
    a = news(title)
    a.created = time.time() - hours_ago * 3600
    if close is not None:
        a.edge = {"outcome": {"ACME": {"sign": sign, "close": close}}}
    eng.storage.add_alert(a)
    return a


def test_the_previous_headline(tmp_path):
    eng = semis_engine(tmp_path)
    old = headline(eng, "Acme wins Army contract", 24 * 10)  # outside the week
    first = headline(eng, "Acme wins Navy contract", 50)
    echo = headline(eng, "Acme wins Navy contract, report says", 49.9)  # the same story from another outlet
    a = headline(eng, "Acme wins $2B Pentagon contract", 0.0)
    assert previous(eng, a.to_dict())["id"] == echo.id
    assert previous(eng, echo.to_dict()) is None  # 6 minutes after the first: the same story, not a repeat
    assert previous(eng, first.to_dict()) is None  # the Army contract was 8 days before: outside the week
    eng.repeat_rates = {"contract": {"kind": "contract", "label": "Contract win", "first": {"n": 30, "hit": 0.61, "avg": 1.2},
                                     "repeat": {"n": 21, "hit": 0.38, "avg": -0.4}}}  # fmt: skip
    s = stamp(eng, a.to_dict())
    assert s["nth"] == 2 and s["same"] and s["hours"] == 49.9  # the Navy story twice counts once
    assert (
        s["line"]
        == "The 2nd headline on ACME in 2 days. Repeat contract win headlines went their way 38% here (21); first ones 61% (30)."
    )
    assert stamp(eng, old.to_dict()) is None
    x = features({**a.to_dict(), "edge": {"repeat": s}}, None)
    assert x["repeat=same"] == 1.0


def test_firsts_against_repeats(tmp_path):
    eng = semis_engine(tmp_path)
    t = 0
    for i in range(10):  # ten stories: a first that works, and a repeat two days on that doesn't
        headline(eng, f"Acme wins contract {i}", 24 * 200 - t, close=2.0)
        headline(eng, f"Acme wins contract follow-up {i}", 24 * 200 - t - 48, close=-1.0 if i < 7 else 1.0)
        t += 24 * 15
    r = repeat_rates(eng, days=365)["contract"]
    assert r["first"]["n"] == 10 and r["first"]["hit"] == 1.0
    assert r["repeat"]["n"] == 10 and r["repeat"]["hit"] == 0.3 and r["same"]["n"] == 10
    assert r["after_win"]["n"] == 10 and r["after_loss"] is None
    assert line({"symbol": "ACME", "hours": 5.0, "nth": 2}) == "The 2nd headline on ACME in 5 hours."


async def test_the_boards(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/combos"), headers=h) as r:
                body = await r.json()
                row = next(x for x in body["rows"] if x["kind"] == "contract")
                assert (
                    row["best"] == "alert|d1"
                    and row["top"][0][0] == "alert|d1"
                    and body["default"] == DEFAULT
                )
            async with s.get(srv.make_url("/api/repeats"), headers=h) as r:
                assert (await r.json()) == {"rows": []}
    finally:
        await srv.close()
        await eng.http.close()
