"""Push sources (X filtered stream, Alpaca news) and a replay of the 2026-10-08 OpenAI scoop."""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web

from news247.config import build_config
from news247.models import NewsItem, PriceMove, Severity, SourceTier
from news247.notify.format import sms_text
from news247.sources import SourceContext, build_sources
from news247.sources.alpaca_news import AlpacaNewsSource
from news247.sources.x_stream import XStreamSource, build_rules

from .test_engine import make_engine

CTX = SourceContext(http=None, user_agent="t")  # type: ignore[arg-type]

TWEET = {
    "data": {
        "id": "1900",
        "text": "OPENAI TOLD INVESTORS ANNUALIZED REVENUE WAS ABOUT $50B AT END OF SEPTEMBER - FT",
        "author_id": "7",
        "created_at": "2026-10-08T17:00:30.000Z",
    },
    "includes": {"users": [{"id": "7", "username": "DeItaone", "name": "Walter Bloomberg"}]},
    "matching_rules": [{"id": "1", "tag": "news247-0"}],
}


# --------------------------------------------------------------------------- X stream


def test_build_rules_chunks_under_limit():
    accts = [f"account_{i:03d}" for i in range(60)]
    rules = build_rules(accts)
    assert all(len(r) <= 512 for r in rules) and sum(r.count("from:") for r in rules) == 60
    assert rules[0].endswith("-is:retweet -is:reply")
    assert build_rules(["@a"], include_replies=True) == ["(from:a) -is:retweet"]


def test_x_stream_vip_and_tiers():
    src = XStreamSource(
        {"name": "xs", "bearer_token": "t", "vip_accounts": ["@OpenAI"], "accounts": ["DeItaone"]}, CTX
    )
    squawk = src.handle_line(json.dumps(TWEET).encode())[0]
    assert squawk.title.startswith("@DeItaone: OPENAI TOLD INVESTORS") and squawk.tier is SourceTier.MEDIA
    assert "floor" not in squawk.extra and squawk.extra["display_name"] == "Walter Bloomberg"
    vip_msg = json.loads(json.dumps(TWEET))
    vip_msg["includes"]["users"][0]["username"] = "OpenAI"
    vip = src.handle_line(json.dumps(vip_msg).encode())[0]
    assert vip.tier is SourceTier.PRIMARY and vip.extra["floor"] == "HIGH"
    assert src.handle_line(b"\r\n") == []
    assert src.accounts == ["OpenAI", "DeItaone"]


async def test_x_stream_syncs_rules_and_streams(server, http):
    existing = {
        "data": [
            {"id": "old1", "value": "(from:gone) -is:retweet -is:reply", "tag": "news247-0"},
            {"id": "other", "value": "someone else's rule", "tag": "mine"},
        ]
    }
    server.on(
        "/2/tweets/search/stream/rules",
        lambda r: web.json_response(existing if r.method == "GET" else {"meta": {}}),
    )

    async def stream(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(b"\r\n")  # heartbeat
        await resp.write(json.dumps(TWEET).encode() + b"\r\n")
        await asyncio.sleep(5)
        return resp

    server.on("/2/tweets/search/stream", stream)
    src = XStreamSource(
        {"name": "xs", "bearer_token": "tok", "accounts": ["DeItaone"], "api_base": server.url("/2")},
        SourceContext(http=http, user_agent="t"),
    )
    got: list[NewsItem] = []
    stop = asyncio.Event()

    async def emit(item):
        got.append(item)
        stop.set()

    await asyncio.wait_for(src.run(emit, stop), 10)
    assert got and got[0].uid == "x:1900"
    posts = [r for r in server.requests if r["method"] == "POST"]
    assert posts[0]["json"] == {"delete": {"ids": ["old1"]}}  # only our stale rule removed
    assert posts[1]["json"]["add"][0]["value"] == "(from:DeItaone) -is:retweet -is:reply"
    stream_req = next(r for r in server.requests if r["path"] == "/2/tweets/search/stream")
    assert stream_req["headers"]["Authorization"] == "Bearer tok"
    assert stream_req["query"]["expansions"] == "author_id"
    assert src.health.connected is False  # cleanly marked down after stop


# --------------------------------------------------------------------------- Alpaca


def test_alpaca_message_parsing():
    src = AlpacaNewsSource({"name": "alp", "key": "k", "secret": "s"}, CTX)
    raw = json.dumps(
        [
            {"T": "success", "msg": "authenticated"},
            {
                "T": "n",
                "id": 24918784,
                "headline": "OpenAI Annualized Revenue Near $50B, Below Investor Estimates: FT",
                "summary": "<p>Shares of AI infrastructure names fall</p>",
                "author": "Benzinga Newsdesk",
                "created_at": "2026-10-08T17:01:10Z",
                "url": "https://www.benzinga.com/x",
                "symbols": ["NVDA", "orcl"],
                "source": "benzinga",
            },
        ]
    )
    (item,) = src.handle_message(raw)
    assert item.uid == "alpaca:24918784" and item.tickers == ["NVDA", "ORCL"]
    assert item.summary == "Shares of AI infrastructure names fall" and item.extra["wire"] == "benzinga"
    import pytest

    with pytest.raises(ConnectionError, match="402"):
        src.handle_message('[{"T":"error","code":402,"msg":"auth failed"}]')


async def test_alpaca_websocket_flow(http):
    from aiohttp.test_utils import TestServer

    received: list[dict] = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str('[{"T":"success","msg":"connected"}]')
        for _ in range(2):
            received.append(json.loads((await ws.receive()).data))
        await ws.send_str(
            json.dumps([{"T": "n", "id": 1, "headline": "Breaking: test headline", "symbols": ["AAPL"]}])
        )
        await asyncio.sleep(5)
        return ws

    app = web.Application()
    app.router.add_get("/news", ws_handler)
    srv = TestServer(app)
    await srv.start_server()
    try:
        src = AlpacaNewsSource(
            {"name": "alp", "key": "k", "secret": "s", "url": str(srv.make_url("/news"))},
            SourceContext(http=http, user_agent="t"),
        )
        got: list[NewsItem] = []
        stop = asyncio.Event()

        async def emit(item):
            got.append(item)
            stop.set()

        await asyncio.wait_for(src.run(emit, stop), 10)
    finally:
        await srv.close()
    assert received == [{"action": "auth", "key": "k", "secret": "s"}, {"action": "subscribe", "news": ["*"]}]
    assert got[0].title == "Breaking: test headline"


# --------------------------------------------------------------------------- defaults & guard


def test_push_sources_enabled_from_env(monkeypatch):
    monkeypatch.setenv("X_STREAM_ENABLED", "true")
    monkeypatch.setenv("X_BEARER_TOKEN", "tok")
    monkeypatch.setenv("ALPACA_ENABLED", "1")
    monkeypatch.setenv("ALPACA_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET", "s")
    cfg = build_config({})
    by = {s["name"]: s for s in cfg.sources}
    assert by["x-stream"]["enabled"] is True and by["x-stream"]["bearer_token"] == "tok"
    assert by["alpaca-news"]["enabled"] is True
    names = {s.name for s in build_sources(cfg.sources, CTX)}
    assert {"x-stream", "alpaca-news", "telegram-squawks", "ft-technology", "openai-youtube"} <= names
    monkeypatch.delenv("X_STREAM_ENABLED")
    assert {s["name"]: s for s in build_config({}).sources}["x-stream"]["enabled"] is False


async def test_hijacked_vip_account_not_fast_tracked(cfg):
    eng, cap = make_engine(cfg)
    src = XStreamSource({"name": "xs", "bearer_token": "t", "vip_accounts": ["OpenAINewsroom"]}, CTX)
    msg = json.loads(json.dumps(TWEET))
    msg["data"]["text"] = "Introducing $OPENAI token — airdrop live, connect wallet to claim"
    msg["includes"]["users"][0]["username"] = "OpenAINewsroom"
    await eng.on_item(src.handle_line(json.dumps(msg).encode())[0])
    await eng.drain()
    assert all(a.severity < Severity.HIGH for a in cap.alerts)


# --------------------------------------------------------------------------- replay: 2026-10-08


async def test_replay_openai_revenue_scoop(cfg):
    """The FT scoop that knocked 300+ points off the Nasdaq-100: a squawk relays it, we text
    it immediately, and the selloff that follows is tied back to it."""
    eng, cap = make_engine(cfg)
    x = XStreamSource({"name": "x-stream", "bearer_token": "t", "accounts": ["DeItaone"]}, CTX)
    tweet = x.handle_line(json.dumps(TWEET).encode())[0]
    tweet.published = time.time() - 2
    a = await eng.on_item(tweet)
    await eng.drain()
    assert a.severity >= Severity.HIGH, a.reasons
    assert "ai_lab_financials" in a.themes and {"NVDA", "ORCL", "CRWV"} <= set(a.tickers)
    text = sms_text(cap.alerts[0])
    assert text.startswith("🟠 @DeItaone: OPENAI TOLD INVESTORS") or text.startswith("🔴 @DeItaone")
    assert "NVDA" in text

    # the same story from a publisher feed a minute later: confirmation, not a duplicate text
    await eng.on_item(
        NewsItem(
            source="bloomberg-technology",
            title="OpenAI's Annualized Revenue Nears $50 Billion, FT Says",
            url="https://www.bloomberg.com/news/articles/2026-10-08/openai-s-annualized-revenue",
            tier=SourceTier.MEDIA,
            published=time.time(),
        )
    )
    await eng.drain()
    assert all(al.item is None or al.severity > cap.alerts[0].severity for al in cap.alerts[1:])

    # then the stocks fall: one combined alert that names the scoop as the likely cause
    cap.alerts.clear()
    now = time.time()
    await eng.on_moves(
        [
            PriceMove(s, c, 300, 90, 100, detected=now)
            for s, c in [("CRWV", -8.0), ("ORCL", -6.0), ("AMD", -5.0), ("NVDA", -3.1)]
        ]
    )
    await eng.drain()
    assert len(cap.alerts) == 1
    causes = [r["title"] for r in cap.alerts[0].related]
    assert causes and all("50" in t and "REVENUE" in t.upper() for t in causes[:2])
