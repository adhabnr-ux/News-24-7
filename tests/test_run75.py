"""Run 75: the playbook's blind spots (slices of its own track record where it loses, in both halves,
sat out without hiding a recovery) and the tilt guard (your calls after a losing streak, graded on
your own record, flagged in the pre-trade check)."""

from __future__ import annotations

import time

from news247.autopilot import playbook_pass
from news247.blindspots import blind_spots, hit, slices, why
from news247.pbcurve import taken
from news247.precheck import pre_trade
from news247.tilt import check, now_streak, record

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run60 import et
from .test_run64 import pair

# --------------------------------------------------------------------------- blind spots


def test_the_slices_of_an_alert():
    a = {
        **news("Acme wins contract").to_dict(),
        "created": et(16, 15, 30),
        "edge": {"catalyst": {"band": "small"}},
    }
    assert slices(a) == {
        "hour": "the last hour",
        "day": "Friday",
        "dir": "long",
        "band": "small",
        "sev": "HIGH",
    }


def history(eng):
    """The playbook makes money, except on alerts in the last hour, where it loses in both halves."""
    for i in range(60):
        a = news(f"Acme wins Pentagon contract {i}")
        late = i % 2 == 0
        a.created = et(14, 15 if late else 10, 30) - 86400 * (60 - i)
        a.edge = {**pair("alert", "close"), "refs": {"ACME": 100.0},
                  "outcome": {"ACME": {"sign": 1, "close": -1.0 if late else 3.0}}}  # fmt: skip
        eng.storage.add_alert(a)


def test_a_blind_spot_is_found(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    r = blind_spots(eng, days=400, now=et(14, 18))
    assert r["overall"]["n"] == 60 and r["overall"]["avg"] == 1.0
    labels = [s["label"] for s in r["spots"]]
    assert labels == ["Time of day: the last hour"]
    s = r["spots"][0]
    assert s["n"] == 30 and s["avg"] == -1.0 and s["older"] == -1.0 and s["newer"] == -1.0
    late = {**news("Acme wins contract").to_dict(), "created": et(14, 15, 30)}
    assert hit(r, late)["dim"] == "hour" and hit(r, {**late, "created": et(14, 11)}) is None
    assert why(s).startswith(
        "a blind spot of the playbook (time of day: the last hour: -1.00% an alert on 30"
    )


def test_one_bad_stretch_isnt_a_blind_spot(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(60):  # the last hour lost only in the newer half
        a = news(f"Acme wins Pentagon contract {i}")
        late = i % 2 == 0
        a.created = et(14, 15 if late else 10, 30) - 86400 * (60 - i)
        close = (-3.0 if i > 30 else 1.0) if late else 3.0
        a.edge = {
            **pair("alert", "close"),
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "close": close}},
        }
        eng.storage.add_alert(a)
    assert blind_spots(eng, days=400, now=et(14, 18))["spots"] == []


def test_sitting_it_out_doesnt_hide_a_recovery():
    a = {"edge": {**pair("alert", "close"), "refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 2.0}},
                  "blind": {"why": "a blind spot"}}}  # fmt: skip
    assert playbook_pass(a) == "a blind spot"
    assert taken(a) == (0.0, 2.0)  # the track record: it sat out
    assert taken(a, counterfactual=True) == (2.0, 2.0)  # the blind-spot judge: what it would have made


async def test_publish_stamps_a_blind_spot(tmp_path):
    eng = semis_engine(tmp_path)
    from .test_run63 import history as pairs

    pairs(eng)
    eng.refresh_base_rates()
    eng.blind_rates = {"overall": {"n": 60, "avg": 1.0}, "spots": [{"dim": "dir", "value": "long", "label": "Direction: long",
                                                                    "n": 30, "avg": -1.0, "older": -1.0, "newer": -1.0}]}  # fmt: skip
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    got = eng.storage.get_alert(a.id)
    assert got["edge"]["blind"]["label"] == "Direction: long" and playbook_pass(got).startswith(
        "a blind spot of the playbook"
    )


# --------------------------------------------------------------------------- the tilt guard


def call(p, opened, closed, pnl, auto=None):
    p.setdefault("closed", []).append({"id": f"c{opened}", "symbol": "ACME", "direction": "long", "ts": opened,
                                       "closed": closed, "pnl_pct": pnl, **({"auto": auto} if auto else {})})  # fmt: skip


def test_your_record_after_a_streak(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    for d in range(6):  # six days: a winner, two losers, then a third call that loses too
        base = et(5, 10) + 86400 * d
        call(p, base - 900, base - 300, 2.0)
        call(p, base, base + 600, -1.0)
        call(p, base + 700, base + 1200, -1.0)
        call(p, base + 1300, base + 2000, -0.8)
        call(p, base + 3000, base + 3600, 2.0, auto="r1")  # the autopilot's don't count
    r = record(p)
    assert (
        r["0"]["n"] == 12
        and r["0"]["avg"] == 0.5
        and r["1"]["n"] == 6
        and r["2+"]["n"] == 6
        and r["2+"]["avg"] == -0.8
    )
    assert r["tilts"] is True
    assert now_streak(p, et(10, 10, 40)) == 3  # the 5th day, after its three losers


def test_the_pre_trade_check_on_a_streak(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    now = time.time()
    call(p, now - 3000, now - 2400, -1.0)
    call(p, now - 2300, now - 1800, -1.5)
    c = check(eng, DEV, now)
    assert c["tone"] == "warn" and c["text"].startswith(
        "You've closed 2 losers in a row today: a moment to step back"
    )
    r = pre_trade(eng, DEV, "ACME", "long", "", now)
    assert any(x["key"] == "tilt" for x in r["checks"])
    call(p, now - 1000, now - 500, 1.0)  # a winner ends the streak
    assert check(eng, DEV, now) is None
