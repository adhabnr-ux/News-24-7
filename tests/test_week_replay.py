"""The real week of Oct 5-9 2026, replayed: every big single-catalyst move among $2B+ US stocks,
with the first public headlines at their real timestamps (TradingView's news feed), scored by
today's rules and judged by the scorecard. Before this week's fixes only PCVX was pushed ahead
of its move; the takeover targets RXO, PTC and OPCH were never even tagged."""

from __future__ import annotations

import json

import pytest

from news247.models import Severity

from .conftest import FIXTURES
from .replaying import replay as run_replay

WEEK = json.loads((FIXTURES / "week_2026-10-05.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def replay():
    return run_replay(WEEK)


def test_every_first_headline_tags_its_stock(replay):
    scored, _ = replay
    for h in WEEK["headlines"]:
        assert h["symbol"] in scored[h["title"]].tickers, (
            h["symbol"],
            h["title"],
            scored[h["title"]].reasons,
        )


@pytest.mark.parametrize(
    "title",
    [
        "C.H. Robinson to buy RXO for $5.8 billion",
        "Schneider Electric to buy PTC for $22.6bn in cash",
        "Mckesson And CD&R Near $5Bn-Plus Deal To Buy Infusion Services Provider Option Care Health - FT",
        "Vaxcyte Reports Positive Topline Data from OPUS-1, the Pivotal Adult Phase 3 Trial of VAX-31, the Company’s 31-Valent Pneumococcal Conjugate Vaccine Candidate",
        "Humana Announces Improved CMS Star Ratings with 95 Percent of Medicare Advantage Members in 4-Star or Above Plans for 2027",
    ],
)
def test_the_first_reports_push(replay, title):
    scored, _ = replay
    an = scored[title]
    assert an.severity >= Severity.HIGH, an.reasons


def test_takeover_targets_are_up_and_first(replay):
    scored, _ = replay
    for title, sym in (
        ("C.H. Robinson to buy RXO for $5.8 billion", "RXO"),
        ("Schneider Electric to buy PTC for $22.6bn in cash", "PTC"),
        ("PTC to Be Acquired by Schneider Electric for $205 Per Share in All-Cash Merger", "PTC"),
    ):
        an = scored[title]
        assert an.tickers[0] == sym and an.direction == "up" and an.deal["target"] == sym, an.reasons
    assert scored["PTC to Be Acquired by Schneider Electric for $205 Per Share in All-Cash Merger"].deal[
        "premium"
    ] == pytest.approx(42.3, abs=0.1)  # $205 vs $144.03


def test_the_scorecard_for_the_week(replay):
    _, verdicts = replay
    assert {s: v["verdict"] for s, v in verdicts.items()} == WEEK["expected"]
    # pushed ahead of the price: minutes for the releases, the FT scoop before the after-hours move
    assert 3 * 60 < verdicts["PCVX"]["lead_s"] < 5 * 60
    assert 4 * 60 < verdicts["RXO"]["lead_s"] < 5 * 60
    assert 3 * 60 < verdicts["OPCH"]["lead_s"] < 4 * 60
    # in the app (MEDIUM) 47 minutes before Humana's after-hours surge
    assert verdicts["HUM"]["lead_s"] == pytest.approx(47 * 60, abs=60)
    # the price moved first: PTC 10 minutes before the first headline
    assert verdicts["PTC"]["lag_s"] == pytest.approx(10 * 60, abs=60)
    # XP moved on Brazil's Sunday vote: in the app (MEDIUM, tagging XP) 9+ hours before the pre-market move
    assert verdicts["XP"]["lead_s"] > 9 * 3600
    assert "NVAX" not in verdicts  # its radar alert was MEDIUM (shown, not pushed)
