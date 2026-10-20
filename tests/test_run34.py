"""Run 34: your risk in one place (open risk across paper calls, clusters, sector shocks, events,
a cap) and skill or the market (each call's return split into SPY's part and the rest)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from news247.actions import action_center
from news247.alpha import attribution, call_split
from news247.precheck import pre_trade
from news247.risk import desk_risk

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run15 import graded


def book(eng, now):
    q = eng.live_quotes
    for sym, px in (("ACME", 101.0), ("BETA", 50.0), ("DLTA", 10.0), ("GAMA", 20.0), ("SPY", 600.0)):
        q.update(sym, px, px, now - 5, "tape")
    d = eng.desk
    d.apply(DEV, "prefs", {"account": 100000, "risk_pct": 1.0})
    d.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100, "stop": 98}, eng.price_of)
    d.apply(
        DEV,
        "track_add",
        {"symbol": "BETA", "direction": "long", "entry": 50, "stop": 49, "shares": 200},
        eng.price_of,
    )
    d.apply(DEV, "track_add", {"symbol": "DLTA", "direction": "long", "entry": 10}, eng.price_of)
    d.apply(DEV, "track_add", {"symbol": "GAMA", "direction": "short", "entry": 20, "stop": 21}, eng.price_of)
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(
        lookup=lambda s, t=None: {"date": "2026-10-22", "in_days": 2, "when": ""} if s == "GAMA" else None))  # fmt: skip


# --------------------------------------------------------------------------- your risk


def test_the_whole_book_at_a_glance(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    book(eng, now)
    r = desk_risk(eng, DEV, now)
    calls = {c["symbol"]: c for c in r["calls"]}
    assert (
        calls["ACME"]["risk_usd"] == pytest.approx(1485.15, abs=0.01)
        and calls["ACME"]["sized"] == "risk per idea"
    )
    assert calls["BETA"]["risk_usd"] == 200.0 and calls["BETA"]["sized"] == "shares"
    assert calls["GAMA"]["risk_usd"] == 1000.0 and calls["GAMA"]["direction"] == "short"
    assert (
        r["unbounded"] == ["DLTA"]
        and r["total_usd"] == pytest.approx(2685.15, abs=0.01)
        and r["total_pct"] == 2.69
    )
    assert r["clusters"] == [{"sector": "Technology", "side": "long", "symbols": ["ACME", "BETA", "DLTA"]}]
    assert [(s["sector"], s["cost"]) for s in r["shocks"]] == [
        ("Technology", -1800.0),
        ("Health Care", 600.0),
    ]
    assert r["events"] == [
        {"symbol": "GAMA", "title": "GAMA reports", "date": "2026-10-22", "in_days": 2, "on": "open call"}
    ]
    assert r["cap"] is None and r["over"] is False


def test_a_cap_flags_the_book_and_the_next_call(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    book(eng, now)
    assert not any(i["kind"] == "risk" for i in action_center(eng, DEV, now)["items"])
    eng.desk.apply(DEV, "prefs", {"risk_cap": 2})
    item = next(i for i in action_center(eng, DEV, now)["items"] if i["kind"] == "risk")
    assert item["title"] == "Open risk 2.7% of the account: over your 2% cap"
    assert item["detail"] == "$2,685 across 3 calls if every stop were hit; 1 with no stop"
    line = next(c for c in pre_trade(eng, DEV, "GAMA", "long", "", now)["checks"] if c["key"] == "book")
    assert (
        line["tone"] == "bad"
        and line["text"] == "Open risk 2.7% of the account → 3.7% with this one (your cap 2%)"
    )
    with pytest.raises(ValueError, match="risk cap is 0"):
        eng.desk.apply(DEV, "prefs", {"risk_cap": 4})
    assert desk_risk(eng, DEV2, now)["calls"] == [] and desk_risk(eng, DEV2, now)["total_pct"] is None


# --------------------------------------------------------------------------- skill or the market


def test_each_call_notes_the_market_and_splits_its_return(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    book(eng, now)
    acme = eng.desk.get(DEV)["tracked"][0]
    assert acme["mkt_entry"] == 600.0 and eng.desk.get(DEV)["tracked"][1]["shares"] == 200
    eng.live_quotes.update("ACME", 103.0, 100.0, now + 60, "tape")
    eng.live_quotes.update("GAMA", 19.0, 20.0, now + 60, "tape")
    eng.live_quotes.update("SPY", 606.0, 600.0, now + 60, "tape")
    eng.desk.apply(DEV, "track_close", {"id": acme["id"]}, eng.price_of)
    gama = next(t for t in eng.desk.get(DEV)["tracked"] if t["symbol"] == "GAMA")
    eng.desk.apply(DEV, "track_close", {"id": gama["id"]}, eng.price_of)
    c = eng.desk.get(DEV)["closed"]
    assert c[1]["mkt_exit"] == 606.0
    assert call_split(c[1]) == {"pnl": 3.0, "market": 1.0, "beyond": 2.0}  # long: the market gave 1 point
    assert call_split(c[0]) == {"pnl": 5.0, "market": -1.0, "beyond": 6.0}  # a short against a rising market
    assert call_split({"pnl_pct": 1.0, "mkt_entry": None}) is None
    a = graded("Acme headline", 2.0)
    a.created = now - 3600
    a.edge["mkt_outcome"] = {"close": 0.5}
    eng.storage.add_alert(a)
    r = attribution(eng, DEV, 90, now + 120)
    assert r["yours"] == {
        "n": 2,
        "total": 8.0,
        "market": 0.0,
        "beyond": 8.0,
        "avg_beyond": 4.0,
        "hit_beyond": 1.0,
    }
    assert r["every_alert"] == {
        "n": 1,
        "total": 2.0,
        "market": 0.5,
        "beyond": 1.5,
        "avg_beyond": 1.5,
        "hit_beyond": 1.0,
    }
    assert r["no_reading"] == 0 and [x["symbol"] for x in r["recent"]] == ["GAMA", "ACME"]
    eng.desk.get(DEV)["closed"].append({"symbol": "OLD", "direction": "long", "closed": now, "pnl_pct": 1.0})
    assert attribution(eng, DEV, 90, now + 120)["no_reading"] == 1
    assert news("x").id
