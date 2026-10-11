"""Takeover targets of any size, from the headline shapes they are first reported in."""

from __future__ import annotations

import pytest

from news247.analysis.deals import read_deal, resolve
from news247.market.universe import Universe

UNI = Universe.stub(
    {
        "NVDA": {"name": "NVIDIA Corporation Common Stock", "mcap": 4e12, "price": 180},
        "AAPL": {"name": "Apple Inc. Common Stock", "mcap": 3.5e12, "price": 230},
        "TSLA": {"name": "Tesla, Inc. Common Stock", "mcap": 1e12, "price": 300},
        "OXY": {"name": "Occidental Petroleum Corporation Common Stock", "mcap": 45e9, "price": 45},
        "INTC": {"name": "Intel Corporation Common Stock", "mcap": 100e9, "price": 24},
        "MSFT": {"name": "Microsoft Corporation Common Stock", "mcap": 3.5e12, "price": 500},
        "PTC": {"name": "PTC Inc. Common Stock", "mcap": 17e9, "price": 144.03},
        "RXO": {"name": "RXO, Inc. Common Stock", "mcap": 3.9e9, "price": 23.38},
        "OPCH": {"name": "Option Care Health, Inc. Common Stock", "mcap": 3.6e9, "price": 23.37},
        "CHRW": {"name": "C.H. Robinson Worldwide, Inc. Common Stock", "mcap": 14e9, "price": 120},
        "MCK": {"name": "McKesson Corporation Common Stock", "mcap": 90e9, "price": 700},
        "WBD": {"name": "Warner Bros. Discovery, Inc. Series A Common Stock", "mcap": 30e9, "price": 12},
        "PSKY": {"name": "Paramount Skydance Corporation Class B Common Stock", "mcap": 15e9, "price": 15},
        "ALL": {"name": "Allstate Corporation (The) Common Stock", "mcap": 50e9, "price": 200},
        "ON": {"name": "ON Semiconductor Corporation Common Stock", "mcap": 25e9, "price": 60},
        "CCI": {"name": "Crown Castle Inc. Common Stock", "mcap": 34e9, "price": 69},
    }
)


@pytest.mark.parametrize(
    "title,target,buyer,stage,price",
    [
        # Oct 5-6 2026, as first reported
        ("C.H. Robinson to buy RXO for $5.8 billion", "RXO", "CHRW", "definitive", None),
        ("Schneider Electric to buy PTC for $22.6bn in cash", "PTC", None, "definitive", None),
        (
            "PTC to Be Acquired by Schneider Electric for $205 Per Share in All-Cash Merger",
            "PTC",
            None,
            "definitive",
            205.0,
        ),
        (
            "McKesson, CD&R near $5 billion-plus deal to buy Option Care, FT reports",
            "OPCH",
            "MCK",
            "talks",
            None,
        ),
        (
            "Mckesson And CD&R Near $5Bn-Plus Deal To Buy Infusion Services Provider Option Care Health - FT",
            "OPCH",
            "MCK",
            "talks",
            None,
        ),
        (
            "CD&R and McKesson Corporation Sign Agreement to Acquire Option Care Health",
            "OPCH",
            None,
            "definitive",
            None,
        ),
        (
            "Option Care Health to be acquired for $32.05 per share by CD&R and McKesson",
            "OPCH",
            "MCK",
            "definitive",
            32.05,
        ),
        (
            "RXO to Merge With C.H. Robinson; Holders May Elect $30.25 Cash or Stock Mix",
            "RXO",
            "CHRW",
            "definitive",
            30.25,
        ),
        # other shapes
        (
            "Paramount Skydance makes $30-per-share offer for Warner Bros. Discovery",
            "WBD",
            "PSKY",
            "talks",
            30.0,
        ),
        (
            "Warner Bros. Discovery to be acquired by Paramount Skydance for $31 a share",
            "WBD",
            "PSKY",
            "definitive",
            31.0,
        ),
        ("Apollo to take Crown Castle private in $40 billion deal", "CCI", None, "definitive", None),
    ],
)
def test_reads_the_target(title, target, buyer, stage, price):
    d = read_deal(title, UNI)
    assert d is not None, title
    assert d.target.symbol == target
    assert (d.buyer.symbol if d.buyer is not None else None) == buyer
    assert d.stage == stage and d.price == price


@pytest.mark.parametrize(
    "title",
    [
        "Investors rush to buy Nvidia ahead of earnings",
        "3 reasons to buy Apple stock now",
        "Is it time to buy Tesla?",
        "Berkshire buys more Occidental shares",
        "Apple to buy back $110 billion of stock",
        "Nvidia to buy $5 billion stake in Intel",
        "Microsoft to acquire gaming studio for $1 billion",
        "SpaceX to acquire spectrum that enables Starlink Mobile services",
        "Buffett says it's not the time to buy stocks",
        "Hedge funds buy Tesla calls ahead of deliveries",
        "Wall Street rushes to buy tech after Fed",
        "Intel nears deal to buy chip startup SambaNova",
        "Allstate to buy National General for $4 billion",  # the target isn't listed
        "Microsoft in talks to acquire Intel's networking unit",  # a unit, not the company
        "Crown Castle to sell fiber business to Zayo for $8.5 billion",
        "Nvidia buys all the Blackwell capacity TSMC can make",
        "Traders buy ON calls as chip stocks rally",
        "Director Jane A Smith buys $550K of Humana Inc (HUM) stock at $250.00 (Form 4, open market)",
        "Cathie Wood's ARK buys $20 million of Tesla shares",
        "Norway's wealth fund buys 2 million shares of Apple",
    ],
)
def test_not_a_takeover(title):
    assert read_deal(title, UNI) is None


def test_premium_and_resolution():
    d = read_deal("PTC to Be Acquired by Schneider Electric for $205 Per Share in All-Cash Merger", UNI)
    assert d.premium == pytest.approx(42.33, abs=0.01)
    assert d.to_dict() == {
        "target": "PTC",
        "buyer": None,
        "buyer_name": "Schneider Electric",
        "stage": "definitive",
        "price": 205.0,
        "premium": 42.3,
    }
    assert resolve("Option Care", UNI).symbol == "OPCH"  # the first words of one listed name
    assert resolve("RXO", UNI).symbol == "RXO"  # a company named by its ticker
    assert resolve("ALL", UNI) is None and resolve("ON", UNI) is None  # common words
    assert resolve("Warner Bros. Discovery", UNI).symbol == "WBD"
    assert read_deal("C.H. Robinson to buy RXO", Universe()) is None  # no universe yet


def _scorer(entries):
    from news247.analysis.scorer import Scorer
    from news247.config import build_config

    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    s.attach_universe(Universe.stub(entries), **cfg.smallcap.filters())
    return s


def _score(s, title):
    import time

    from news247.models import NewsItem, SourceTier

    return s.score(NewsItem(source="t", title=title, url="u", tier=SourceTier.MEDIA, published=time.time()))


def test_a_giant_buying_something_small_is_not_its_own_big_news():
    from news247.models import Severity

    s = _scorer({"MSFT": {"name": "Microsoft Corporation Common Stock", "mcap": 3.5e12, "price": 500}})
    a = _score(s, "Microsoft to acquire gaming studio for $1 billion")
    assert a.severity < Severity.HIGH, a.reasons
    assert any("MSFT is the buyer" in r and "0.03% of its value" in r for r in a.reasons)
    # a deal big for the buyer keeps its weight
    big = _scorer({"MSFT": {"name": "Microsoft Corporation Common Stock", "mcap": 1e12, "price": 500}})
    assert not any(
        "is the buyer" in r for r in _score(big, "Microsoft to acquire Activision for $69 billion").reasons
    )


def test_previews_are_not_results():
    from news247.models import Severity

    s = _scorer({})
    assert _score(s, "Investors rush to buy Nvidia ahead of earnings").severity < Severity.HIGH
    assert _score(s, "Nvidia reports record third-quarter results").severity >= Severity.HIGH
