"""Run 69: the playbook 5 minutes late (alert pairs re-graded from where the stock stood 5 minutes
in, and a too-fast-to-trade-by-hand mark) and the playbook's day, written up (the board's paragraph
and the 16:10 ET push)."""

from __future__ import annotations

import time

from news247.combos import combo_line, combo_rates, late
from news247.journal import day, send_day, story

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run60 import et
from .test_run64 import pair

# --------------------------------------------------------------------------- 5 minutes late


def test_one_alert_five_minutes_late():
    a = {
        "edge": {
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "m5": 2.0, "close": 3.0, "d1": 1.0}},
        }
    }
    assert late(a) == {"close": round((1.03 / 1.02 - 1) * 100, 2), "d1": round((1.01 / 1.02 - 1) * 100, 2)}
    assert late({"edge": {"outcome": {"ACME": {"sign": 1, "close": 3.0}}}}) == {}  # no 5-minute print


def fast_news(eng):
    """Contract wins that pop in the first 5 minutes and then drift: all the edge is in the first minutes."""
    for i in range(12):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (20 - i)
        a.edge = {"outcome": {"ACME": {"sign": 1, "m5": 2.5, "close": 2.0 if i % 3 else 1.0}}}
        eng.storage.add_alert(a)


def test_a_kind_too_fast_to_trade_by_hand(tmp_path):
    eng = semis_engine(tmp_path)
    fast_news(eng)
    r = combo_rates(eng)["contract"]
    assert (
        r["best"] == "alert|close"
        and r["late"]["n"] == 12
        and r["late"]["avg"] < 0
        and r["late"]["fast"] is True
    )
    assert combo_line(r).endswith("too fast to trade by hand.")


async def test_the_late_grade_rides_the_alert(tmp_path):
    eng = semis_engine(tmp_path)
    fast_news(eng)
    eng.refresh_base_rates()
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    cr = eng.storage.get_alert(a.id)["edge"]["combo_rec"]
    assert cr["late"]["fast"] is True and cr["late"]["n"] == 12


# --------------------------------------------------------------------------- the playbook's day


def rows():
    return [
        {"symbol": "ACME", "status": "out", "in": "alert", "pnl": 3.1, "plain": 2.0, "fresh": True},
        {"symbol": "BETA", "status": "out", "in": "leg2", "pnl": -1.2, "plain": -0.5, "fresh": True},
        {
            "symbol": "DLTA",
            "status": "in",
            "in": "leg2",
            "pnl": 0.5,
            "plain": 0.4,
            "fresh": True,
            "gb": {"exit": True},
        },  # fmt: skip
        {"symbol": "GAMA", "status": "pass", "in": "alert", "fresh": True},
        {"symbol": "EPSI", "status": "skipped", "in": "patient", "fresh": True},
        {
            "symbol": "ZETA",
            "status": "out",
            "in": "alert",
            "pnl": -0.2,
            "fresh": True,
            "stall": {"exit": True},
        },
        {
            "symbol": "ETA",
            "status": "in",
            "in": "alert",
            "pnl": 9.0,
            "plain": 1.0,
            "fresh": False,
        },  # an older hold
    ]


def test_the_day_in_a_paragraph():
    d = day({"rows": rows()})
    assert (
        d["n"] == 6
        and d["took"] == 4
        and d["ways"] == {"alert": 2, "leg2": 2}
        and d["passed"] == 1
        and d["never"] == 1
    )
    assert d["avg"] == round((3.1 - 1.2 + 0.5 - 0.2) / 4, 2) and d["plain"] == round((2.0 - 0.5 + 0.4) / 3, 2)
    assert d["text"] == (
        "The playbook's day: 6 directional alerts. It took 4 (2 at the alert, 2 on second legs), passed on 1 (kinds "
        "that don't pay here) and 1 entry never came. Cut early: 1 by the time stop, 1 at the close instead of "
        "overnight. On paper it made +0.6% a call against +0.6% the plain way on the same alerts (3). "
        "Best ACME +3.1%, worst BETA -1.2%."
    )
    assert story({"n": 0}) == ""


async def test_the_1610_push(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    t = et(14, 16, 12)  # a Wednesday, 16:12 ET
    a = news("Acme wins $2B Pentagon contract")
    a.created = et(14, 11)
    a.edge = {
        "refs": {"ACME": 100.0},
        **pair("alert", "close"),
        "outcome": {"ACME": {"sign": 1, "close": 2.0}},
    }
    eng.storage.add_alert(a)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        assert await send_day(eng, et(14, 15)) == 0  # not before 16:10
        assert await send_day(eng, t) >= 1
        msg = ph.read(next(r for r in server.requests if r["path"] == "/push/a")["body"])
        assert msg["title"] == "📓 The playbook's day: +2.0% a call" and msg["body"].startswith(
            "The playbook's day: 1 directional alert."
        )
        assert msg["open"] == "/app/#a=today"
        assert await send_day(eng, t + 60) == 0  # once a day
        eng.desk.apply(DEV, "prefs", {"pb_day": False})
        assert eng.desk.get(DEV)["prefs"]["pb_day"] is False
    finally:
        await eng.http.close()
