"""Run 26: market-adjusted grades (every alert also read against SPY over the same window) and the
pre-trade check (every reason for pause, the moment you tap Track)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.catalysts import catalyst_rates
from news247.dossier import excess, record_outcomes, track_record
from news247.precheck import pre_trade
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run16 import contract_history, stored

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 14, 14, 0, tzinfo=ET).timestamp()  # a Wednesday, two hours before the close
CLOSE = datetime(2026, 10, 14, 16, 0, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- market-adjusted grades


async def test_new_alerts_note_the_market_and_are_graded_against_it(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    eng.live_quotes.update("SPY", 600.0, 600.0, now - 5, "tape")
    a = news("Acme wins $900M Navy contract")
    await eng.publish(a, push=False)
    assert eng.storage.get_alert(a.id)["edge"]["mkt_ref"] == 600.0
    eng2 = semis_engine(tmp_path / "b")
    b = news("Acme wins another contract")
    await eng2.publish(b, push=False)  # no market quote: nothing noted, nothing guessed
    assert "mkt_ref" not in eng2.storage.get_alert(b.id)["edge"]


def graded_vs_market(eng, title, direction, stock_close, spy_close=606.0):
    a = news(title, direction=direction)
    a.created = T0
    a.edge = {"refs": {"ACME": 100.0}, "mkt_ref": 600.0}
    eng.storage.add_alert(a)
    eng.live_quotes.update("ACME", stock_close, 99.0, CLOSE, "tape")
    eng.live_quotes.update("SPY", spy_close, 600.0, CLOSE, "tape")
    record_outcomes(eng, CLOSE + 300)
    return eng.storage.get_alert(a.id)


def test_each_grade_is_also_read_against_the_market(tmp_path):
    eng = semis_engine(tmp_path)
    up = graded_vs_market(eng, "Acme wins contract number 1", "up", 103.0)  # +3%, the market +1%
    assert up["edge"]["outcome"]["ACME"]["close"] == 3.0 and up["edge"]["mkt_outcome"]["close"] == 1.0
    assert excess(up) == 2.0
    down = graded_vs_market(eng, "Acme wins contract number 2", "down", 97.0)  # a short: -3% while SPY +1%
    assert excess(down) == 4.0
    lag = graded_vs_market(eng, "Acme wins contract number 3", "up", 100.5)  # up, but less than the market
    assert lag["edge"]["outcome"]["ACME"]["close"] == 0.5 and excess(lag) == -0.5
    assert excess({"edge": {"outcome": {"ACME": {"close": 1.0, "sign": 1}}}}) is None  # no market noted
    rates = catalyst_rates(eng, now=CLOSE + 300)
    assert rates["contract|*"]["x_close"] == {"n": 3, "hit": 0.67, "median": 2.0}
    assert rates["contract|*"]["close"]["hit"] == 1.0  # raw, all three went their way; one trailed the market
    row = next(r for r in track_record(eng, now=CLOSE + 300)["rows"] if r["kind"] == "news:HIGH")
    assert row["x_close"]["n"] == 3


# --------------------------------------------------------------------------- the pre-trade check


def setup(eng):
    contract_history(eng)  # contract wins, $2-10B: 83% their way, median +2.75%
    now = time.time()
    eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
    a = stored(eng)  # from $100, 1% before: +0.5% since
    eng.storage.update_alert_edge(a["id"], {"odds": {"p": 0.71, "ev": 1.1, "n": 300},
                                            "regime_base": {"regime": "risk-on", "n": 9, "hit": 0.78, "median": 2.0}})  # fmt: skip
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(lookup=lambda s, t=None: None))
    return a, now


def tones(r):
    return {c["key"]: c["tone"] for c in r["checks"]}


def test_a_clean_setup_gets_a_go(tmp_path):
    eng = semis_engine(tmp_path)
    a, now = setup(eng)
    r = pre_trade(eng, DEV, "ACME", "long", a["id"], now)
    assert r["verdict"] == "go" and r["bad"] == 0
    assert tones(r) == {
        "odds": "good",
        "record": "good",
        "regime": "good",
        "moved": "good",
        "plan": "good",
        "size": "good",
    }
    text = {c["key"]: c["text"] for c in r["checks"]}
    assert text["odds"] == "Odds 71% by the close, expected +1.1% (the model, 300 graded)"
    assert text["moved"] == "+0.5% its way since the alert, of a typical +2.8%"
    assert text["plan"].endswith("reward-to-risk 1.5")


def test_reasons_for_pause_add_up(tmp_path):
    eng = semis_engine(tmp_path)
    a, now = setup(eng)
    # already long two other Technology names, and the report is tomorrow
    eng.desk.apply(DEV, "hold_set", {"symbol": "DLTA", "shares": 100})
    eng.live_quotes.update("BETA", 50.0, 50.0, now - 5, "tape")
    eng.desk.apply(DEV, "track_add", {"symbol": "BETA", "direction": "long", "entry": 50})
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(
        lookup=lambda s, t=None: {"in_days": 1, "when": "after the close"} if s == "ACME" else None))  # fmt: skip
    r = pre_trade(eng, DEV, "ACME", "long", a["id"], now)
    t = {c["key"]: c for c in r["checks"]}
    assert t["sector"]["tone"] == "warn" and t["sector"]["text"].startswith(
        "You're already long 2 other Technology stocks (BETA, DLTA)"
    )
    assert (
        t["report"]["text"] == "ACME reports after the close tomorrow: a gap can go straight through a stop"
    )
    assert r["verdict"] == "caution"
    # reporting today, and the market fading the news: red flags
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(lookup=lambda s, t=None: {"in_days": 0, "when": ""} if s == "ACME" else None)
    )
    day = datetime.fromtimestamp(now, ET).date().isoformat()
    eng.storage.set_setting("diverge_log", json.dumps([{"alert_id": a["id"], "day": day, "against": 2.1}]))
    r = pre_trade(eng, DEV, "ACME", "long", a["id"], now)
    assert tones(r)["report"] == "bad" and tones(r)["diverge"] == "bad" and r["verdict"] == "think twice"


def test_an_alert_that_already_moved(tmp_path):
    eng = semis_engine(tmp_path)
    a, now = setup(eng)
    eng.live_quotes.update("ACME", 103.0, 99.0, now - 1, "tape")  # +3% since: past the typical +2.75%
    r = pre_trade(eng, DEV, "ACME", "long", a["id"], now)
    t = {c["key"]: c for c in r["checks"]}
    assert t["moved"]["tone"] == "bad" and t["moved"]["text"].endswith(": the usual move is done")
    assert t["plan"]["tone"] == "bad" and r["verdict"] == "think twice"
    # the short side of the same alert: the odds and records are for its call, the move is with it
    eng.desk.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100})
    r2 = pre_trade(eng, DEV, "ACME", "long", "", now)
    assert tones(r2) == {"dup": "warn"} and r2["verdict"] == "go"


def test_a_stock_with_nothing_known(tmp_path):
    eng = semis_engine(tmp_path)
    r = pre_trade(eng, DEV, "gama", "short")
    assert r == {
        "symbol": "GAMA",
        "direction": "short",
        "verdict": "go",
        "checks": [],
        "good": 0,
        "warn": 0,
        "bad": 0,
    }


async def test_run26_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    a, _ = setup(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                srv.make_url(f"/api/precheck?symbol=acme&direction=long&alert={a['id']}"), headers=h
            ) as r:
                assert (await r.json())["verdict"] == "go"
            async with s.get(srv.make_url("/api/precheck?symbol=acme&direction=sideways"), headers=h) as r:
                assert r.status == 400
            async with s.get(
                srv.make_url("/api/precheck?symbol=acme"), headers={"Authorization": "Bearer tok"}
            ) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
