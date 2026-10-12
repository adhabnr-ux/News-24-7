"""An AI lab renting compute from a listed supplier is the supplier's news (IREN, Oct 11 2026).

The Information reported on Sunday evening that Anthropic has an unannounced multiyear cloud
deal with IREN. The rules read it as AI-capex news for NVDA and AMD (MEDIUM, IREN not tagged).
"""

from __future__ import annotations

import pytest

from news247.analysis.deals import read_compute_contract
from news247.analysis.scorer import Scorer
from news247.config import build_config
from news247.market.universe import Universe
from news247.models import NewsItem, Severity
from news247.models import SourceTier as T

UNI = {
    "IREN": {"name": "IREN Limited Ordinary Shares", "mcap": 13.85e9, "price": 35.19},
    "HUT": {"name": "Hut 8 Corp. Common Stock", "mcap": 10.4e9, "price": 84.27},
    "WULF": {"name": "TeraWulf Inc. Common Stock", "mcap": 6.9e9, "price": 13.77},
    "CIFR": {"name": "Cipher Mining Inc. Common Stock", "mcap": 5.6e9, "price": 13.52},
    "NBIS": {"name": "Nebius Group N.V. Class A Ordinary Shares", "mcap": 60e9, "price": 221.0},
    "SNOW": {"name": "Snowflake Inc. Common Stock", "mcap": 70e9, "price": 210.0},
    "CRM": {"name": "Salesforce, Inc. Common Stock", "mcap": 250e9, "price": 260.0},
    "MSFT": {"name": "Microsoft Corporation Common Stock", "mcap": 3.5e12, "price": 500.0},
    "CEG": {"name": "Constellation Energy Corporation Common Stock", "mcap": 90e9, "price": 280.0},
}


@pytest.fixture
def uni() -> Universe:
    return Universe.stub(UNI)


@pytest.fixture
def scorer(uni) -> Scorer:
    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    s.attach_universe(uni, **cfg.smallcap.filters())
    return s


@pytest.mark.parametrize(
    "title,summary,supplier,customer,reported",
    [
        ("Anthropic has quietly signed a multiyear cloud deal with miner IREN, The Information reported", "", "IREN", "Anthropic", True),
        ("@DeItaone: ANTHROPIC SIGNS MULTIYEAR CLOUD DEAL WITH IREN - THE INFORMATION", "", "IREN", "Anthropic", True),
        ("Anthropic's $530 Billion Data Center Sprint Leans on Bitcoin Miners",
         "People familiar with the relationship say Anthropic also has an unannounced multiyear cloud deal with IREN.",
         "IREN", "Anthropic", True),
        ("IREN Signs US$9.7bn AI Cloud Contract with Microsoft", "", "IREN", "Microsoft", False),
        ("Cipher Mining Signs 15-Year Lease with Amazon Web Services", "", "CIFR", "AWS", False),
        ("Hut 8 inks 245 MW data center lease with Anthropic, people familiar say", "", "HUT", "Anthropic", True),
        ("OpenAI taps TeraWulf for 200 MW of AI capacity", "", "WULF", "OpenAI", False),
    ],
)  # fmt: skip
def test_the_supplier_is_found(uni, title, summary, supplier, customer, reported):
    c = read_compute_contract(title, summary, uni)
    assert c is not None and (c.supplier.symbol, c.customer, c.reported) == (supplier, customer, reported)


@pytest.mark.parametrize(
    "title",
    [
        "Snowflake signs $1 billion cloud deal with Microsoft",  # Snowflake buys Azure
        "Salesforce signs expanded cloud deal with Google",  # a customer, and $250B
        "OpenAI signs deal with Microsoft for more Azure capacity",  # Microsoft is too big to matter
        "Anthropic partners with Deloitte to bring Claude to 470,000 employees",  # private, not compute
        "Is IREN's cloud deal with Anthropic real?",  # a question
        "Meta signs 20-year nuclear power deal with Constellation Energy",  # power, not compute
        "IREN to present at the Needham Growth Conference",
    ],
)
def test_not_a_compute_contract(uni, title):
    assert read_compute_contract(title, "", uni) is None


def test_scored_high_with_the_supplier_first(scorer):
    for title, summary, tier in [
        (
            "Anthropic has quietly signed a multiyear cloud deal with miner IREN, The Information reported",
            "",
            T.MEDIA,
        ),
        (
            "@theinformation: Anthropic has an unannounced multiyear cloud deal with bitcoin miner IREN",
            "",
            T.SOCIAL,
        ),
    ]:
        a = scorer.explain(NewsItem(source="t", title=title, summary=summary, tier=tier))
        assert a.severity >= Severity.HIGH and a.tickers[0] == "IREN" and a.direction == "up"
        assert any("compute contract: IREN" in r and "Anthropic" in r for r in a.reasons)


def test_a_large_supplier_is_shown_not_pushed(scorer):
    a = scorer.explain(
        NewsItem(source="t", title="Nebius signs $3 billion AI cloud contract with Meta", tier=T.MEDIA)
    )
    assert a.tickers[0] == "NBIS" and any("compute contract" in r for r in a.reasons)
    assert a.severity >= Severity.MEDIUM  # $60B: a big deal still moves it, but the floor is MEDIUM


def test_takeovers_keep_their_own_reading(scorer):
    a = scorer.explain(NewsItem(source="t", title="Hut 8 to acquire TeraWulf for $8 billion", tier=T.WIRE))
    assert not any("compute contract" in r for r in a.reasons)


def test_sources_cover_the_information_and_compute_contracts():
    from news247.config import load_package_yaml

    sources = {s["name"]: s for s in load_package_yaml("default_sources.yaml")}
    assert "site:theinformation.com" in sources["gnews-scoops"]["url"]
    assert (
        "IREN" in sources["gnews-compute-contracts"]["url"]
        and sources["gnews-compute-contracts"]["interval"] <= 60
    )
