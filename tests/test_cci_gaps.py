"""Regression for Oct 8-9 2026: SpaceX agreed to buy Grain Management's 800 MHz spectrum (~$8B,
announced Oct 8 after the close). The next day Crown Castle closed +15.6% (opened +7.9%),
American Tower +9.3%, SBA +7.3%, while Verizon, AT&T and T-Mobile fell 10-13%, all on 3-5x volume.

Replayed through the rules as they were, the coverage scored 15-37 (never pushed) and tagged
TSLA, RKLB and ASTS instead of the towers and carriers; the radar never pushed AMT. Every link
must now fire. Prices are real (TradingView daily bars); headlines are the published ones."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news247.analysis.scorer import Scorer
from news247.config import build_config
from news247.market.radar import MoversRadar, Quote
from news247.market.universe import Universe
from news247.models import NewsItem
from news247.models import SourceTier as T

ET = ZoneInfo("America/New_York")
UNI = {
    "CCI": {"name": "Crown Castle Inc. Common Stock", "mcap": 33.9e9, "price": 68.89},
    "AMT": {"name": "American Tower Corporation (REIT) Common Stock", "mcap": 84.9e9, "price": 166.7},
    "SBAC": {"name": "SBA Communications Corporation Class A Common Stock", "mcap": 19.4e9, "price": 170.0},
    "VZ": {"name": "Verizon Communications Inc. Common Stock", "mcap": 173e9, "price": 46.35},
    "T": {"name": "AT&T Inc.", "mcap": 152e9, "price": 24.87},
    "TMUS": {"name": "T-Mobile US, Inc. Common Stock", "mcap": 159e9, "price": 171.3},
    "SATS": {"name": "EchoStar Corporation Class A Common Stock", "mcap": 20e9, "price": 70.0},
    "DAL": {"name": "Delta Air Lines, Inc. Common Stock", "mcap": 40e9, "price": 60.0},
    "HUM": {"name": "Humana Inc. Common Stock", "mcap": 30e9, "price": 250.0},
}


def et(*a: int) -> float:
    return datetime(*a, tzinfo=ET).timestamp()


@pytest.fixture(scope="module")
def scorer() -> Scorer:
    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    s.attach_universe(Universe.stub(UNI), **cfg.smallcap.filters())
    return s


def score(s: Scorer, title: str, tier: T = T.MEDIA):
    return s.score(NewsItem(source="t", title=title, url="u", tier=tier, published=time.time()))


# --------------------------------------------------------------------------- the news


@pytest.mark.parametrize(
    "title,severity,direction",
    [
        # the first reports, Oct 8 after the close (Reuters, WSJ)
        ("SpaceX to acquire spectrum that enables Starlink Mobile services", "CRITICAL", "mixed"),
        ("SpaceX to Pay About $8 Billion for Grain Management's 800 MHz Spectrum", "HIGH", "mixed"),
        ("SpaceX takes aim at US wireless carriers with spectrum acquisition", "HIGH", "down"),
        # Oct 9 coverage (Benzinga)
        ("Crown Castle Stock Jumping as SpaceX Acquires 800 MHz Spectrum", "HIGH", "up"),
        # the Sep 2025 precedent: SpaceX buys EchoStar's AWS-4 / H-block licenses
        ("EchoStar to sell spectrum licenses to SpaceX for about $17 billion", "HIGH", "mixed"),
    ],
)
def test_the_spectrum_deal_pushes_with_the_right_tickers(scorer, title, severity, direction):
    a = score(scorer, title)
    assert a.severity.name == severity, a.reasons
    assert a.direction == direction
    assert "satellite_carrier_threat" in a.themes
    assert {"VZ", "T", "TMUS"} <= set(a.tickers)


def test_tower_read_through_is_tagged(scorer):
    a = score(scorer, "SpaceX to acquire spectrum that enables Starlink Mobile services")
    assert {"CCI", "AMT", "SBAC"} <= set(a.tickers)
    assert "satellite_tower_readthrough" in a.themes


@pytest.mark.parametrize(
    "title",
    [
        "SpaceX launches 24 more Starlink satellites from Cape Canaveral",
        "Starlink launches direct-to-cell texting in Chile with Entel",
        "T-Mobile expands T-Satellite with Starlink to more phones",
        "Starlink adds 1 million subscribers in three months, SpaceX says",
    ],
)
def test_routine_spacex_news_stays_quiet(scorer, title):
    a = score(scorer, title)
    assert a.severity.name in ("LOW", "MEDIUM") and "satellite_carrier_threat" not in a.themes


@pytest.mark.parametrize(
    "title,want",
    [
        ("Crown Castle Stock Jumping as SpaceX Acquires 800 MHz Spectrum", {"CCI"}),
        (
            "SpaceX, AT&T, Crown Castle, Delta, Humana, and More Stocks That Explain Today's Market",
            {"T", "CCI", "HUM"},
        ),
        ("Verizon, AT&T, T-Mobile shares slide as SpaceX buys spectrum", {"VZ", "T", "TMUS"}),
        ("Why American Tower Stock Jumped 8.6% Today", {"AMT"}),  # "(REIT)" in the listed name
    ],
)
def test_big_companies_named_in_a_headline_are_tagged(scorer, title, want):
    assert want <= set(score(scorer, title).tickers)


def test_name_index_reads_reit_suffixes_and_hyphens():
    u = Universe.stub(UNI)
    assert [li.symbol for li in u.find_named("American Tower rallies")] == ["AMT"]
    assert [li.symbol for li in u.find_named("T-Mobile falls")] == ["TMUS"]


# --------------------------------------------------------------------------- the radar


def sc_cfg():
    return build_config({}).smallcap


def q(sym: str, pct: float, session: str = "regular") -> Quote:
    prev = UNI[sym]["price"]
    return Quote(
        sym,
        price=prev * (1 + pct / 100),
        prev_close=prev,
        change_pct=pct,
        market_cap=UNI[sym]["mcap"],
        session=session,
        source="sweep",
    )


def test_cci_flags_pre_market_and_pushes_when_it_crosses_9_percent():
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    (pre,) = r.scan([q("CCI", 7.9, "pre")], et(2026, 10, 9, 8, 5), provider="sweep")
    assert pre.severity == "MEDIUM"  # +7.9% pre-market: on the board, in the app
    (cross,) = r.scan([q("CCI", 9.6)], et(2026, 10, 9, 10, 2), provider="sweep")
    assert cross.severity == "HIGH" and cross.level == pytest.approx(9.0)
    (later,) = r.scan([q("CCI", 15.6)], et(2026, 10, 9, 15, 30), provider="sweep")
    assert later.level == pytest.approx(15.0)


def test_amt_pushes_at_9_3_percent_between_ladder_rungs():
    """+9.3% is past the 9% push line but short of the 10.5% rung: it used to never push."""
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    (first,) = r.scan([q("AMT", 6.4)], et(2026, 10, 9, 9, 50), provider="sweep")
    assert first.severity == "MEDIUM"
    (push,) = r.scan([q("AMT", 9.3)], et(2026, 10, 9, 14, 0), provider="sweep")
    assert push.severity == "HIGH"
    assert r.scan([q("AMT", 9.1)], et(2026, 10, 9, 14, 5), provider="sweep") == []  # no repeat


def test_carriers_after_hours_on_the_announcement():
    """Oct 8 after the close: the carriers fell ~6% in extended trading, then 10-13% on Oct 9."""
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    hits = r.scan(
        [q("VZ", -6.1, "post"), q("T", -6.3, "post"), q("TMUS", -6.0, "post")], et(2026, 10, 8, 17, 30)
    )
    assert {h.symbol for h in hits} == {"VZ", "T", "TMUS"} and all(h.direction == "down" for h in hits)
    next_day = r.scan([q("TMUS", -13.3)], et(2026, 10, 9, 11, 0), provider="sweep")
    assert next_day and next_day[0].severity == "HIGH"
