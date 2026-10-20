"""Run 25: expected value per alert (the odds times how big wins and losses run) and managing an
open paper call (breakeven at +1R, a gain given back, dead money, a report while you hold it)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from news247.actions import action_center
from news247.desk import Desk
from news247.manage import check_manage, nudges
from news247.odds import fit_odds, odds_line, payoffs, predict
from news247.plans import setups_push, todays_setups
from news247.storage import Storage

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run16 import contract_history, stored
from .test_run23 import history, noon

# --------------------------------------------------------------------------- expected value


def test_payoffs_need_wins_and_losses():
    pay = payoffs(
        {"*": [2.0, 3.0, 1.0, -1.0, -2.0, -0.5], "upgrade": [2.0, 1.0, -1.0, -1.0, -3.0], "fda": [5.0, 4.0]}
    )
    assert pay["*"] == {"win": 2.0, "loss": 1.17, "n": 6}
    assert "upgrade" not in pay and "fda" not in pay  # two wins, or no losses: too few to size either


def test_each_alert_gets_its_expected_move(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)  # contract wins: +2.0% three times in four, -1.5% the rest
    st = fit_odds(eng)
    assert st["payoff"]["contract"] == {"win": 2.0, "loss": 1.5, "n": 80}
    a = news("Acme wins $900M Navy contract")
    a.created = noon()
    o = predict(st, a.to_dict(), None)
    assert o["ev"] == pytest.approx(o["p"] * 2.0 - (1 - o["p"]) * 1.5, abs=0.01) and o["ev"] > 0.5
    assert (o["win"], o["loss"], o["pay_from"]) == (2.0, 1.5, "contract win news")
    up = news("Morgan Stanley upgrades Acme to buy")
    up.created = noon()
    assert predict(st, up.to_dict(), None)["ev"] < 0  # a kind of news that mostly lost
    plain = news("Acme hosts investor day")
    plain.created = noon()
    po = predict(st, plain.to_dict(), None)
    assert po["pay_from"] == "every alert" and po["win"] == 2.0
    assert (
        odds_line(o)
        == f"Odds {round(o['p'] * 100)}% by the close · expected {o['ev']:+.1f}% (model, 160 graded)"
    )
    assert odds_line({"p": 0.6, "n": 9}) == "Odds 60% by the close (model, 9 graded)"


def test_setups_use_the_odds_when_the_model_is_in_use(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = stored(eng)
    plain = todays_setups(eng, DEV)["rows"][0]
    assert plain["odds"] is None
    eng.storage.update_alert_edge(a["id"], {"odds": {"p": 0.9, "ev": 1.4, "n": 300}})
    s = todays_setups(eng, DEV)
    row = s["rows"][0]
    assert row["odds"]["p"] == 0.9 and row["score"] > plain["score"]  # 90% beats the record's 83%
    assert setups_push(s)["body"].endswith("odds 90%, exp +1.4%")


# --------------------------------------------------------------------------- managing a call


def test_a_stop_can_trail_past_the_entry():
    d = Desk(Storage(":memory:"))
    pf = {"ACME": (time.time(), 100.0, 0.0)}.get
    d.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "stop": 95, "target": 110}, price_of=pf)
    t = d.get(DEV)["tracked"][0]
    d.apply(DEV, "track_levels", {"id": t["id"], "stop": 100})  # breakeven
    assert t["stop"] == 100.0
    d.apply(DEV, "track_levels", {"id": t["id"], "stop": 104})  # locking in a gain
    assert t["stop"] == 104.0
    with pytest.raises(ValueError, match="long's stop goes under its target"):
        d.apply(DEV, "track_levels", {"id": t["id"], "stop": 111})
    with pytest.raises(
        ValueError, match="long's stop goes under"
    ):  # a new call still starts on the right side
        d.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "stop": 105}, price_of=pf)
    d.apply(
        DEV2, "track_add", {"symbol": "ACME", "direction": "short", "stop": 105, "target": 90}, price_of=pf
    )
    s = d.get(DEV2)["tracked"][0]
    d.apply(DEV2, "track_levels", {"id": s["id"], "stop": 97})
    assert s["stop"] == 97.0
    with pytest.raises(ValueError, match="short's stop goes over its target"):
        d.apply(DEV2, "track_levels", {"id": s["id"], "stop": 89})


def calls(eng, now):
    q = eng.live_quotes
    q.update("ACME", 102.5, 100.0, now - 5, "tape")
    q.update("BETA", 50.1, 50.0, now - 5, "tape")
    q.update("GAMA", 20.0, 20.0, now - 5, "tape")
    d = eng.desk
    d.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100, "stop": 98, "target": 110}
    )
    b = news("Beta wins Army contract", tickers=("BETA",))
    b.edge = {
        "cat_base": {
            "label": "Contract win",
            "band_label": "$2-10B",
            "close": {"n": 12, "hit": 0.75, "median": 2.1},
        }
    }
    eng.storage.add_alert(b)
    d.apply(DEV, "track_add", {"symbol": "BETA", "direction": "long", "entry": 50, "alert_id": b.id})
    d.get(DEV)["tracked"][-1]["ts"] = now - 6 * 86400
    d.apply(DEV, "track_add", {"symbol": "GAMA", "direction": "short", "entry": 20})
    d.get(DEV)["tracked"].append({"id": "auto1", "symbol": "ACME", "direction": "long", "entry": 90.0, "ts": now,
                                  "auto": "r1", "exit_at": now + 3600, "stop": 89.0, "target": 95.0})  # fmt: skip
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(
        lookup=lambda s, t=None: {"date": "x", "in_days": 0, "when": "after the close"} if s == "GAMA" else None))  # fmt: skip


def test_the_nudges_on_open_calls(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    calls(eng, now)
    n = {x["kind"]: x for x in nudges(eng, DEV, now)}
    assert set(n) == {"breakeven", "dead", "event"}  # the autopilot call manages itself
    be = n["breakeven"]
    assert be["title"] == "ACME is +1.2R (+2.5%): move your stop to breakeven?"
    assert be["do"] == {
        "label": "Stop to breakeven",
        "action": "track_levels",
        "body": {"id": be["call"], "stop": 100.0},
    }
    assert n["dead"]["title"] == "BETA is flat 6 days on (+0.2%): dead money?"
    assert (
        "Contract win news usually does its move by the close (median +2.1%, 12 graded)"
        in n["dead"]["detail"]
    )
    assert n["dead"]["do"]["body"]["lesson"] == "dead money"
    assert n["event"]["title"] == "GAMA reports after the close today, and you hold a paper short"
    # one tap: the stop goes to the entry, and the nudge is done
    eng.desk.apply(DEV, be["do"]["action"], be["do"]["body"], eng.price_of)
    assert "breakeven" not in {x["kind"] for x in nudges(eng, DEV, now)}


async def test_a_gain_given_back_and_the_pushes(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        calls(eng, now)
        assert await check_manage(eng, now) == 3  # breakeven, dead money, the report
        titles = sorted(a.read(r["body"])["title"] for r in server.requests)
        assert titles[0].startswith("💤 BETA is flat") and titles[1].startswith("📅 GAMA reports")
        assert titles[2] == "🛡 ACME is +1.2R (+2.5%): move your stop to breakeven?"
        assert await check_manage(eng, now + 60) == 0  # once each
        acme = eng.desk.get(DEV)["tracked"][0]
        assert (acme["best"], acme["risk_stop"]) == (2.5, 98)
        # it slips back to +0.4%: most of the gain given back
        eng.live_quotes.update("ACME", 100.4, 100.0, now + 115, "tape")
        assert await check_manage(eng, now + 120) == 1
        msg = a.read(server.requests[-1]["body"])
        assert msg["title"] == "↘ ACME gave back most of its gain: +2.5% at best, +0.4% now"
        n = next(x for x in nudges(eng, DEV, now + 120) if x["kind"] == "giveback")
        assert n["do"]["action"] == "track_levels"  # still above the entry, still at risk: lock the entry
        assert acme["worst"] == 0.4
        # the phone turned nudges off: tracked, not pushed
        eng.desk.apply(DEV, "prefs", {"manage": False})
        eng.desk.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100, "stop": 99})
        assert await check_manage(eng, now + 180) == 0
    finally:
        await eng.http.close()


def test_nudges_are_in_needs_you(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    calls(eng, now)
    kinds = [i["kind"] for i in action_center(eng, DEV, now)["items"]]
    assert {"breakeven", "dead", "event"} <= set(kinds)
    eng.desk.apply(DEV, "watch_add", {"symbol": "GAMA"})  # watched: its report is already listed
    kinds = [i["kind"] for i in action_center(eng, DEV, now)["items"]]
    assert "event" not in kinds and "report" in kinds
