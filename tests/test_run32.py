"""Run 32: model health (the odds model pauses itself when its live record slips, and resumes when
it recovers) and what's at stake (upcoming binary events with how both outcomes have traded)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.actions import action_center
from news247.odds import DRIFT_N, fit_odds, health, predict
from news247.stakes import event_stakes
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run15 import graded
from .test_run23 import history, noon

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- model health


def test_health_reads_the_last_forty_live_alerts():
    assert health([(0.7, 1)] * 10, 0.5) == {"n": 10, "need": DRIFT_N, "paused": False}
    good = [(0.7, 1)] * 28 + [(0.7, 0)] * 12  # said 70%, 70% happened
    h = health(good, 0.5)
    assert not h["paused"] and h["skill"] > 0 and h["gap"] == 0.0
    over = [(0.9, 1)] * 16 + [(0.9, 0)] * 24  # said 90%, 40% happened
    h = health(over, 0.5)
    assert h["paused"] and h["why"] == "its last 40 live alerts did worse than guessing the plain hit rate"
    # it ranks well (the 90%s mostly won, the 60%s mostly lost) but runs 18 points high overall
    drift = [(0.9, 1)] * 20 + [(0.9, 0)] * 4 + [(0.6, 1)] * 4 + [(0.6, 0)] * 12
    h = health(drift, 0.5)
    assert h["paused"] and h["skill"] > 0
    assert h["why"] == "its odds ran 18 points above what happened over its last 40 live alerts"
    assert not health(over[:10] + good, 0.5)["paused"]  # only the last 40 count


def slipping(eng):
    """The usual history, plus 40 older alerts that went out saying 90% and went their way 40%."""
    history(eng)
    for i in range(40):
        a = graded(f"Acme hosts investor day {i}", 1.0 if i % 5 < 2 else -1.0)
        a.created = noon(170)
        a.edge["odds"] = {"p": 0.9, "n": 100}
        eng.storage.add_alert(a)


async def test_the_model_pauses_itself_and_keeps_a_shadow(tmp_path):
    eng = semis_engine(tmp_path)
    slipping(eng)
    st = fit_odds(eng)
    assert st["test_trusted"] and st["paused"] and not st["trusted"]
    assert st["health"]["why"].startswith("its last 40 live alerts did worse")
    a = news("Acme wins $900M Navy contract")
    a.created = noon()
    assert predict(st, a.to_dict(), None) is None and predict(st, a.to_dict(), None, shadow=True)["p"] > 0.5
    eng.odds = st
    await eng.publish(a, push=False)
    edge = eng.storage.get_alert(a.id)["edge"]
    assert "odds" not in edge and edge["odds_shadow"]["p"] > 0.5  # kept, not shown: how it recovers
    # phones leaning on the odds hear about it; others don't
    eng.desk.apply(DEV, "prefs", {"edge_min": "odds60"})
    eng.desk.apply(DEV2, "watch_add", {"symbol": "GAMA"})
    item = next(i for i in action_center(eng, DEV)["items"] if i["kind"] == "model")
    assert (
        item["title"] == "The odds model is paused" and "aren't filtering until it recovers" in item["detail"]
    )
    assert not any(i["kind"] == "model" for i in action_center(eng, DEV2)["items"])


def test_a_healthy_model_stays_in_use(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    st = fit_odds(eng)
    assert st["trusted"] and not st["paused"] and st["health"] == {"n": 0, "need": DRIFT_N, "paused": False}


# --------------------------------------------------------------------------- what's at stake


def stakes_history(eng):
    for i, r in enumerate([12.0, 8.0, 15.0, -2.0, 10.0]):
        eng.storage.add_alert(graded(f"FDA approves Gamma drug {i}", r, sym="GAMA"))
    for i, r in enumerate([30.0, 25.0, 40.0, -5.0]):  # its way for a short: the stock fell
        eng.storage.add_alert(graded(f"FDA rejects Gamma drug {i}", r, sign=-1, sym="GAMA"))
    eng.refresh_base_rates()


def test_each_upcoming_event_with_both_branches(tmp_path):
    eng = semis_engine(tmp_path)
    stakes_history(eng)
    now = time.time()
    today = datetime.fromtimestamp(now, ET).date()
    d5 = (today + timedelta(days=5)).isoformat()
    eng.storage.add_catalyst({"id": f"GAMA:{d5}:pdufa", "date": d5, "time": "", "title": "GAMA · FDA decision (PDUFA)",
                              "kind": "binary", "event": "pdufa", "symbol": "GAMA", "headline": "x"})  # fmt: skip
    far = (today + timedelta(days=30)).isoformat()
    eng.storage.add_catalyst({"id": f"GAMA:{far}:pdufa", "date": far, "time": "", "title": "later", "kind": "binary",
                              "event": "pdufa", "symbol": "GAMA", "headline": "x"})  # fmt: skip
    eng.live_quotes.update("GAMA", 20.0, 20.0, now - 5, "tape")
    eng.implied_cache["GAMA"] = (now, {"rows": [{"expiry": d5, "move_pct": 25.0}]})
    eng.desk.apply(DEV, "hold_set", {"symbol": "GAMA", "shares": 100})
    d3 = (today + timedelta(days=3)).isoformat()
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(
        lookup=lambda s, t=None: {"date": d3, "when": "after the close", "in_days": 3} if s == "ACME" else None))  # fmt: skip
    r = event_stakes(eng, DEV, now)
    assert [(x["symbol"], x["event"], x["in_days"]) for x in r["rows"]] == [
        ("ACME", "earnings", 3),
        ("GAMA", "pdufa", 5),
    ]
    g = r["rows"][1]
    assert (g["mine"], g["value"], g["implied_pct"]) == (True, 2000.0, 25.0)
    yes, no = g["branches"]
    assert (yes["label"], yes["n"], yes["close"]) == ("an approval", 5, 10.0)
    assert (no["label"], no["n"], no["close"]) == ("a rejection or delay", 4, -27.5)  # the stock's move: down
    acme = r["rows"][0]
    assert [b["label"] for b in acme["branches"]] == ["a beat", "a miss", "guidance up", "guidance down"]
    assert all(b["n"] == 0 for b in acme["branches"])  # no record of those yet: said so, not guessed
    assert event_stakes(eng, DEV2, now)["rows"][0]["mine"] is False  # another phone sees the PDUFA, not yours


async def test_run32_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    stakes_history(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/stakes"), headers={"Authorization": "Bearer tok"}) as r:
                body = await r.json()
                assert (body["rows"], body["days"], body["record"]) == ([], 14, {"n": 0, "kinds": []})
            async with s.get(srv.make_url("/api/odds"), headers={"Authorization": "Bearer tok"}) as r:
                assert (await r.json())["ready"] is False
    finally:
        await srv.close()
        await eng.http.close()
