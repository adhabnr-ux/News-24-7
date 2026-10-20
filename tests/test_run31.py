"""Run 31: tune my pushes (the last month replayed through every edge bar and budget) and equity
curves (your calls, each autopilot and every alert, with drawdowns, beside SPY)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.equity import curve, equity
from news247.tune import TMP, tune, tune_cached
from news247.web.server import WebServer

from .test_desk import DEV
from .test_personal import semis_engine
from .test_run15 import graded

ET = ZoneInfo("America/New_York")
DAY = 86400.0


def month(eng):
    """Three days of HIGH alerts: contract wins with a 80% record that mostly worked, upgrades with
    a 40% record that mostly didn't."""
    now = time.time()
    for i in range(12):
        for key, label, hit, r in (("contract", "Contract win", 0.8, [2.0, 1.5, 1.0, -0.5][i % 4]),
                                   ("upgrade", "Analyst upgrade", 0.4, [-1.0, -1.5, 0.5, -1.2][i % 4])):  # fmt: skip
            a = graded(f"{label} alert {i}", r)
            a.created = now - DAY * (1 + i % 3)
            a.edge["catalyst"] = {"key": key, "label": label, "band": "mid"}
            a.edge["cat_base"] = {
                "label": label,
                "band_label": "$2-10B",
                "close": {"n": 20, "hit": hit, "median": 0.5},
                "typical": 1.5,
            }
            eng.storage.add_alert(a)
    return now


def test_every_setting_replayed_on_the_last_month(tmp_path):
    eng = semis_engine(tmp_path)
    now = month(eng)
    eng.desk.apply(DEV, "watch_add", {"symbol": "GAMA"})  # a profile, settings untouched
    r = tune(eng, DEV, 30, now)
    assert r["sessions"] == 3 and len(r["options"]) == 28  # 7 edge bars (the call is one) x 4 budgets
    cur = r["current"]
    assert (cur["n"], cur["per_day"], cur["label"]) == (24, 8.0, "any record, no budget") and cur[
        "avg"
    ] == pytest.approx(0.1, abs=0.01)
    q = r["quality"]
    assert (q["edge_min"], q["budget"], q["n"], q["per_day"], q["avg"]) == ("hit50", 0, 12, 4.0, 1.0)
    assert q["vs_avg"] == pytest.approx(0.9, abs=0.01) and r["balanced"]["edge_min"] == "hit50"
    assert TMP not in eng.desk.profiles and eng.desk.get(DEV)["prefs"]["edge_min"] is None  # nothing changed
    eng.desk.apply(DEV, "prefs", {"edge_min": q["edge_min"], "budget": q["budget"]})
    again = tune(eng, DEV, 30, now)
    assert again["current"]["avg"] == 1.0 and again["quality"] is None  # already the best: nothing to suggest


def test_tuning_is_kept_until_the_settings_change(tmp_path):
    eng = semis_engine(tmp_path)
    now = month(eng)
    eng.desk.apply(DEV, "watch_add", {"symbol": "GAMA"})
    first = tune_cached(eng, DEV, now)
    assert tune_cached(eng, DEV, now + 60) is first
    eng.desk.apply(DEV, "prefs", {"edge_min": "hit60"})
    assert tune_cached(eng, DEV, now + 120) is not first
    assert tune(eng, "dev_nobody_here1", 30, now)["options"] == []


# --------------------------------------------------------------------------- equity curves


def test_a_curve_and_its_drawdown():
    c = curve([(1, 2.0), (2, -3.0), (3, 1.0), (4, 4.0)])
    assert (c["n"], c["total"], c["max_dd"], c["win"], c["avg"]) == (4, 4.0, -3.0, 0.75, 1.0)
    assert c["sharpe"] == pytest.approx(0.39, abs=0.01) and c["points"] == [
        [1, 2.0],
        [2, -1.0],
        [3, 0.0],
        [4, 4.0],
    ]
    assert (c["best"], c["worst"]) == (4.0, -3.0)
    assert curve([]) == {"n": 0, "points": []} and curve([(1, 1.0)])["sharpe"] is None
    long = curve([(i, 0.1) for i in range(1000)])
    assert len(long["points"]) <= 130 and long["points"][-1] == [999, 100.0]


def test_your_calls_the_autopilots_every_alert_and_spy(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    eng.desk.apply(DEV, "autopilot_add", {"catalyst": "contract", "exit": "close"})
    rule = eng.desk.get(DEV)["autopilots"][0]
    eng.desk.get(DEV)["closed"] = [
        {"id": "a", "symbol": "ACME", "direction": "long", "ts": now - 5 * DAY, "closed": now - 4 * DAY, "pnl_pct": 3.0},
        {"id": "b", "symbol": "BETA", "direction": "long", "ts": now - 4 * DAY, "closed": now - 3 * DAY, "pnl_pct": -2.0},
        {"id": "c", "symbol": "ACME", "direction": "long", "ts": now - 3 * DAY, "closed": now - 2 * DAY, "pnl_pct": 1.5, "auto": rule["id"]},
    ]  # fmt: skip
    for i, r in enumerate([1.0, -0.5]):
        a = graded(f"Acme headline {i}", r)
        a.created = now - (3 - i) * DAY
        eng.storage.add_alert(a)
    for back, chg in ((4, 0.5), (3, -1.0), (2, 1.5)):
        d = datetime.fromtimestamp(now - back * DAY, ET).date().isoformat()
        eng.storage.set_setting(f"memory:{d}", json.dumps({"indexes": {"SPY": chg}}))
    eq = equity(eng, DEV, 90, now)
    assert (eq["yours"]["n"], eq["yours"]["total"], eq["yours"]["max_dd"]) == (2, 1.0, -2.0)
    assert [(x["label"], x["n"], x["total"]) for x in eq["autopilots"]] == [(rule["label"], 1, 1.5)]
    assert eq["every_alert"]["n"] == 2 and eq["every_alert"]["total"] == 0.5
    assert (eq["spy"]["n"], eq["spy"]["total"], eq["spy"]["max_dd"]) == (3, 1.0, -1.0)


async def test_run31_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    month(eng)
    eng.desk.apply(DEV, "watch_add", {"symbol": "GAMA"})
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/tune"), headers=h) as r:
                assert (await r.json())["quality"]["edge_min"] == "hit50"
            async with s.get(srv.make_url("/api/equity"), headers=h) as r:
                body = await r.json()
                assert body["yours"]["n"] == 0 and body["every_alert"]["n"] == 24
            async with s.get(srv.make_url("/api/tune"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
