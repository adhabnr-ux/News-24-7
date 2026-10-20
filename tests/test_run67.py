"""Run 67: the stall rule (a time stop graded from where each alert stood 30 minutes in) and crowded
days (the 2nd and later alert in a sector the same day, the same way, graded against the first, with
a guard so the playbook autopilot doesn't stack one bet three times)."""

from __future__ import annotations

import time

from news247.crowd import crowd_rates, line, spot, stamp, too_crowded
from news247.odds import features
from news247.stall import stall_rates, state_of, verdict

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run64 import pair

# --------------------------------------------------------------------------- the stall rule


def test_how_its_first_half_hour_went():
    assert state_of(0.5) == "working" and state_of(0.1) == "stalled" and state_of(-0.2) == "stalled"
    assert state_of(-0.4) == "against" and state_of(None) is None


def stalls(eng):
    """Contract wins: stalled at 30 minutes, they drifted lower; working ones kept going."""
    for i in range(20):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (30 - i)
        stalled = i % 2 == 0
        a.edge = {
            "outcome": {
                "ACME": {"sign": 1, "m30": 0.1 if stalled else 1.0, "close": -0.5 if stalled else 2.0}
            }
        }
        eng.storage.add_alert(a)


def test_the_rest_of_the_day_by_its_first_half_hour(tmp_path):
    eng = semis_engine(tmp_path)
    stalls(eng)
    r = stall_rates(eng)["contract"]
    assert r["stalled"]["n"] == 10 and r["stalled"]["avg"] == round((0.995 / 1.001 - 1) * 100, 2)
    assert r["working"]["n"] == 10 and r["working"]["hit"] == 1.0
    v = verdict({"contract": r}, "contract", "stalled")
    assert (
        v["exit"]
        and v["line"]
        == "Stalled 30 minutes in; alerts like it went -0.6% the rest of the day here (0% its way, 10): the time stop says out."
    )
    assert not verdict({"contract": r}, "contract", "working")["exit"]


async def test_the_autopilot_time_stop(tmp_path, server):
    from .test_run3 import phones

    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    stalls(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    rid = eng.desk.get(DEV)["autopilots"][0]["id"]
    now = time.time()
    a = news("Acme wins $2B Pentagon contract")
    a.created = now - 1900  # 31 minutes ago
    a.edge = {
        "refs": {"ACME": 100.0},
        "catalyst": {"key": "contract", "label": "Contract win", "band": "large"},
    }
    eng.storage.add_alert(a)
    eng.desk.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "alert_id": a.id, "entry": 100.0}
    )
    t = eng.desk.get(DEV)["tracked"][-1]
    t["auto"], t["pb"] = rid, {"in": "alert", "out": "close"}
    eng.live_quotes.update("ACME", 100.1, 99.0, now - 5, "tape")
    await eng.http.start()
    try:
        phones(eng, server)
        assert await eng.autopilot_stall(now) == 1
        assert eng.desk.get(DEV)["closed"][0]["lesson"].startswith(
            "autopilot: the time stop: stalled 30 minutes in"
        )
        assert await eng.autopilot_stall(now + 60) == 0  # checked once
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- crowded days


def test_the_spots():
    assert [spot(n) for n in (0, 1, 2, 5)] == ["first", "second", "crowded", "crowded"]
    c = {"sector": "Technology", "n_before": 2, "spot": "crowded", "before": ["ACME", "BETA"],
         "rec": {"group": "*", "here": {"n": 31, "hit": 0.44, "avg": -0.2}, "first": {"n": 120, "hit": 0.61, "avg": 0.9}}}  # fmt: skip
    assert line(c) == ("The 3rd technology alert today, the same way (after ACME, BETA). The third or later went their way "
                       "44% here (31); the first 61% (120).")  # fmt: skip
    assert line({**c, "spot": "first", "n_before": 0}) == ""


def sector(eng):
    return eng.universe.get("ACME").sector


def crowded_days(eng):
    """Ten days with three semis alerts each: the first works, the later ones don't."""
    for d in range(10):
        for j, sym in enumerate(("ACME", "BETA", "DLTA")):
            a = news(f"{sym} wins contract {d}", tickers=(sym,))
            a.created = time.time() - 86400 * (20 - d) + 600 * j
            a.edge = {"outcome": {sym: {"sign": 1, "close": 2.0 if j == 0 else -1.0}}}
            eng.storage.add_alert(a)


def test_the_record_by_spot(tmp_path):
    eng = semis_engine(tmp_path)
    crowded_days(eng)
    r = crowd_rates(eng)
    s = sector(eng)
    assert r["*"]["first"] == {"n": 10, "hit": 1.0, "avg": 2.0}
    assert r["*"]["second"]["n"] == 10 and r["*"]["crowded"]["hit"] == 0.0 and r[s]["crowded"]["n"] == 10


def test_a_new_alert_knows_its_spot(tmp_path):
    eng = semis_engine(tmp_path)
    crowded_days(eng)
    eng.crowd_rates = crowd_rates(eng)
    now = time.time()
    for j, sym in enumerate(("ACME", "BETA")):
        a = news(f"{sym} wins contract today", tickers=(sym,))
        a.created = now - 600 + j
        eng.storage.add_alert(a)
    c = news("DLTA wins contract today", tickers=("DLTA",))
    c.created = now
    st = stamp(eng, c.to_dict())
    assert st["spot"] == "crowded" and st["n_before"] == 2 and st["before"] == ["ACME", "BETA"]
    assert (
        st["rec"]["group"] == sector(eng)
        and "went their way 0% in its sector (10); the first 100% (10)" in st["line"]
    )
    x = features({**c.to_dict(), "edge": {"crowd": st}}, None)
    assert x["crowd=crowded"] == 1.0


def test_the_guard_keeps_it_one_bet(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    p = eng.desk.get(DEV)
    rid = p["autopilots"][0]["id"]
    alert = {**news("DLTA wins contract", tickers=("DLTA",)).to_dict(), "edge": pair("alert", "close")}
    assert not too_crowded(eng, p, alert, rid)
    for sym in ("ACME", "BETA"):
        p.setdefault("tracked", []).append({"id": sym, "symbol": sym, "direction": "long", "auto": rid})
    assert too_crowded(eng, p, alert, rid)
    p["tracked"][-1]["direction"] = "short"  # the other way: not the same bet
    assert not too_crowded(eng, p, alert, rid)


async def test_publish_stamps_the_crowd(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for j, sym in enumerate(("ACME", "BETA")):
        a = news(f"{sym} wins contract today", tickers=(sym,))
        a.created = now - 600 + j
        eng.storage.add_alert(a)
    c = news("DLTA wins $2B contract", tickers=("DLTA",))
    await eng.publish(c, push=False)
    cw = eng.storage.get_alert(c.id)["edge"]["crowd"]
    assert cw["spot"] == "crowded" and cw["line"].startswith("The 3rd technology alert today")
    first = news("Gama wins contract", tickers=("GAMA",))
    await eng.publish(first, push=False)
    assert "crowd" not in (
        eng.storage.get_alert(first.id)["edge"] or {}
    )  # first in its sector: nothing to flag
