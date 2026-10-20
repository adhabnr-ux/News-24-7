"""Run 74: the playbook in the pre-trade check (pass, wait for its way in, or take it its way) and
liquidity bands (each alert's dollar volume, the plain way, 5 minutes late and the playbook graded
per band, and thin stocks sat out when even 5 minutes late lost)."""

from __future__ import annotations

import time

from news247.autopilot import playbook_pass
from news247.liquidity import band, board, liq_rates, stamp
from news247.precheck import pre_trade

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run64 import pair

# --------------------------------------------------------------------------- the pre-trade check


def check(eng, edge, direction="long"):
    a = news("Acme wins $2B Pentagon contract")
    a.edge = {"refs": {"ACME": 100.0}, **edge}
    eng.storage.add_alert(a)
    r = pre_trade(eng, DEV, "ACME", direction, a.id)
    return next((c for c in r["checks"] if c["key"] == "playbook"), None)


def styled(pin, pout, avg=1.4, plain=0.6, **extra):
    p = pair(pin, pout)
    p["combo_rec"]["styles"] = {
        f"{pin}|{pout}": {"avg": avg, "n": 20},
        "alert|close": {"avg": plain, "n": 20},
    }
    p["combo_rec"].update(extra)
    return p


def test_the_playbook_takes_it_waits_or_passes(tmp_path):
    eng = semis_engine(tmp_path)
    c = check(eng, styled("alert", "close", avg=0.6))
    assert c["tone"] == "good" and c["text"] == "The playbook takes it the plain way (+0.6% on average, 20)"
    c = check(eng, styled("alert", "d1"))
    assert (
        c["tone"] == "good"
        and c["text"]
        == "The playbook takes it, at the alert, out the next day (+1.4% on average, 20): mind the exit"
    )
    c = check(eng, styled("leg2", "close"))
    assert c["tone"] == "warn" and c["text"].startswith(
        "The playbook waits: on the second leg, out at the close (+1.4% on average, 20)"
    )
    assert c["text"].endswith("; in now and out at the close made +0.6%")
    c = check(eng, styled("alert", "close", avg=-0.4, **{"pass": True}))
    assert c["tone"] == "bad" and c["text"].startswith(
        "The playbook passes on this one: no way in and out made money"
    )
    c = check(eng, styled("alert", "close"), direction="short")
    assert c["tone"] == "warn" and c["text"].startswith("Against its news")
    assert check(eng, {}) is None  # no pair graded yet: nothing to say


# --------------------------------------------------------------------------- liquidity bands


def priced(eng, price, volume):
    """ACME's listing with a price and today's volume (listings are frozen: swap the lookup)."""
    import dataclasses

    get = eng.universe.get
    eng.universe.get = lambda s: (
        dataclasses.replace(get(s), price=price, volume=volume) if s == "ACME" else get(s)
    )


def test_the_bands():
    assert [band(x) for x in (5e5, 5e6, 5e7, 5e8, None, 0)] == ["thin", "light", "liquid", "deep", None, None]


def thin_history(eng, n=16):
    """Thin stocks: up on paper at the alert, lost by the time a person could get in."""
    for i in range(n):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (n + 2 - i)
        a.edge = {"liq": {"band": "thin", "dv": 900_000}, **pair("alert", "close"),
                  "outcome": {"ACME": {"sign": 1, "m5": 4.0, "close": 2.0}}}  # fmt: skip
        eng.storage.add_alert(a)


def test_thin_stocks_that_dont_pay_late(tmp_path):
    eng = semis_engine(tmp_path)
    thin_history(eng)
    r = liq_rates(eng)["thin"]
    assert (
        r["plain"]["avg"] == 2.0
        and r["late"]["avg"] == round((1.02 / 1.04 - 1) * 100, 2)
        and r["late"]["n"] == 16
    )
    assert r["playbook"]["n"] == 16 and r["untradeable"] is True
    assert board({"thin": r})["rows"][0]["label"] == "Thin (under $2M a day)"
    eng.liq_rates = {"thin": r}
    priced(eng, 2.0, 300_000)  # $600K a day
    s = stamp(eng, "ACME")
    assert (
        s["band"] == "thin"
        and s["pass"]
        and s["why"].startswith("a thin stock: thin names here lost even 5 minutes late")
    )
    assert playbook_pass({"edge": {**pair("alert", "close"), "liq": s}}).startswith("a thin stock")


def test_too_few_to_judge(tmp_path):
    eng = semis_engine(tmp_path)
    thin_history(eng, n=6)
    assert liq_rates(eng)["thin"]["untradeable"] is False


async def test_publish_stamps_liquidity(tmp_path):
    eng = semis_engine(tmp_path)
    priced(eng, 100.0, 1_000_000)  # $100M a day
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert eng.storage.get_alert(a.id)["edge"]["liq"] == {"dv": 100_000_000, "band": "liquid"}
