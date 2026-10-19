"""Why is it moving? The instant EDGAR + news lookup for a move with no headline."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import news247.market.why as why
from news247.market.universe import Universe
from news247.market.why import WhyHunter, parse_company_filings, parse_news_search, search_name
from news247.models import Alert, PriceMove, Severity, SourceTier

from .conftest import fixture_text, rfc822

ET = ZoneInfo("America/New_York")
SINCE = datetime(2026, 10, 8, 10, 0, tzinfo=ET).timestamp()  # the previous close minus 6 h


def test_company_filings_since_the_last_session():
    items = parse_company_filings(fixture_text("edgar_company_cci.xml"), "CCI", SINCE)
    assert [i.title for i in items] == [
        "Crown Castle Inc. (CCI) files 8-K: 7.01 Reg FD Disclosure; 8.01 Other Events",
        "Crown Castle Inc. (CCI) files 424B5: prospectus supplement (likely an offering)",
    ]  # July's 10-Q is history
    k8, pro = items
    assert k8.uid == "sec:0001051470-26-000077"  # the 8-K feed's uid: one alert per filing
    assert k8.tier is SourceTier.PRIMARY and k8.tickers == ["CCI"]
    assert k8.extra["boost"] == 6 + 5 + 6 and pro.extra["boost"] == 12
    assert k8.url.endswith("-index.htm") and k8.published == pytest.approx(
        datetime(2026, 10, 9, 7, 31, 10, tzinfo=ET).timestamp()
    )
    assert parse_company_filings("<feed></feed>", "CCI", SINCE) == []


def test_news_search_keeps_only_headlines_that_name_the_company():
    body = fixture_text(
        "gnews_cci.xml", RECENT=rfc822(time.time() - 600), OLD=rfc822(time.time() - 5 * 86400)
    )
    items = parse_news_search(body.encode(), "CCI", "Crown Castle", time.time() - 86400)
    assert [i.title for i in items] == [
        "Crown Castle stock jumping as SpaceX acquires 800 MHz spectrum - Benzinga",
        "Tower stocks: what SpaceX's spectrum deal means for AMT, CCI and SBAC - Barron's",
    ]  # Scottish castles don't count; last week's CFO news is too old
    assert all(i.tickers == ["CCI"] and i.source == "why-gnews" for i in items)


def test_search_name():
    assert search_name("Crown Castle Inc. Common Stock") == "Crown Castle"
    assert search_name("T-Mobile US, Inc. Common Stock") == "T-Mobile US"
    assert search_name("Icahn Enterprises L.P. - Depositary units") == "Icahn Enterprises"
    assert search_name("NVIDIA Corporation Common Stock") == "NVIDIA"


def test_rate_limits():
    h = WhyHunter(None, "ua", per_hour=2, cooldown_s=600)
    assert h.allow("A", 1000) and not h.allow("A", 1100)  # one lookup per company per cooldown
    assert h.allow("B", 1100) and not h.allow("C", 1200)  # and at most 2 an hour
    assert h.allow("C", 1000 + 3601)
    assert h.stats["skipped"] == 2


@pytest.fixture
def lookup(server):
    server.on("/cgi-bin/browse-edgar", (200, fixture_text("edgar_company_cci.xml")))
    server.on(
        "/rss/search",
        (
            200,
            fixture_text(
                "gnews_cci.xml",
                RECENT=rfc822(time.time() - 300),
                OLD=rfc822(
                    datetime(2026, 10, 1, 12, 0, tzinfo=ET).timestamp()
                ),  # before any test alert's window, whatever today is
            ),
        ),
    )
    old = (why.EDGAR_COMPANY_URL, why.GNEWS_URL)
    why.EDGAR_COMPANY_URL = server.url("/cgi-bin/browse-edgar") + "?CIK={symbol}"
    why.GNEWS_URL = server.url("/rss/search") + "?q={q}"
    yield server
    why.EDGAR_COMPANY_URL, why.GNEWS_URL = old


async def test_hunt_reads_both_and_sends_the_contact_agent(lookup, http):
    h = WhyHunter(http, "Test/1.0 (t@example.com)")
    items = await h.hunt("CCI", "Crown Castle Inc. Common Stock", time.time() - 86400 * 30)
    assert {i.source for i in items} == {"why-edgar", "why-gnews"}
    edgar = next(r for r in lookup.requests if r["path"] == "/cgi-bin/browse-edgar")
    assert edgar["query"]["CIK"] == "CCI" and edgar["headers"]["User-Agent"] == "Test/1.0 (t@example.com)"
    news = next(r for r in lookup.requests if r["path"] == "/rss/search")
    assert news["query"]["q"] == '"Crown Castle"'
    assert h.stats == {"hunts": 1, "found": 1, "skipped": 0, "errors": 0}


async def test_hunt_survives_a_dead_source(server, http):
    server.on("/rss/search", (500, "down"))
    old = (why.EDGAR_COMPANY_URL, why.GNEWS_URL)
    why.EDGAR_COMPANY_URL = server.url("/missing") + "?CIK={symbol}"
    why.GNEWS_URL = server.url("/rss/search") + "?q={q}"
    try:
        h = WhyHunter(http, "ua")
        assert await h.hunt("CCI", "Crown Castle", 0) == []
        assert h.stats["errors"] == 2
    finally:
        why.EDGAR_COMPANY_URL, why.GNEWS_URL = old


def radar_alert(sym: str = "CCI", related: list | None = None) -> Alert:
    a = Alert(
        kind="price",
        severity=Severity.HIGH,
        title=f"▲ {sym} +9.3%",
        body="",
        tickers=[sym],
        move=PriceMove(sym, 9.3, 86400, 75.0, 68.6),
        related=related or [],
    )
    a.edge["smallcap"] = {"label": "Radar: moving before the news", "radar": True}
    return a


async def test_engine_attaches_the_best_explanation(cfg, lookup, http):
    from news247.engine import Engine

    eng = Engine(cfg, http=http, sources=[])
    eng.why = WhyHunter(http, "Test/1.0 (t@example.com)")  # off in the shared test config
    eng.universe.replace(
        Universe.stub(
            {"CCI": {"name": "Crown Castle Inc. Common Stock", "mcap": 34e9, "price": 75.0}}
        ).by_symbol.values()
    )
    events: list[tuple[str, dict]] = []
    eng._broadcast = lambda ev, data: events.append((ev, data))  # type: ignore[method-assign]
    alert = radar_alert()
    alert.created = datetime(2026, 10, 9, 9, 35, tzinfo=ET).timestamp()  # Friday, after the 07:31 8-K
    await eng.publish(alert)
    w = await eng._hunt(alert, "CCI", "Crown Castle Inc. Common Stock")
    assert w["found"] and w["source"] in ("why-edgar", "why-gnews") and w["others"] == 3
    assert (
        w["title"]
        == max(
            (r for r in eng.storage.recent_items(50) if r["source"].startswith("why-")),
            key=lambda r: r["score"],
        )["title"]
    )
    stored = eng.storage.get_alert(alert.id)
    assert stored["edge"]["why"]["title"] == w["title"]
    assert ("edge", {"id": alert.id, "why": w}) in events
    # every finding went through the normal pipeline: stored, scored, deduplicated
    assert len([r for r in eng.storage.recent_items(50) if r["source"].startswith("why-")]) == 4
    again = await eng._hunt(alert, "CCI", "Crown Castle")
    assert again["found"]  # already-seen items still explain the move


async def test_engine_only_hunts_moves_without_a_headline(cfg, http):
    from news247.engine import Engine

    eng = Engine(cfg, http=http, sources=[])
    eng.why = WhyHunter(http, "Test/1.0 (t@example.com)")  # off in the shared test config
    spawned: list = []
    eng._spawn = lambda coro: spawned.append(coro.close())  # type: ignore[method-assign]
    eng._maybe_hunt(radar_alert("AAA", related=[{"kind": "news", "title": "x"}]))  # already explained
    cal = radar_alert("BBB")
    cal.edge["smallcap"]["label"] = "Radar: on the calendar today"
    eng._maybe_hunt(cal)
    low = radar_alert("CCC")
    low.severity = Severity.LOW
    eng._maybe_hunt(low)
    assert spawned == []
    eng._maybe_hunt(radar_alert("DDD"))
    eng._maybe_hunt(radar_alert("DDD"))  # once per company per cooldown
    assert len(spawned) == 1
    eng.why = None  # why_lookup: false
    eng._maybe_hunt(radar_alert("EEE"))
    assert len(spawned) == 1


async def test_engine_hunt_with_nothing_found(cfg, server, http):
    from news247.engine import Engine

    server.on("/cgi-bin/browse-edgar", (200, "<feed></feed>"))
    server.on("/rss/search", (200, "<rss><channel></channel></rss>"))
    old = (why.EDGAR_COMPANY_URL, why.GNEWS_URL)
    why.EDGAR_COMPANY_URL = server.url("/cgi-bin/browse-edgar") + "?CIK={symbol}"
    why.GNEWS_URL = server.url("/rss/search") + "?q={q}"
    try:
        eng = Engine(cfg, http=http, sources=[])
        eng.why = WhyHunter(http, "Test/1.0 (t@example.com)")  # off in the shared test config
        alert = radar_alert("ZZZZ")
        await eng.publish(alert)
        w = await eng._hunt(alert, "ZZZZ", "")
    finally:
        why.EDGAR_COMPANY_URL, why.GNEWS_URL = old
    assert w["found"] is False and w["searched"] == ["EDGAR filings", "news search"]
    assert eng.storage.get_alert(alert.id)["edge"]["why"]["found"] is False
    assert server.requests[-1]["query"]["q"] == '"ZZZZ" stock'
