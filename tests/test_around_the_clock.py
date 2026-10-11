"""Markets that trade while US stocks don't: Bitcoin on weekends, index futures and oil overnight."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from news247.config import CASH_HOURS_ONLY_QUIET, PROXIES, build_config
from news247.models import NewsItem, PriceMove, SourceTier
from news247.notify.format import move_headline

ET = ZoneInfo("America/New_York")


def test_baskets_and_thresholds_are_on_by_default():
    cfg = build_config({})
    m = cfg.market
    for sym in ("BTC-USD", "ETH-USD", "SOL-USD", "ES=F", "NQ=F", "RTY=F", "YM=F", "CL=F", "BZ=F"):
        assert sym in m.symbols, sym
        assert sym in m.overrides, sym  # their own thresholds, never the stock defaults
    groups = {g.name: g for g in m.groups}
    assert groups["futures"].min_members == 3 and groups["crypto_24h"].window == 900
    # 2% in 5 minutes is news for Bitcoin; 0.8% is news for S&P futures
    assert m.overrides["BTC-USD"][0].pct == 2.0 and m.overrides["ES=F"][1].pct == 0.8
    assert set(CASH_HOURS_ONLY_QUIET) >= {"ES=F", "NQ=F", "GROUP:futures"}
    assert "MSTR" in PROXIES["BTC-USD"] and "XOM" in PROXIES["CL=F"]


def test_headlines_name_the_market():
    assert move_headline(PriceMove("BTC-USD", -6.2, 3600, 92000, 98000)) == "▼ Bitcoin -6.20% in 1h"
    assert move_headline(PriceMove("ES=F", -1.3, 900, 6500, 6585)).startswith(
        "▼ S&P 500 futures -1.30% in 15m"
    )
    g = PriceMove(
        "GROUP:futures", -1.1, 600, 0, 0, members={"ES=F": -1.0, "NQ=F": -1.4, "RTY=F": -1.2, "YM=F": -0.8}
    )
    assert move_headline(g) == "▼ INDEX FUTURES -1.10% in 10m (4/4 moving together)"
    s = PriceMove("GROUP:software", -2.5, 600, 0, 0, members={"CRM": -3.0, "NOW": -2.0})
    assert move_headline(s).startswith("▼ SOFTWARE stocks")


async def test_weekend_bitcoin_drop_links_crypto_news_and_names_the_stocks(cfg):
    from news247.engine import Engine
    from news247.models import Analysis, Severity

    eng = Engine(cfg, sources=[])
    published: list = []

    async def capture(alert, push=True):
        published.append(alert)

    eng.publish = capture  # type: ignore[method-assign]
    sat = datetime(2026, 10, 10, 14, 0, tzinfo=ET).timestamp()  # Saturday: stocks are shut
    item = NewsItem(
        source="t", title="Strategy pauses bitcoin purchases", detected=sat - 1800, tier=SourceTier.WIRE
    )
    eng._remember(item, Analysis(score=52, severity=Severity.MEDIUM, tickers=["MSTR"]))
    mv = PriceMove("BTC-USD", -4.8, 3600, 93000, 97700, detected=sat)
    alert = eng._move_alert(mv)
    assert alert.title == "▼ Bitcoin -4.80% in 1h"
    assert "Watch: COIN MSTR IBIT HOOD MARA RIOT" in alert.body
    assert [r["title"] for r in alert.related] == ["Strategy pauses bitcoin purchases"]


async def test_futures_are_quiet_while_the_cash_market_is_open(cfg, monkeypatch):
    from news247.engine import Engine

    eng = Engine(cfg, sources=[])
    eng.coalesce_s = 0
    published: list = []

    async def capture(alert, push=True):
        published.append(alert.title)

    eng.publish = capture  # type: ignore[method-assign]
    eng.detector.check_groups = lambda: []  # type: ignore[method-assign]
    for phase, expect in (("open", 1), ("pre-market", 2), ("closed", 2)):
        published.clear()
        monkeypatch.setattr(eng.calendar, "market_status", lambda now, p=phase: {"phase": p})
        await eng.on_moves(
            [
                PriceMove("ES=F", -1.3, 900, 6500, 6585, detected=time.time()),
                PriceMove("CL=F", 3.0, 900, 80, 77.7, detected=time.time()),
            ]
        )
        assert len(published) == expect, (phase, published)
        assert any("WTI crude" in t for t in published)  # oil always counts
