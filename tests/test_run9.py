"""Run 9 of the desk: the portfolio, following themes and sectors, "tomorrow", and the smart-alert
preview."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.desk import Desk
from news247.dossier import portfolio, rule_preview, tomorrow, tomorrow_push
from news247.models import Analysis, Severity
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import item, phones

ET = ZoneInfo("America/New_York")


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- the portfolio


def test_holdings_are_validated_and_count_as_watched():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "hold_set", {"symbol": "acme", "shares": 10, "cost": 90})
    d.apply(DEV, "hold_set", {"symbol": "BETA", "shares": 5})
    assert d.get(DEV)["holdings"] == {
        "ACME": {"shares": 10.0, "cost": 90.0},
        "BETA": {"shares": 5.0, "cost": None},
    }
    for bad in (
        {"symbol": "ACME", "shares": -1},
        {"symbol": "ACME", "shares": 1, "cost": 0},
        {"symbol": "ACME", "shares": "x"},
    ):
        with pytest.raises(ValueError):
            d.apply(DEV, "hold_set", bad)
    assert d.watchers(["BETA"]) == [DEV]  # held, not on the watchlist: still pushed at MEDIUM
    d.apply(DEV, "hold_set", {"symbol": "BETA", "shares": 0})
    assert "BETA" not in d.get(DEV)["holdings"] and d.watchers(["BETA"]) == []


def test_portfolio_value_pnl_and_exposure(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=10.0, GAMA=-5.0)  # ACME 110 (+10%), GAMA 95 (-5%)
    eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 10, "cost": 100})
    eng.desk.apply(DEV, "hold_set", {"symbol": "GAMA", "shares": 20})
    p = portfolio(eng, DEV, now)
    assert p["value"] == 1100 + 1900 and p["day_pnl"] == pytest.approx(
        100 - 100, abs=0.01
    )  # +$100 on ACME, -$100 on GAMA
    acme = next(r for r in p["rows"] if r["symbol"] == "ACME")
    assert (
        acme["day_pnl"] == 100.0
        and acme["unreal"] == 100.0
        and acme["unreal_pct"] == 10.0
        and acme["weight"] == pytest.approx(36.7, abs=0.1)
    )
    assert p["unreal"] == 100.0 and p["unreal_pct"] == 10.0  # only the holding with a cost counts
    assert [s["sector"] for s in p["sectors"]] == ["Health Care", "Technology"]
    assert portfolio(eng, DEV2, now)["rows"] == []


# --------------------------------------------------------------------------- following themes and sectors


async def test_following_a_theme_or_sector_pushes_its_medium_alerts(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, b = phones(eng, server)
        eng.desk.apply(DEV, "follow", {"kind": "themes", "name": "chip_export_controls"})
        eng.desk.apply(DEV2, "follow", {"kind": "sectors", "name": "Health Care"})
        with pytest.raises(ValueError):
            eng.desk.apply(DEV, "follow", {"kind": "planets", "name": "x"})
        chips = news("US widens chip export curbs", tickers=(), sev=Severity.MEDIUM)
        chips.analysis.themes = ["chip_export_controls"]
        await eng._push_watchers(chips)
        assert [r["path"] for r in server.requests] == ["/push/a"]
        server.requests.clear()
        await eng._push_watchers(news("Gamma trial update", tickers=("GAMA",), sev=Severity.MEDIUM))
        assert [r["path"] for r in server.requests] == ["/push/b"] and b.read(server.requests[0]["body"])[
            "title"
        ].startswith("★ ")
        eng.desk.apply(DEV, "follow", {"kind": "themes", "name": "chip_export_controls", "on": False})
        assert eng.desk.get(DEV)["follow"]["themes"] == []
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- tomorrow


def test_tomorrow_card(tmp_path):
    eng = semis_engine(tmp_path)
    now = at(20, 10)  # Wednesday evening: the next session is Thursday
    quotes(eng, now, ACME=6.0, BETA=-4.0)
    eng.desk.apply(DEV, "watch_add", {"symbol": "DLTA"})
    nxt = {"BETA": "2026-10-15", "DLTA": "2026-10-15", "GAMA": "2026-10-20"}
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(
            next={s: {} for s in nxt},
            lookup=lambda s, t=None: {"date": nxt[s], "when": "after the close", "in_days": 1},
        )
    )
    t = tomorrow(eng, DEV, now)
    assert t["date"] == "2026-10-15" and [m["symbol"] for m in t["movers"]] == ["ACME", "BETA"]
    assert [(r["symbol"], r["mine"]) for r in t["reports"]] == [
        ("DLTA", True),
        ("BETA", False),
    ]  # yours first; DLTA is under $2B but yours
    push = tomorrow_push(t)
    assert push["title"] == "🌙 Thursday: what to know" and "Reporting: ★DLTA, BETA" in push["body"]
    assert (
        eng._tomorrow_due(now)
        and not eng._tomorrow_due(at(20, 10, d=16))
        and not eng._tomorrow_due(at(19, 0))
    )
    assert tomorrow(eng, DEV, at(10, 0))["date"] == "2026-10-15"  # during a session: the next one


# --------------------------------------------------------------------------- the rule preview


def test_rule_preview(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for i, score in enumerate([50, 35, 15]):
        it = item(f"Acme headline {i}")
        it.detected = now - 86400 * (i + 1)
        eng.storage.add_item(it, Analysis(score=score, severity=Severity.MEDIUM, tickers=["ACME"]))
    for h1 in (4.0, -1.0):
        a = news()
        a.created = now - 7200
        a.edge = {"outcome": {"ACME": {"h1": h1, "sign": 1}}}
        eng.storage.add_alert(a)
    head = rule_preview(eng, DEV, {"kind": "headline", "symbol": "ACME", "value": 30}, now)
    assert head == {"replayable": True, "days": 30, "headlines": 2, "per_week": 0.5}
    mv = rule_preview(eng, DEV, {"kind": "news_move", "symbol": "ACME", "value": 3}, now)
    assert mv["headlines"] == 2 and mv["graded"] == 2 and mv["moved"] == 1
    assert (
        rule_preview(eng, DEV, {"kind": "volume", "symbol": "ACME", "value": 5}, now)["replayable"] is False
    )


async def test_run9_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/follow-options"), headers=h) as r:
                body = await r.json()
                assert "chip_export_controls" in body["themes"] and "Technology" in body["sectors"]
            for path in ("/api/portfolio", "/api/tomorrow"):
                async with s.get(srv.make_url(path), headers=h) as r:
                    assert r.status == 200, path
            async with s.post(
                srv.make_url("/api/rule-preview"),
                json={"kind": "headline", "symbol": "*", "value": 20},
                headers=h,
            ) as r:
                assert (await r.json())["replayable"] is True
            async with s.get(srv.make_url("/api/portfolio"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
