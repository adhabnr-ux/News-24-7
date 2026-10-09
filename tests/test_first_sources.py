"""Sources added from the 2026 "where did it break first" research: Federal Register public
inspection, Polymarket odds jumps, and EDGAR stake / tender-offer filings."""

from __future__ import annotations

import json

import pytest

from news247.config import build_config
from news247.models import Severity, SourceTier
from news247.sources import SOURCE_TYPES, build_sources
from news247.sources.federal_register import FederalRegisterSource
from news247.sources.polymarket import PolymarketSource
from news247.sources.sec_edgar import SECEdgarSource

from .conftest import fixture_text
from .test_sources import ctx


def scorer():
    from news247.analysis.scorer import Scorer

    cfg = build_config({})
    return Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)


# --------------------------------------------------------------------------- Federal Register


def test_federal_register_parse_and_filters():
    data = json.loads(fixture_text("federal_register_pi.json"))
    items = FederalRegisterSource({"name": "fr"}, ctx()).parse(data)
    assert len(items) == 3
    bis = items[0]
    assert bis.title == "Public inspection (Commerce, BIS, Rule): Entity List: Additions to the Entity List"
    assert bis.tier is SourceTier.PRIMARY and bis.uid == "fr-pi-2026-20111"
    assert bis.extra["special_filing"] and bis.extra["publication_date"] == "2026-10-14"
    assert bis.published == pytest.approx(1791576900, abs=1)  # 2026-10-09T16:15-04:00
    assert "23 entities in China" in bis.summary and "Federal Register" in bis.extra["entities"]
    only = FederalRegisterSource(
        {"name": "fr", "agencies": ["industry-and-security-bureau"], "types": ["Rule"]}, ctx()
    ).parse(data)
    assert [i.uid for i in only] == ["fr-pi-2026-20111"]
    with pytest.raises(ValueError):
        FederalRegisterSource({"name": "fr"}, ctx()).parse({"error": "x"})


def test_federal_register_filings_are_scored_sensibly():
    s = scorer()
    items = FederalRegisterSource({"name": "fr"}, ctx()).parse(
        json.loads(fixture_text("federal_register_pi.json"))
    )
    entity_list, paperwork, s232 = (s.score(i) for i in items)
    assert entity_list.severity >= Severity.HIGH, entity_list.reasons
    assert "NVDA" in entity_list.tickers
    assert paperwork.severity <= Severity.LOW, paperwork.reasons
    assert s232.severity >= Severity.HIGH, s232.reasons


async def test_federal_register_conditional_get(server, http):
    body = fixture_text("federal_register_pi.json")

    def handler(request):
        from aiohttp import web

        if request.headers.get("If-None-Match") == '"pi1"':
            return web.Response(status=304)
        return web.Response(text=body, content_type="application/json", headers={"ETag": '"pi1"'})

    server.on("/current.json", handler)
    src = FederalRegisterSource({"name": "fr", "url": server.url("/current.json")}, ctx(http))
    assert len(await src.fetch()) == 3
    assert await src.fetch() == []


# --------------------------------------------------------------------------- Polymarket


def market(mid: str, q: str, yes: float, vol: float = 2_000_000, slug: str = "us-iran") -> dict:
    return {
        "id": mid,
        "question": q,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": json.dumps([str(yes), str(round(1 - yes, 3))]),
        "volume24hr": vol,
        "events": [{"slug": slug}],
    }


def test_polymarket_alerts_on_fast_moves_only():
    src = PolymarketSource({"name": "pm", "min_move_pts": 10, "window_s": 300}, ctx())
    t0 = 1_800_000_000.0
    q = "US x Iran ceasefire by October 31?"
    assert src.parse([market("1", q, 0.40), market("2", "Small market?", 0.5, vol=1000)], now=t0) == []
    assert src.parse([market("1", q, 0.45), market("2", "Small market?", 0.9, vol=1000)], now=t0 + 60) == []
    out = src.parse([market("1", q, 0.71), market("2", "Small market?", 0.1, vol=1000)], now=t0 + 180)
    assert len(out) == 1  # the thin market moved more but on no volume
    it = out[0]
    assert it.title == f"Polymarket: '{q}' jumps 31 pts to 71% in 3 min ($2.0M traded in 24h)"
    assert it.url == "https://polymarket.com/event/us-iran" and it.tier is SourceTier.SOCIAL
    # cooldown: drifting a little further doesn't re-alert...
    assert src.parse([market("1", q, 0.75)], now=t0 + 240) == []
    # ...but a reversal of another 10+ points does
    out = src.parse([market("1", q, 0.52)], now=t0 + 300)
    assert len(out) == 1 and "plunges" in out[0].title
    # slow drift over hours never alerts
    slow = PolymarketSource({"name": "pm"}, ctx())
    for k in range(20):
        assert slow.parse([market("9", "Fed hike in December?", 0.30 + k * 0.01)], now=t0 + k * 600) == []


def test_polymarket_jump_scores_high_enough_to_text():
    src = PolymarketSource({"name": "pm"}, ctx())
    t0 = 1_800_000_000.0
    q = "US strikes Iran by October 15?"
    src.parse([market("1", q, 0.20)], now=t0)
    item = src.parse([market("1", q, 0.55)], now=t0 + 120)[0]
    a = scorer().score(item)
    assert a.severity >= Severity.HIGH, a.reasons


def test_polymarket_rejects_garbage():
    src = PolymarketSource({"name": "pm"}, ctx())
    with pytest.raises(ValueError):
        src.parse({"data": "nope"})
    assert src.parse([{"id": "1"}, {"question": "no id"}]) == []


# --------------------------------------------------------------------------- EDGAR stakes


def test_sec_pairs_stake_and_tender_filings():
    src = SECEdgarSource({"name": "sec", "forms": ["SCHEDULE 13G", "SC TO-T"]}, ctx())
    src.load_ticker_map({"1513845": "NBIS", "1739566": "UTZ", "1045810": "NVDA"})
    items = src.parse(fixture_text("sec_current_13d.xml").encode())
    titles = [i.title for i in items]
    assert titles == [
        "SCHEDULE 13G: Nvidia Corp discloses stake in Nebius Group N.V. (NBIS)",
        "Utz Brands, Inc. (UTZ): SC TO-T tender offer",
    ]  # the "(Filed by)" row became the author; the private target was dropped
    nb, utz = items
    assert nb.tickers == ["NBIS"] and nb.extra["filed_by"] == "Nvidia Corp" and nb.extra["boost"] == 4
    assert utz.extra["boost"] == 25 and nb.uid == "sec:0001045810-26-000200"
    s = scorer()
    assert s.score(nb).severity >= Severity.HIGH, s.score(nb).reasons
    assert s.score(utz).severity >= Severity.HIGH, s.score(utz).reasons


def test_new_types_are_registered_and_default_sources_build():
    assert {"federal_register", "polymarket"} <= set(SOURCE_TYPES)
    built = {s.name: s for s in build_sources(build_config({}).sources, ctx())}
    for name in ("federal-register-pi", "polymarket", "ofac-actions", "scotus-slip", "fhfa-news"):
        assert name in built, name
    assert built["federal-register-pi"].tier is SourceTier.PRIMARY


async def test_pagewatch_with_a_wrong_pattern_reports_failure(server, http):
    from news247.sources.pagewatch import PageWatchSource

    server.on("/recent-actions", (200, '<a href="/recent-actions/20261009">Iran-related Designations</a>'))
    good = PageWatchSource(
        {
            "name": "ofac",
            "url": server.url("/recent-actions"),
            "link_pattern": r"/recent-actions/\d{8}(_\d+)?$",
        },
        ctx(http),
    )
    assert [i.title for i in await good.fetch()] == ["Iran-related Designations"]
    bad = PageWatchSource(
        {"name": "ofac", "url": server.url("/recent-actions"), "link_pattern": "/press-release/"}, ctx(http)
    )
    with pytest.raises(ValueError, match="no links .* match link_pattern"):
        await bad.fetch()
