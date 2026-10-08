from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from news247.http import HTTPError
from news247.models import NewsItem, SourceTier
from news247.sources import SOURCE_TYPES, SourceContext, build_sources
from news247.sources.apis import FinnhubNewsSource, HackerNewsSource, RedditSource
from news247.sources.base import PollingSource, SeenSet
from news247.sources.bluesky import BlueskySource
from news247.sources.halts import TradingHaltsSource
from news247.sources.pagewatch import PageWatchSource, extract_links
from news247.sources.rss import RSSSource
from news247.sources.sec_edgar import SECEdgarSource
from news247.sources.social import MastodonSource, XSource

from .conftest import fixture_text, rfc822


def ctx(http=None, **kw) -> SourceContext:
    return SourceContext(http=http, user_agent="Test/1.0 (t@example.com)", **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- RSS


async def test_rss_conditional_get_and_parse(server, http):
    body = fixture_text("openai_rss.xml", RECENT=rfc822(time.time() - 60), OLD=rfc822(time.time() - 86400))

    def handler(request):
        from aiohttp import web

        if request.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304)
        return web.Response(
            body=body.encode(), headers={"ETag": '"v1"', "Content-Type": "application/rss+xml"}
        )

    server.on("/feed.xml", handler)
    src = RSSSource(
        {"name": "openai-news", "url": server.url("/feed.xml"), "tier": "primary", "entities": ["OpenAI"]},
        ctx(http),
    )
    items = await src.fetch()
    assert [i.title for i in items][0] == "Introducing ChatGPT agents for finance and legal teams"
    first = items[0]
    assert first.tier is SourceTier.PRIMARY
    assert first.summary.startswith("Today we're launching agents that automate")
    assert first.extra["entities"] == ["OpenAI"]
    assert abs(first.published - (time.time() - 60)) < 5
    assert items[1].tickers == ["EXM"]  # from <category>NYSE:EXM</category>
    assert items[2].published is None
    # second poll: server answers 304 -> no work
    assert await src.fetch() == []
    assert server.requests[-1]["headers"]["If-None-Match"] == '"v1"'


async def test_polling_baseline_and_dedupe(server, http):
    body = fixture_text("openai_rss.xml", RECENT=rfc822(time.time() - 60), OLD=rfc822(time.time() - 86400))
    server.on("/feed.xml", (200, body))
    src = RSSSource({"name": "f", "url": server.url("/feed.xml")}, ctx(http, max_item_age_s=1800))
    got: list[NewsItem] = []

    async def emit(i: NewsItem) -> None:
        got.append(i)

    await src.poll_once(emit, baseline=True)
    # fresh item emitted (flagged backfill); old item and undated item swallowed
    assert [i.title for i in got] == ["Introducing ChatGPT agents for finance and legal teams"]
    assert got[0].extra["backfill"] is True
    await src.poll_once(emit, baseline=False)
    assert len(got) == 1  # nothing new
    assert src.health.polls == 2 and src.health.items == 1 and src.health.status == "ok"


async def test_include_exclude_filters(server, http):
    body = fixture_text("openai_rss.xml", RECENT=rfc822(time.time() - 60), OLD=rfc822(time.time() - 60))
    server.on("/feed.xml", (200, body))
    src = RSSSource({"name": "f", "url": server.url("/feed.xml"), "exclude": "partnership"}, ctx(http))
    got: list[NewsItem] = []

    async def emit(i):
        got.append(i)

    await src.poll_once(emit, baseline=False)
    assert all("partnership" not in i.title for i in got) and len(got) == 2


class FlakySource(PollingSource):
    type_name = "flaky"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    async def fetch(self) -> list[NewsItem]:
        self.calls += 1
        if self.calls == 1:
            raise HTTPError(503, "u", retry_after=0.01)
        if self.calls == 2:
            raise ValueError("boom")
        return [
            self.make_item(title=f"item {self.calls}", url=f"https://x/{self.calls}", published=time.time())
        ]


async def test_run_loop_survives_errors_and_backs_off():
    src = FlakySource({"name": "flaky", "interval": 0.01}, ctx())
    got: list[NewsItem] = []
    stop = asyncio.Event()

    async def emit(i):
        got.append(i)
        if len(got) >= 2:
            stop.set()

    src.max_backoff = 0.05
    await asyncio.wait_for(src.run(emit, stop), timeout=10)
    assert src.health.errors == 2
    assert src.health.consecutive_errors == 0
    assert "ValueError" in src.health.last_error
    assert len(got) == 2


def test_next_delay_backoff():
    src = FlakySource({"name": "f", "interval": 10}, ctx())
    assert 9 <= src.next_delay() <= 11
    src.health.consecutive_errors = 3
    assert 72 <= src.next_delay() <= 88
    src.health.consecutive_errors = 30
    assert src.next_delay() <= src.max_backoff * 1.1


def test_seen_set_bounded():
    s = SeenSet(3)
    assert s.add("a") and not s.add("a")
    for k in "bcd":
        s.add(k)
    assert "a" not in s and len(s) == 3


# --------------------------------------------------------------------------- SEC / halts / pages


def test_sec_edgar_parse():
    src = SECEdgarSource({"name": "sec-8k"}, ctx())
    src.load_ticker_map({"1045810": "NVDA", "9999999": "TINY"})
    items = src.parse(fixture_text("sec_current_8k.xml").encode())
    assert [i.tickers for i in items] == [["NVDA"], ["TINY"]]  # private filer and "Reporting" rows dropped
    nv, tiny = items
    assert nv.title == "Nvidia Corp (NVDA) files 8-K: 2.02 Results of Operations"
    assert nv.uid == "sec:0001045810-26-000123"
    assert nv.extra["sec_items"] == ["2.02", "9.01"] and nv.extra["boost"] == 14
    assert nv.tier is SourceTier.PRIMARY
    assert "BANKRUPTCY" in tiny.title and tiny.extra["boost"] == 45
    # 2026-10-08T16:04:01-04:00
    assert nv.published == pytest.approx(1791489841, abs=1)


def test_sec_edgar_min_boost_and_unmapped():
    src = SECEdgarSource({"name": "s", "only_tickers": False, "min_boost": 20}, ctx())
    src.load_ticker_map({})
    items = src.parse(fixture_text("sec_current_8k.xml").encode())
    assert [i.extra["sec_items"] for i in items] == [["1.03"]]


async def test_sec_uses_contact_user_agent(server, http, tmp_path):
    server.on("/cgi-bin/browse-edgar", (200, fixture_text("sec_current_8k.xml")))
    server.on("/files/company_tickers.json", {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"}})
    import news247.sources.sec_edgar as mod

    old = (mod.CURRENT_URL, mod.TICKERS_URL)
    mod.CURRENT_URL = server.url("/cgi-bin/browse-edgar") + "?type={form}&count={count}"
    mod.TICKERS_URL = server.url("/files/company_tickers.json")
    try:
        src = SECEdgarSource({"name": "s"}, ctx(http, data_dir=tmp_path))
        items = await src.fetch()
    finally:
        mod.CURRENT_URL, mod.TICKERS_URL = old
    assert [i.tickers for i in items] == [["NVDA"]]
    assert all(r["headers"]["User-Agent"] == "Test/1.0 (t@example.com)" for r in server.requests)
    assert (tmp_path / "sec_company_tickers.json").exists()  # cached for a day


def test_halts_parse():
    src = TradingHaltsSource({"name": "halts"}, ctx())
    items = src.parse(fixture_text("nasdaq_halts.xml").encode())
    assert len(items) == 2  # IPO1 skipped
    nv, ab = items
    assert nv.title == "TRADING HALT NVDA (NVIDIA Corporation Common Stock) — News Pending [T1]"
    assert nv.tickers == ["NVDA"] and nv.extra["boost"] == 30
    assert nv.published == pytest.approx(1791484172, abs=1)  # 14:29:32 ET
    assert nv.uid == "halt:NVDA:10/08/2026:14:29:32.000:T1"
    assert "Resumes 14:25:00" in ab.summary and ab.extra["halt_code"] == "LUDP"


def test_circuit_breaker_title():
    src = TradingHaltsSource({"name": "halts"}, ctx())
    xml = fixture_text("nasdaq_halts.xml").replace(
        "<ndaq:ReasonCode>T1</ndaq:ReasonCode>", "<ndaq:ReasonCode>MWC1</ndaq:ReasonCode>"
    )
    it = src.parse(xml.encode())[0]
    assert "MARKET-WIDE CIRCUIT BREAKER" in it.title and it.tickers == [] and it.extra["boost"] == 90


def test_pagewatch_on_real_anthropic_markup():
    import re

    html = fixture_text("anthropic_news.html")
    links = extract_links(
        html, "https://www.anthropic.com/news", re.compile(r"anthropic\.com/news/[a-z0-9-]+$")
    )
    unique_hrefs = set(re.findall(r'href="(/news/[a-z0-9-]+)"', html))
    assert len(links) == len(unique_hrefs) >= 6  # featured + list duplicates collapse to one
    for link in links:
        assert link["url"].startswith("https://www.anthropic.com/news/")
        assert link["title"] and not re.match(r"^\w{3} \d{1,2}, \d{4}$", link["title"])  # not the date
        assert link["title"] not in ("Announcements", "Product", "Policy")  # not the category


def test_pagewatch_item_has_no_fake_publish_time():
    src = PageWatchSource(
        {
            "name": "anthropic-news",
            "url": "https://www.anthropic.com/news",
            "link_pattern": r"/news/[a-z0-9-]+$",
            "entities": ["Anthropic"],
        },
        ctx(),
    )
    items = src.parse(fixture_text("anthropic_news.html"))
    assert items and all(i.published is None for i in items)
    assert all(i.extra["entities"] == ["Anthropic"] for i in items)
    assert src.tier is SourceTier.PRIMARY


def test_pagewatch_regex_fallback_for_sitemaps():
    import re

    xml = "<urlset><url><loc>https://x.ai/news/grok-5</loc></url><url><loc>https://x.ai/about</loc></url></urlset>"
    links = extract_links(xml, "https://x.ai/sitemap.xml", re.compile(r"/news/"))
    assert [link["url"] for link in links] == ["https://x.ai/news/grok-5"]


def test_pagewatch_title_from_heading_and_slug():
    import re

    html = '<a href="/blog/big-launch"><span>Mar 3, 2026</span><h3>Big Launch Today</h3></a><a href="/blog/no-text"></a>'
    links = extract_links(html, "https://ex.com/blog", re.compile(r"/blog/[a-z-]+$"))
    assert links[0]["title"] == "Big Launch Today" and links[0]["time"] == "Mar 3, 2026"
    src = PageWatchSource(
        {"name": "p", "url": "https://ex.com/blog", "link_pattern": r"/blog/[a-z-]+$"}, ctx()
    )
    assert [i.title for i in src.parse(html)] == ["Big Launch Today", "No text"]


# --------------------------------------------------------------------------- JSON APIs


def test_hackernews_parse():
    src = HackerNewsSource({"name": "hn"}, ctx())
    items = src.parse(
        {
            "hits": [
                {
                    "title": "OpenAI announces GPT-6",
                    "url": "https://openai.com/x",
                    "objectID": "1",
                    "created_at_i": 1700000000,
                    "points": 900,
                    "author": "a",
                },
                {"title": None, "objectID": "2"},
            ]
        }
    )
    assert len(items) == 1 and items[0].extra["points"] == 900 and items[0].tier is SourceTier.SOCIAL


def test_reddit_requires_credentials_and_parses():
    with pytest.raises(ValueError, match="client_id"):
        RedditSource({"name": "r"}, ctx())
    src = RedditSource({"name": "r", "client_id": "a", "client_secret": "b", "min_score": 5}, ctx())
    data = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "NVDA halted",
                        "permalink": "/r/x/1",
                        "score": 10,
                        "created_utc": 1700000000,
                        "subreddit": "stocks",
                        "author": "u",
                    }
                },
                {"data": {"title": "low", "permalink": "/r/x/2", "score": 1}},
                {"data": {"title": "pinned", "permalink": "/r/x/3", "score": 99, "stickied": True}},
            ]
        }
    }
    assert [i.title for i in src.parse(data)] == ["NVDA halted"]


async def test_reddit_oauth_flow(server, http, monkeypatch):
    server.on("/api/v1/access_token", {"access_token": "tok", "expires_in": 3600})
    server.on(
        "/r/stocks/new", {"data": {"children": [{"data": {"title": "x", "permalink": "/p", "score": 1}}]}}
    )
    src = RedditSource(
        {"name": "r", "client_id": "a", "client_secret": "b", "subreddits": ["stocks"]}, ctx(http)
    )
    real_get, real_post = http.get, http.post

    async def get(url, **kw):
        return await real_get(url.replace("https://oauth.reddit.com", server.url("")), **kw)

    async def post(url, **kw):
        return await real_post(url.replace("https://www.reddit.com", server.url("")), **kw)

    monkeypatch.setattr(http, "get", get)
    monkeypatch.setattr(http, "post", post)
    items = await src.fetch()
    assert [i.title for i in items] == ["x"]
    assert server.requests[0]["headers"]["Authorization"].startswith("Basic ")
    assert server.requests[1]["headers"]["Authorization"] == "Bearer tok"


def test_finnhub_news_parse():
    src = FinnhubNewsSource({"name": "f", "token": "t"}, ctx())
    items = src.parse(
        [
            {
                "id": 7,
                "headline": "Apple hits record",
                "url": "https://u",
                "datetime": 1700000000,
                "related": "AAPL, MSFT",
                "source": "Reuters",
            }
        ]
    )
    assert items[0].tickers == ["AAPL", "MSFT"] and src._min_id == 7


def test_x_query_chunking_and_parse():
    accounts = [f"account_number_{i}" for i in range(40)]
    src = XSource({"name": "x", "bearer_token": "t", "accounts": accounts}, ctx())
    assert len(src.queries) > 1 and all(len(q) <= XSource.MAX_QUERY for q in src.queries)
    assert sum(q.count("from:") for q in src.queries) == 40
    data = {
        "data": [
            {
                "id": "123",
                "text": "We're launching $AAPL thing",
                "author_id": "9",
                "created_at": "2026-10-08T14:00:00.000Z",
                "entities": {"cashtags": [{"tag": "aapl"}]},
            }
        ],
        "includes": {"users": [{"id": "9", "username": "OpenAI", "name": "OpenAI"}]},
        "meta": {"newest_id": "123"},
    }
    it = src.parse(data)[0]
    assert it.title == "@OpenAI: We're launching $AAPL thing" and it.url == "https://x.com/OpenAI/status/123"
    assert it.tickers == ["AAPL"] and it.uid == "x:123"


def test_mastodon_parse():
    src = MastodonSource({"name": "truth", "instance": "https://truthsocial.com", "accounts": ["1"]}, ctx())
    items = src.parse(
        [
            {
                "id": "5",
                "content": "<p>Tariffs on chips: 100%!</p>",
                "url": "https://truthsocial.com/@x/5",
                "created_at": "2026-10-08T14:00:00Z",
                "account": {"acct": "realDonaldTrump", "display_name": "Donald J. Trump"},
            },
            {"id": "6", "content": "", "card": {"title": "Link title"}, "account": {"acct": "a"}},
            {"id": "7", "content": "", "account": {"acct": "a"}},
        ]
    )
    assert [i.title for i in items] == ["@realDonaldTrump: Tariffs on chips: 100%!", "@a: [link] Link title"]


def test_bluesky_event_handling():
    src = BlueskySource({"name": "bsky", "accounts": ["reuters.com"], "keywords": ["openai"]}, ctx())
    src.dids = {"did:plc:reuters": "reuters.com"}
    evt = {
        "did": "did:plc:reuters",
        "time_us": 1791490000000000,
        "kind": "commit",
        "commit": {
            "operation": "create",
            "collection": "app.bsky.feed.post",
            "rkey": "3abc",
            "record": {"text": "BREAKING: Fed cuts rates", "createdAt": "2026-10-08T14:00:00.000Z"},
        },
    }
    it = src.handle_event(evt)
    assert it.title == "@reuters.com: BREAKING: Fed cuts rates"
    assert (
        it.url == "https://bsky.app/profile/reuters.com/post/3abc" and it.uid == "bsky:did:plc:reuters:3abc"
    )
    # replies are skipped, other accounts only pass with a keyword
    evt2 = json.loads(json.dumps(evt))
    evt2["commit"]["record"]["reply"] = {"parent": {}}
    assert src.handle_event(evt2) is None
    stranger = json.loads(json.dumps(evt))
    stranger["did"] = "did:plc:other"
    assert src.handle_event(stranger) is None
    stranger["commit"]["record"]["text"] = "OpenAI just shipped something"
    s_item = src.handle_event(stranger)
    assert s_item is not None and s_item.tier is SourceTier.SOCIAL
    assert src.handle_event({"kind": "identity"}) is None
    url = src.stream_url("wss://h/subscribe")
    assert "wantedCollections=app.bsky.feed.post" in url and "cursor=" in url and "wantedDids" not in url


def test_bluesky_followed_only_url():
    src = BlueskySource({"name": "bsky", "accounts": ["a.com"]}, ctx())
    src.dids = {"did:plc:a": "a.com"}
    assert "wantedDids=did%3Aplc%3Aa" in src.stream_url("wss://h/subscribe")


# --------------------------------------------------------------------------- registry


def test_registry_and_build_sources(caplog):
    assert set(SOURCE_TYPES) == {
        "rss",
        "sec_edgar",
        "halts",
        "pagewatch",
        "hackernews",
        "reddit",
        "finnhub_news",
        "x",
        "mastodon",
        "bluesky",
        "telegram",
        "x_stream",
        "alpaca_news",
    }
    srcs = build_sources(
        [
            {"name": "ok", "type": "rss", "url": "u"},
            {"name": "off", "type": "rss", "url": "u", "enabled": False},
            {"name": "bad", "type": "rss"},
            {"name": "weird", "type": "nope"},
        ],
        ctx(),
    )
    assert [s.name for s in srcs] == ["ok"]
    assert "needs a url" in caplog.text and "unknown type" in caplog.text


def test_default_sources_all_construct(cfg):
    """Every built-in source definition must be constructible (credentials aside)."""
    defs: list[dict[str, Any]] = []
    for s in cfg.sources:
        s = dict(s, enabled=True)
        for key in ("bearer_token", "client_id", "client_secret", "token", "key", "secret"):
            if key in s:
                s[key] = "dummy"
        defs.append(s)
    built = build_sources(defs, ctx())
    assert len(built) == len(defs)


def test_rss_date_only_mirror_feeds():
    xml = """<rss version="2.0"><channel><title>m</title>
    <item><title>New post</title><link>https://www.anthropic.com/news/new-post</link>
    <pubDate>Thu, 08 Oct 2026 00:00:00 +0000</pubDate></item>
    <item><title>Timed post</title><link>https://www.anthropic.com/news/timed</link>
    <pubDate>Thu, 08 Oct 2026 13:14:15 +0000</pubDate></item></channel></rss>"""
    from news247.sources.rss import parse_feed

    src = RSSSource({"name": "m", "url": "u", "date_only": True}, ctx())
    items = [src.entry_to_item(e) for e in parse_feed(xml.encode()).entries]
    assert items[0].published is None and items[0].extra["listed_date"] == 1791417600
    assert items[1].published == 1791465255
    # same URL as the page watcher -> same uid -> never alerted twice
    pw = PageWatchSource(
        {"name": "p", "url": "https://www.anthropic.com/news", "link_pattern": "/news/"}, ctx()
    )
    assert pw.make_item(title="x", url="https://www.anthropic.com/news/new-post/").uid == items[0].uid
