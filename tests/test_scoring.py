from __future__ import annotations

import re

import pytest

from news247.analysis.entities import EntityMatcher
from news247.analysis.scorer import Scorer, term_regex
from news247.config import build_config
from news247.models import NewsItem, Severity
from news247.models import SourceTier as T


@pytest.fixture(scope="module")
def scorer() -> Scorer:
    cfg = build_config({})
    return Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)


def item(title: str, tier: T = T.MEDIA, entities: list[str] | None = None, **kw) -> NewsItem:
    return NewsItem(
        source="t", title=title, tier=tier, extra={"entities": entities or [], **kw.pop("extra", {})}, **kw
    )


# (title, tier, entity hints, minimum severity, maximum severity)
CALIBRATION = [
    (
        "Introducing ChatGPT agents for enterprise sales and customer support",
        T.PRIMARY,
        ["OpenAI"],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    ("Introducing GPT-6", T.PRIMARY, ["OpenAI"], Severity.HIGH, Severity.CRITICAL),
    (
        "Claude Cowork plugins for legal, finance and sales teams",
        T.PRIMARY,
        ["Anthropic"],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "Software stocks tumble after OpenAI unveils AI agent for accounting",
        T.MEDIA,
        [],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "Federal Reserve issues FOMC statement",
        T.PRIMARY,
        ["Federal Reserve"],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "Salesforce (NYSE: CRM) to Acquire Informatica for $8 Billion",
        T.WIRE,
        [],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "I am imposing a 100% tariff on all semiconductors coming into the United States",
        T.PRIMARY,
        ["White House"],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "@reuters.com: BREAKING: China bans exports of Nvidia H20 chips",
        T.MEDIA,
        [],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "@DeItaone: *TESLA CEO ELON MUSK SAYS ROBOTAXI LAUNCH DELAYED",
        T.PRIMARY,
        [],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "DeepSeek releases V4, matches GPT-5 at a fraction of the cost",
        T.SOCIAL,
        [],
        Severity.HIGH,
        Severity.CRITICAL,
    ),
    (
        "SHAREHOLDER ALERT: Rosen Law Firm reminds investors of class action against XYZ Corp",
        T.WIRE,
        [],
        Severity.LOW,
        Severity.LOW,
    ),
    ("5 AI stocks to buy now: here's why", T.MEDIA, [], Severity.LOW, Severity.LOW),
    ("NVIDIA to Present at Upcoming Investor Conferences", T.PRIMARY, ["NVIDIA"], Severity.LOW, Severity.LOW),
    (
        "Acme Biotech (NASDAQ: ACMB) Announces FDA Approval of Lead Drug",
        T.WIRE,
        [],
        Severity.LOW,
        Severity.MEDIUM,
    ),
    ("Is Nvidia stock a buy?", T.MEDIA, [], Severity.LOW, Severity.LOW),
    ("Weekly market recap podcast", T.MEDIA, [], Severity.LOW, Severity.LOW),
]


@pytest.mark.parametrize("title,tier,ents,lo,hi", CALIBRATION)
def test_calibration(scorer: Scorer, title, tier, ents, lo, hi):
    a = scorer.score(item(title, tier, ents))
    assert lo <= a.severity <= hi, f"{title!r} -> {a.score} {a.severity.name}\n" + "\n".join(a.reasons)


def test_software_disruption_names_software_tickers(scorer: Scorer):
    a = scorer.score(item("Introducing ChatGPT agents for accounting and legal work", T.PRIMARY, ["OpenAI"]))
    assert "ai_disrupts_software" in a.themes
    assert {"CRM", "INTU", "NOW"} <= set(a.tickers)
    assert a.direction == "down"
    assert a.tickers.index("CRM") < a.tickers.index("MSFT")  # theme victims before exposure names


def test_exposure_for_private_company(scorer: Scorer):
    a = scorer.score(item("OpenAI raises funding at a record valuation"))
    assert "OpenAI" in a.entities
    assert "MSFT" in a.tickers


def test_reasons_explain_score(scorer: Scorer):
    a = scorer.explain(item("BREAKING: Apple to acquire Disney"))
    joined = "\n".join(a.reasons)
    assert "urgency" in joined and "to acquire" in joined and "watchlist" in joined


def test_summary_keywords_count_half(scorer: Scorer):
    with_title = scorer.score(item("Company announces bankruptcy filing"))
    in_summary = scorer.score(item("Company update", summary="The company filed for bankruptcy protection."))
    assert in_summary.score < with_title.score
    assert any("(summary)" in r for r in in_summary.reasons)


def test_untracked_tickers_scaled_down(scorer: Scorer):
    tracked = scorer.score(
        item("NVIDIA (NASDAQ: NVDA) announces definitive agreement to acquire Foo", T.WIRE)
    )
    untracked = scorer.score(
        item("Zzyzx (NASDAQ: ZZYZ) announces definitive agreement to acquire Foo", T.WIRE)
    )
    assert untracked.score < tracked.score * 0.6
    assert any("untracked" in r for r in untracked.reasons)


def test_source_boost(scorer: Scorer):
    plain = scorer.score(item("Some filing"))
    boosted = scorer.score(item("Some filing", extra={"boost": 30}))
    assert boosted.score == pytest.approx(plain.score + 30)


def test_mute():
    cfg = build_config({"scoring": {"mute": ["(?i)openai"]}})
    s = Scorer(cfg.scoring, cfg.knowledge)
    a = s.score(item("OpenAI launches GPT-6", T.PRIMARY))
    assert a.score == 0 and a.severity is Severity.LOW and "muted" in a.reasons[0]


def test_user_keywords_and_themes():
    cfg = build_config(
        {
            "scoring": {
                "keywords": {"widget*": 40},
                "themes": {"mine": {"all": [["foo"], ["bar*"]], "weight": 20, "tickers": ["FOO"]}},
                "companies": [{"name": "Foo Corp", "ticker": "FOO", "aliases": ["foo corp"]}],
            }
        }
    )
    s = Scorer(cfg.scoring, cfg.knowledge)
    a = s.score(item("foo barrels into widgets"))
    assert "mine" in a.themes and "FOO" in a.tickers
    assert any("widget*" in r for r in a.reasons)
    assert s.score(item("Foo Corp news")).entities == ["Foo Corp"]


def test_rescore_updates_severity(scorer: Scorer):
    a = scorer.score(item("Nothing much"))
    scorer.rescore(a, 80, "test")
    assert a.severity is Severity.CRITICAL and a.reasons[-1] == "+80 test"
    scorer.rescore(a, -500, "down")
    assert a.score == 0


@pytest.mark.parametrize(
    "term,text,hit",
    [
        ("acqui*", "Acquisition of X", True),
        ("acqui*", "reacquire", False),
        ("ban", "Bank of America", False),
        ("ban", "China ban on chips", True),
        ("chapter 11", "files for Chapter  11", True),
        ("gpt-6*", "GPT-6.5 is here", True),
        ("open-source*", "open-sourced model", True),
        ("cpi", "CPIX", False),
    ],
)
def test_term_regex(term, text, hit):
    assert bool(re.search(term_regex(term), text, re.I)) is hit


def test_entity_matcher():
    m = EntityMatcher(
        [
            {"name": "Meta Platforms", "ticker": "META", "aliases": ["facebook"], "cased": ["Meta"]},
            {"name": "Alphabet", "ticker": "GOOGL", "aliases": ["google"]},
            {"name": "Google DeepMind", "aliases": ["google deepmind"], "exposure": ["GOOGL"]},
        ],
        watchlist=["NVDA", "NOW"],
    )
    names = lambda t: [c.name for c in m.find_companies(t)]  # noqa: E731
    assert names("Meta unveils glasses") == ["Meta Platforms"]
    assert names("the meta question") == []  # case-sensitive alias
    assert names("Google DeepMind ships Gemini") == ["Google DeepMind"]  # longest alias wins
    assert m.find_tickers("Buy $NVDA and $BRK.B (NYSE: XYZ) now", "NVDA NOW soars") == [
        "NVDA",
        "BRK-B",
        "XYZ",
    ]
    assert "NOW" not in m.find_tickers("", "Act NOW")  # stoplisted bare word
