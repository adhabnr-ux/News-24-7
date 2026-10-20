"""Run 16: a paper plan for every alert (stop, target, reward-to-risk, size) and the coach (what
your closed calls say about your habits)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.plans import coach, trade_plan
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run15 import graded

ET = ZoneInfo("America/New_York")


def contract_history(eng):
    for i, r in enumerate([3.0, 2.0, 4.0, -1.0, 2.5, 3.5]):
        eng.storage.add_alert(graded(f"Acme wins contract number {i}", r))
    eng.refresh_base_rates()  # contract wins, $2-10B: typical close move 2.75%


def stored(eng, title="Acme wins $900M Navy contract", pre=1.0, ref=100.0, direction="up"):
    a = news(title, direction=direction)
    a.edge = {"refs": {"ACME": ref}, **({"pre": {"ACME": pre}} if pre is not None else {})}
    eng.storage.add_alert(a)
    return eng.storage.get_alert(a.id)


def test_a_plan_from_the_catalyst_record(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    now = time.time()
    eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
    a = stored(eng)
    p = trade_plan(eng, DEV, a, now)
    assert p["ok"] and p["status"] == "live" and p["direction"] == "up"
    assert (
        p["stop"] == pytest.approx(99.0099, abs=1e-3)
        and p["stop_basis"] == "back through where it stood before the news"
    )
    assert (
        p["target"] == pytest.approx(102.75) and "contract win news, $2-10B (6 graded)" in p["target_basis"]
    )
    assert p["risk_pct"] == pytest.approx(1.48, abs=0.01) and p["reward_pct"] == pytest.approx(2.24, abs=0.01)
    assert p["rr"] == pytest.approx(1.51, abs=0.01) and "shares" not in p  # no account size: no sizing
    eng.desk.apply(DEV, "prefs", {"account": 10000, "risk_pct": 1})
    p = trade_plan(eng, DEV, a, now)
    assert (p["risk_budget"], p["shares"]) == (100.0, 67) and p["position"] == pytest.approx(6733.5)
    assert p["position_pct"] == pytest.approx(67.3, abs=0.1)
    for bad in ({"account": -5}, {"risk_pct": 3}):
        with pytest.raises(ValueError):
            eng.desk.apply(DEV, "prefs", bad)
    # no pre-news level: the stop is half the target's distance the other way
    p = trade_plan(eng, DEV, stored(eng, pre=None), now)
    assert p["stop"] == pytest.approx(98.625) and p["stop_basis"].startswith("half the target")
    # a short, from a downgrade with no record: the options market's implied move is the target
    eng.implied_cache["ACME"] = (now, {"rows": [{"expiry": "2026-10-23", "move_pct": 4.0}]})
    p = trade_plan(eng, DEV2, stored(eng, "Acme downgraded at Goldman", pre=-1.0, direction="down"), now)
    assert (
        p["direction"] == "down"
        and p["target"] == pytest.approx(96.0)
        and "options price by 2026-10-23" in p["target_basis"]
    )
    assert p["stop"] == pytest.approx(101.0101, abs=1e-3) and p["ok"]


def test_plans_say_when_there_is_nothing_to_plan(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    now = time.time()
    a = stored(eng)
    assert trade_plan(eng, DEV, a, now)["why"].startswith("No live price")
    eng.live_quotes.update("ACME", 98.0, 99.0, now - 5, "tape")
    p = trade_plan(eng, DEV, a, now)
    assert p["status"] == "invalidated" and not p["ok"]
    eng.live_quotes.update("ACME", 103.0, 99.0, now - 1, "tape")
    assert trade_plan(eng, DEV, a, now)["status"] == "done"
    flat = stored(eng, "Acme hosts investor day", direction="mixed")
    assert "doesn't lean" in trade_plan(eng, DEV, flat, now)["why"]
    nobasis = stored(eng, "Acme hosts investor day")
    eng.live_quotes.update("ACME", 100.5, 99.0, now, "tape")
    assert trade_plan(eng, DEV, nobasis, now)["why"].startswith("No basis for a target")


# --------------------------------------------------------------------------- the coach


def at(h, m, d=13):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


def call(sym, direction, pnl, opened, held_h, tags=(), stop=None, alert_id=""):
    return {"id": f"{abs(hash((sym, pnl, opened))) % 10**10:010x}", "symbol": sym, "direction": direction,
            "entry": 100.0, "ts": opened, "closed": opened + held_h * 3600, "pnl_pct": pnl, "tags": list(tags),
            "stop": stop, "target": None, "alert_id": alert_id}  # fmt: skip


def test_the_coach_reads_your_habits(tmp_path):
    eng = semis_engine(tmp_path)
    assert coach(eng, DEV)["enough"] is False and coach(eng, DEV)["need"] == 5
    # alerts whose moves ran further by the close than these calls kept
    ids = []
    for i in range(4):
        a = graded(f"Acme wins contract {i}", 5.0)
        eng.storage.add_alert(a)
        ids.append(a.id)
    closed = [
        call("ACME", "long", 1.0, at(9, 40), 0.5, tags=("breakout",), stop=98, alert_id=ids[0]),
        call("ACME", "long", 0.8, at(9, 45), 0.4, tags=("breakout",), stop=98, alert_id=ids[1]),
        call("ACME", "long", 1.2, at(9, 50), 0.6, tags=("breakout",), stop=98, alert_id=ids[2]),
        call("BETA", "long", -3.0, at(11, 0), 4.0, tags=("fade",)),
        call("BETA", "long", -2.5, at(11, 30), 5.0, tags=("fade",)),
        call("GAMA", "short", -4.0, at(12, 0), 6.0, alert_id=ids[3]),
    ]
    eng.desk.get(DEV)["closed"] = closed
    c = coach(eng, DEV)
    assert c["enough"] and c["n"] == 6
    s = c["summary"]
    assert s["win_rate"] == 0.5 and s["avg_win"] == 1.0 and s["avg_loss"] == pytest.approx(-3.17, abs=0.01)
    assert s["payoff"] == pytest.approx(0.32, abs=0.01) and s["expectancy"] == pytest.approx(-1.08, abs=0.01)
    text = " ".join(i["text"] for i in c["insights"])
    assert "Your losers are bigger than your winners" in text
    assert "You hold losers longer than winners (median 5.0 h vs 0.5 h)" in text
    assert "holding to the alert's close would have done better" in text
    assert "Your best calls: #breakout (+1.00% a call, 3); your worst: #fade (-2.75%, 2)" in text
    assert "Calls without a stop lost up to -4.00%; with one, the worst was +0.80%" in text
    assert "Calls taken in the first hour average +1.00%; later ones -3.17%" in text
    assert c["insights"][0]["weight"] >= c["insights"][-1]["weight"]
    assert [r["name"] for r in c["by"]["catalyst"]] == ["Contract win"]


async def test_run16_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = stored(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url(f"/api/plan/{a['id']}"), headers=h) as r:
                assert (await r.json())["rr"] == pytest.approx(1.51, abs=0.01)
            async with s.get(srv.make_url("/api/plan/nope"), headers=h) as r:
                assert r.status == 404
            async with s.get(srv.make_url("/api/coach"), headers=h) as r:
                assert (await r.json())["enough"] is False
            async with s.get(srv.make_url("/api/coach"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
            # take the plan: a tracked call with its stop and target already set
            async with s.post(srv.make_url("/api/desk"), json={"action": "track_add", "symbol": "ACME", "direction": "long",
                                                              "alert_id": a["id"], "stop": 99.01, "target": 102.75}, headers=h) as r:  # fmt: skip
                t = (await r.json())["tracked"][0]
                assert (t["stop"], t["target"], t["alert_id"]) == (99.01, 102.75, a["id"])
    finally:
        await srv.close()
        await eng.http.close()
