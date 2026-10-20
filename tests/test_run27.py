"""Run 27: why it missed (each graded miss read against the market, its sector and later news) and
size by edge (half-Kelly from the odds and the plan's reward-to-risk, capped by the phone's risk)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.dossier import record_outcomes
from news247.misses import miss_report, post_mortem, sector_ref
from news247.models import Analysis, Severity
from news247.plans import edge_size, trade_plan
from news247.precheck import pre_trade
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import quotes, semis_engine
from .test_run3 import item
from .test_run26 import setup

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 14, 14, 0, tzinfo=ET).timestamp()
CLOSE = datetime(2026, 10, 14, 16, 0, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- why it missed


def test_the_sector_is_noted_when_an_alert_goes_out(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    assert sector_ref(eng, "ACME", now) is None  # no ETF quote and too few stocks on the tape: nothing
    quotes(eng, now, ACME=1.0, BETA=2.0, DLTA=-1.0)  # three Technology names: the tape's sector move
    sr = sector_ref(eng, "ACME", now)
    assert sr["sector"] == "Technology" and "etf" not in sr and sr["chg"] == pytest.approx(1.69, abs=0.01)
    eng.live_quotes.update("SMH", 250.0, 248.0, now - 5, "tape")  # Semiconductors: its own ETF first
    assert sector_ref(eng, "ACME", now) == {"sector": "Technology", "etf": "SMH", "px": 250.0}
    assert sector_ref(eng, "NOPE", now) is None


def missed(eng, title, stock, spy=600.0, smh=250.0, direction="up", later=None, sym="ACME"):
    a = news(title, tickers=(sym,), direction=direction)
    a.created = T0
    a.edge = {
        "refs": {sym: 100.0},
        "mkt_ref": 600.0,
        "sector_ref": {"sector": "Technology", "etf": "SMH", "px": 250.0},
    }
    eng.storage.add_alert(a)
    if later:
        it = item(later)
        it.detected = it.published = T0 + 3600
        eng.storage.add_item(it, Analysis(score=30, severity=Severity.MEDIUM, tickers=["ACME"]))
    eng.live_quotes.update(sym, stock, 99.0, CLOSE, "tape")
    eng.live_quotes.update("SPY", spy, 600.0, CLOSE, "tape")
    eng.live_quotes.update("SMH", smh, 250.0, CLOSE, "tape")
    record_outcomes(eng, CLOSE + 300)
    return eng.storage.get_alert(a.id)


def test_each_miss_gets_its_cause(tmp_path):
    eng = semis_engine(tmp_path)
    # the sector fell 3%, the stock 1%: the news held up, the group didn't
    a = missed(eng, "Acme wins contract number 1", 99.0, smh=242.5)
    assert a["edge"]["sector_outcome"] == {"close": -3.0}
    pm = post_mortem(eng, a)
    assert (pm["cause"], pm["stock"], pm["sector"], pm["market"]) == ("sector", -1.0, -3.0, 0.0)
    assert (
        pm["text"]
        == "SMH went -3.0% against the call; ACME did better (-1.0%). The news held up; the group didn't."
    )
    # the market fell 2%, the sector held: the market
    b = missed(eng, "Acme wins contract number 2", 98.5, spy=588.0, smh=250.0)
    assert post_mortem(eng, b)["cause"] == "market"
    # a later headline on the stock
    c = missed(eng, "Acme wins contract number 3", 98.0, later="Acme CFO resigns unexpectedly")
    pm = post_mortem(eng, c)
    assert (
        pm["cause"] == "news"
        and pm["text"] == "A later headline before the close: “Acme CFO resigns unexpectedly”"
    )
    # nothing else moved: the signal itself
    d = missed(eng, "Beta wins contract number 4", 98.0, sym="BETA")  # no later news on BETA
    pm = post_mortem(eng, d)
    assert pm["cause"] == "faded" and pm["text"].endswith("the signal itself didn't hold.")
    # a short that went up while its sector rallied harder: swamped
    e = missed(eng, "Acme downgraded to sell", 101.0, smh=257.5, direction="down")
    assert post_mortem(eng, e)["cause"] == "sector"
    # a call that worked has no post-mortem
    assert post_mortem(eng, missed(eng, "Acme wins contract number 5", 103.0)) is None
    r = miss_report(eng, now=CLOSE + 600)
    assert (
        r["n"] == 5
        and r["causes"] == {"sector": 2, "market": 1, "news": 1, "faded": 1}
        and r["failed"] == 0.2
    )
    contract = next(k for k in r["kinds"] if k["key"] == "contract")
    assert (contract["n"], contract["sector"], contract["failed"]) == (4, 1, 0.25)


# --------------------------------------------------------------------------- size by edge


def test_half_kelly_capped_by_your_risk():
    es = edge_size(0.6, 1.5, 1.0)
    assert es["kelly_pct"] == pytest.approx(33.33, abs=0.01) and es["risk_pct"] == 1.0 and es["capped"]
    assert es["why"] == "half-Kelly at 60% odds and 1.5 : 1, capped at your 1%"
    small = edge_size(0.42, 1.5, 2.0)  # kelly = 0.42 - 0.58/1.5 = 3.3%: half is 1.67%
    assert small["risk_pct"] == pytest.approx(1.67, abs=0.01) and not small["capped"]
    none = edge_size(0.35, 1.5, 1.0)
    assert none["risk_pct"] == 0.0 and none["why"] == "No edge at 1.5 : 1 with 35% odds: it would need 40%+"
    assert edge_size(None, 1.5, 1.0) is None


def test_plans_and_the_check_carry_the_size(tmp_path):
    eng = semis_engine(tmp_path)
    a, now = setup(eng)  # odds 71%, a 1.5 : 1 plan
    eng.desk.apply(DEV, "prefs", {"account": 100000, "risk_pct": 0.5})
    plan = trade_plan(eng, DEV, eng.storage.get_alert(a["id"]), now)
    es = plan["edge_size"]
    assert es["risk_pct"] == 0.5 and es["capped"] and es["risk_budget"] == 500.0
    assert es["shares"] == int(500 // (plan["entry"] - plan["stop"]))
    eng.storage.update_alert_edge(a["id"], {"odds": {"p": 0.38, "ev": -0.2, "n": 300}})
    r = pre_trade(eng, DEV, "ACME", "long", a["id"], now)
    size = next(c for c in r["checks"] if c["key"] == "size")
    assert size["tone"] == "bad" and size["text"].startswith("No edge at 1.5 : 1 with 38% odds")
    assert r["verdict"] == "think twice"


async def test_run27_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    a = missed(eng, "Acme wins contract number 1", 99.0, smh=242.5)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url(f"/api/alert/{a['id']}"), headers=h) as r:
                assert (await r.json())["post_mortem"]["cause"] == "sector"
            async with s.get(srv.make_url("/api/misses"), headers=h) as r:
                assert (await r.json())["days"] == 60
    finally:
        await srv.close()
        await eng.http.close()
