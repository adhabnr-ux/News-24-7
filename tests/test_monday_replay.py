"""Monday Oct 12 2026, replayed: every US-traded $2B+ stock that moved 10%+, with the first headlines
News247 can read at their real timestamps (TradingView's news feed), scored by today's rules and
judged by the scorecard.

The audit's finding: Fannie Mae and Freddie Mac fell 14% the morning after Michael Burry warned
their common stock could become "worthless", and the system saw none of it. Both trade over the
counter, so Nasdaq's screener (the universe) leaves them out and the radar never priced them; and
every Fannie/Freddie headline was read as FHFA news and tagged FICO, RKT and UWMC instead."""

from __future__ import annotations

import json

import pytest

from news247.analysis.entities import EntityMatcher
from news247.analysis.scorer import Scorer
from news247.config import OptionsConfig, build_config
from news247.market.options import OptionsFeed, OptionsRadar
from news247.market.universe import OTC_LISTINGS, Universe, with_otc
from news247.models import NewsItem, Severity, SourceTier

from .conftest import FIXTURES
from .replaying import replay as run_replay

MONDAY = json.loads((FIXTURES / "monday_2026-10-12.json").read_text(encoding="utf-8"))
BURRY = (
    "Michael Burry Warns Fannie Mae and Freddie Mac Could Become ‘Worthless’ as Trump-Era Gains "
    "Collapse Under Fresh Pressure"
)
MIZUHO = "Fannie Mae, Freddie Mac cut to Neutral at Mizuho as release date extends"


@pytest.fixture(scope="module")
def replay():
    return run_replay(MONDAY)


@pytest.fixture(scope="module")
def scorer():
    cfg = build_config({})
    return Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)


def score(scorer: Scorer, title: str, tier: SourceTier = SourceTier.MEDIA):
    return scorer.score(NewsItem(source="test", title=title, url="u", published=1.0, detected=1.0, tier=tier))


def test_every_first_headline_tags_its_stock(replay):
    scored, _ = replay
    for h in MONDAY["headlines"]:
        assert h["symbol"] in scored[h["title"]].tickers, (
            h["symbol"],
            h["title"],
            scored[h["title"]].reasons,
        )


def test_the_scorecard_for_monday(replay):
    _, verdicts = replay
    assert {s: v["verdict"] for s, v in verdicts.items()} == MONDAY["expected"]
    # Benzinga's 04:56 ET story (Alpaca news) pushes both, hours before the open
    for sym, lead_h in (("FNMA", 5.45), ("FMCC", 4.87)):
        assert verdicts[sym]["headline"]["title"] == BURRY
        assert verdicts[sym]["lead_s"] == pytest.approx(lead_h * 3600, abs=120), sym
    # WSJ's credit-score story was in the app (MEDIUM) 5h45m before FICO crossed +9%
    assert verdicts["FICO"]["lead_s"] == pytest.approx(5.75 * 3600, abs=60)
    # sector beta, correctly not pushed: Benzinga's Cerebras recap had nothing new; Semtech had
    # no headline News247 reads
    assert verdicts["CBRS"]["headline"]["score"] < 35
    assert "FIGS" not in verdicts  # its radar alert was MEDIUM (+13.9% at the high, under 15%)


def test_burry_is_pushed_and_says_down(replay):
    scored, _ = replay
    an = scored[BURRY]
    assert an.severity >= Severity.HIGH and an.direction == "down", an.reasons
    assert an.tickers[:2] == ["FNMA", "FMCC"]
    assert {"gse_release", "star_investor_bet"} <= set(an.themes)


def test_the_downgrade_is_shown_not_penalized_as_an_earnings_date(replay):
    scored, _ = replay
    an = scored[MIZUHO]
    assert an.severity == Severity.MEDIUM and an.direction == "down", an.reasons
    assert not any("release date" in r for r in an.reasons if r.startswith("-"))


def test_fannie_and_freddie_are_companies_not_fhfa_news(scorer):
    """The scorer alone (no universe): the knowledge base itself now knows both companies."""
    for title in (
        BURRY,
        MIZUHO,
        "Why Michael Burry Is Swapping Fannie Mae, Freddie Mac Common Stock For Preferred Shares",
    ):
        tickers = score(scorer, title).tickers
        assert tickers[:2] == ["FNMA", "FMCC"] and "FICO" not in tickers, (title, tickers)
    # credit scores are still FICO's story (Pulte's posts crashed FICO twice in Sep 2026)
    an = score(
        scorer, "FHFA's Pulte: lenders may use VantageScore 4.0 instead of FICO, effective immediately"
    )
    assert an.severity >= Severity.HIGH and "FICO" in an.tickers and "FNMA" in an.tickers


@pytest.mark.parametrize(
    ("title", "direction"),
    [
        ("Morgan Stanley cut to Equal Weight at Wells Fargo", "down"),
        ("Fannie Mae lowered to Underperform at BofA", "down"),
        ("Fannie Mae raised to Buy at Jefferies", "up"),
    ],
)
def test_rating_changes_read_as_rating_changes(scorer, title, direction):
    an = score(scorer, title)
    assert an.direction == direction and any("keyword" in r for r in an.reasons), an.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Equifax Announces Earnings Release Date and Conference Call for Third Quarter 2026 Results",
        "Acme Sets Third Quarter 2026 Release Date",
        "Acme Announces Q3 Release Date and Conference Call",
    ],
)
def test_earnings_dates_are_still_penalized(scorer, title):
    an = score(scorer, title, SourceTier.WIRE)
    assert an.severity == Severity.LOW and any("release date" in r for r in an.reasons), an.reasons


def test_an_era_is_not_an_actor():
    m = EntityMatcher([{"name": "White House", "aliases": ["trump", "white house"], "authority": True}])
    assert m.find_companies("Burry Takes Aim At Another Trump-Era Trade") == []
    assert m.find_companies("Biden-era rule scrapped by Trump") != []  # Trump the actor still counts
    assert [c.name for c in m.find_companies("Trump signs order")] == ["White House"]


def test_the_universe_carries_the_otc_giants():
    uni = Universe(with_otc([]))
    assert {li.symbol for li in OTC_LISTINGS} == {"FNMA", "FMCC"}
    assert uni.get("FNMA").market_cap >= 2e9 and uni.get("FMCC").market_cap >= 2e9  # inside the radar's sweep
    # a headline naming them is recognized by the universe too ("fannie", "freddie" are distinctive)
    assert [li.symbol for li in uni.find_named("Fannie Mae and Freddie Mac shares slide", limit=4)] == [
        "FNMA",
        "FMCC",
    ]
    # no listed options over the counter: the options tape leaves them out
    feed = OptionsFeed(OptionsConfig(), None, uni, OptionsRadar(OptionsConfig()))  # type: ignore[arg-type]
    assert all(li.otc for li in OTC_LISTINGS) and feed.companies() == []
    # a screener listing of the same symbol wins over the built-in one
    stub = Universe.stub({"FNMA": {"name": "Fannie Mae", "mcap": 9e9, "price": 9.0}})
    merged = Universe(with_otc(stub.by_symbol.values()))
    assert merged.get("FNMA").market_cap == 9e9 and merged.get("FMCC") is not None
