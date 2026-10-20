"""Run 40: the news climate (each kind's last two weeks against its record, flagged hot or cold only
beyond chance) and your exits against the plan's (each closed call replayed with its alert's plan)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.climate import climate_board, climate_for, climate_line, climate_rates, z_score
from news247.planvs import plan_vs, replay
from news247.precheck import pre_trade
from news247.thecall import the_call
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

ET = ZoneInfo("America/New_York")


def graded(eng, title, sym, closes, days_ago, now):
    for i, close in enumerate(closes):
        a = news(f"{title} number {days_ago} {i}", tickers=(sym,))
        a.created = now - days_ago * 86400 - i * 600
        a.edge = {"refs": {sym: 100.0}, "outcome": {sym: {"close": close, "sign": 1}}}
        eng.storage.add_alert(a)


def history(eng, now):
    graded(eng, "Acme wins contract", "ACME", [1.0] * 18 + [-1.0] * 12, 40, now)  # before: 60% its way
    graded(eng, "Acme wins contract", "ACME", [1.0] + [-1.0] * 9, 3, now)  # lately: 10%
    graded(
        eng, "Beta downgraded to sell at Citi", "BETA", [-1.0] * 15 + [1.0] * 15, 40, now
    )  # before: 50% (a short's way: raw)


# --------------------------------------------------------------------------- the climate


def test_the_z_score():
    assert z_score(0.1, 10, 0.6, 30) == pytest.approx(-2.74, abs=0.01)
    assert z_score(0.5, 10, 0.5, 30) == 0.0 and z_score(0.5, 0, 0.5, 30) == 0.0


def test_a_kind_gone_cold_is_flagged(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    history(eng, now)
    rates = climate_rates(eng, now=now)
    c = rates["contract"]
    assert c["state"] == "cold" and c["recent"]["hit"] == 0.1 and c["before"]["hit"] == 0.6 and c["z"] < -1.64
    assert rates["downgrade"]["state"] == "normal" and rates["downgrade"]["recent"] is None  # nothing lately
    assert (
        climate_line(c)
        == "🧊 Cold lately: contract win news went its way 10% in the last two weeks (10) vs 60% before (30)"
    )
    assert climate_for(rates, "contract")["state"] == "cold" and climate_for(rates, "downgrade") is None
    assert climate_for(rates, None) is None
    assert {r["key"] for r in climate_board(rates)["rows"] if r["state"] == "cold"} == {
        "*",
        "contract",
    }  # every alert too: only contract news lately


def test_a_small_gap_or_a_thin_record_isnt_flagged(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    graded(eng, "Acme wins contract", "ACME", [1.0] * 18 + [-1.0] * 12, 40, now)
    graded(eng, "Acme wins contract", "ACME", [1.0] * 5 + [-1.0] * 5, 3, now)  # 50% vs 60%: within chance
    assert climate_rates(eng, now=now)["contract"]["state"] == "normal"
    eng2 = semis_engine(tmp_path / "b")
    graded(eng2, "Acme wins contract", "ACME", [1.0] * 18 + [-1.0] * 12, 40, now)
    graded(eng2, "Acme wins contract", "ACME", [-1.0] * 5, 3, now)  # 0% but only 5 lately
    assert climate_rates(eng2, now=now)["contract"]["state"] == "normal"


async def test_a_cold_kind_is_named_and_counts_against(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng, time.time())
    eng.refresh_base_rates()
    a = news("Acme wins $900M Navy contract")
    await eng.publish(a, push=False)
    assert a.edge["climate"]["state"] == "cold"
    assert "cold lately (10% in two weeks)" in a.edge["call"]["against"]
    body = eng.webpush.payload(a)["body"]
    assert (
        "🧊 Cold lately: contract win news went its way 10% in the last two weeks (10) vs 60% before (30)"
        in body
    )
    eng.storage.add_alert(a)
    chk = next(c for c in pre_trade(eng, DEV, "ACME", "long", a.id)["checks"] if c["key"] == "climate")
    assert chk["tone"] == "warn" and chk["text"].startswith("🧊 Cold lately")
    hot = the_call({"climate": {"state": "hot", "recent": {"hit": 0.9, "n": 10}}, "odds": {"p": 0.62}})
    assert hot["verb"] == "act" and "hot lately (90% in two weeks)" in hot["for"]


# --------------------------------------------------------------------------- your exits vs the plan's

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=ET).timestamp()  # a Wednesday morning


def alert(mfe, mae, close, direction="up", created=T0):
    a = news("Acme wins contract", direction=direction)
    a.created = created
    sign = 1 if direction == "up" else -1
    stop, target = (98.0, 104.0) if sign > 0 else (102.0, 96.0)
    a.edge = {"refs": {"ACME": 100.0}, "plan": {"dir": direction, "symbol": "ACME", "entry": 100.0, "stop": stop, "target": target, "rr": 2.0},
              "exc": {"mfe": mfe, "mae": mae}, "outcome": {"ACME": {"close": close * sign, "sign": sign}}}  # fmt: skip
    return a.to_dict()


def call(pnl, entry=100.0, direction="long", ts=T0 + 120, closed=T0 + 3 * 3600):
    return {"id": "c", "symbol": "ACME", "direction": direction, "entry": entry, "exit": None, "ts": ts, "closed": closed,
            "pnl_pct": pnl}  # fmt: skip


def test_replaying_the_plan():
    assert replay(call(0.5), alert(1.0, -2.5, 0.5)) == {
        "plan": -2.0,
        "yours": 0.5,
        "how": "stopped",
        "ambiguous": False,
    }
    assert replay(call(1.0), alert(4.5, -1.0, 3.0)) == {
        "plan": 4.0,
        "yours": 1.0,
        "how": "target",
        "ambiguous": False,
    }
    assert replay(call(1.0), alert(2.0, -1.0, 1.5)) == {
        "plan": 1.5,
        "yours": 1.0,
        "how": "close",
        "ambiguous": False,
    }
    assert (
        replay(call(1.0), alert(5.0, -3.0, 1.5))["how"] == "stopped"
        and replay(call(1.0), alert(5.0, -3.0, 1.5))["ambiguous"]
    )
    # from the call's own entry: in at 100.4, the target at 104 is +3.59%
    assert replay(call(1.0, entry=100.4), alert(4.5, -1.0, 3.0))["plan"] == 3.59
    short = replay(call(1.2, direction="short"), alert(4.2, -0.5, 2.0, direction="down"))
    assert short == {"plan": 4.0, "yours": 1.2, "how": "target", "ambiguous": False}
    assert replay(call(1.0, entry=101.0), alert(4.5, -1.0, 3.0)) is None  # 1% from the alert's price
    assert replay(call(1.0, ts=T0 + 3600), alert(4.5, -1.0, 3.0)) is None  # an hour later
    assert replay(call(1.0, closed=T0 + 86400 * 2), alert(4.5, -1.0, 3.0)) is None  # held past its session
    assert replay(call(1.0, direction="short"), alert(4.5, -1.0, 3.0)) is None  # against the plan
    assert replay(call(1.0), {**alert(4.5, -1.0, 3.0), "edge": {}}) is None


def test_your_exits_against_the_plans(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    closed = []
    for i, (mfe, mae, close, pnl) in enumerate(
        [(4.5, -1.0, 3.0, 1.0)] * 4 + [(2.0, -1.0, 1.5, 0.5), (1.0, -2.5, 0.5, 0.2)]
    ):
        a = news(f"Acme wins contract {i}")
        a.created = T0 - i * 86400 * 7
        a.edge = alert(mfe, mae, close, created=a.created)["edge"]
        eng.storage.add_alert(a)
        closed.append(
            {**call(pnl, ts=a.created + 120, closed=a.created + 3 * 3600), "id": f"c{i}", "alert_id": a.id}
        )
    closed.append({**call(0.3), "id": "manual", "alert_id": ""})  # no alert: not counted, not skipped
    closed.append({**call(0.3, entry=103.0, ts=T0 + 60), "id": "far", "alert_id": a.id})  # can't be replayed
    p["closed"] = closed
    r = plan_vs(eng, DEV, now=T0 + 86400)
    assert r["n"] == 6 and r["skipped"] == 1
    assert (r["yours"], r["plan"]) == (0.78, 2.58) and r["how"] == {"stopped": 1, "target": 4, "close": 1}
    assert r["verdict"] == "plan" and r["plan_better"] == 0.83
    assert r["line"] == "The plan's exits would have done better: +2.58% a call vs your +0.78% (6)"


async def test_run40_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng, time.time())
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/climate"), headers=h) as r:
                assert (await r.json())["rows"][0]["state"] == "cold"
            async with s.get(srv.make_url("/api/planvs"), headers=h) as r:
                assert (await r.json())["n"] == 0
            async with s.get(srv.make_url("/api/planvs"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
