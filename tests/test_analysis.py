from __future__ import annotations

import asyncio

import pytest

from news247.analysis.dedup import StoryClusterer, tokens
from news247.analysis.llm import LLMClient, LLMVerdict, apply_verdict, build_user_prompt, parse_verdict
from news247.config import LLMConfig
from news247.models import Analysis, NewsItem, Severity


def ni(title: str, source: str = "s") -> NewsItem:
    return NewsItem(source=source, title=title)


# --------------------------------------------------------------------------- clustering


def test_tokens_normalize_synonyms_and_noise():
    assert tokens("OpenAI unveils GPT-6") == tokens("OpenAI launches GPT-6")
    assert "the" not in tokens("The stock market")
    assert tokens("@reuters.com: Nvidia's shares plunge") == tokens("Nvidia shares tumble")


def test_same_story_from_many_sources_clusters():
    c = StoryClusterer()
    s1, new1 = c.assign(ni("OpenAI unveils GPT-6, its most capable model", "openai"), ["OpenAI"])
    s2, new2 = c.assign(ni("OpenAI launches GPT-6 model", "cnbc"), ["OpenAI"])
    s3, new3 = c.assign(ni("OpenAI releases GPT-6 most capable model yet", "hn"), ["OpenAI"])
    assert new1 and not new2 and not new3
    assert s1 is s2 is s3 and s1.confirmations == 3 and s1.items == 3


def test_different_actors_do_not_merge():
    c = StoryClusterer()
    a, _ = c.assign(ni("Fed holds rates steady"), ["Federal Reserve"])
    b, new = c.assign(ni("ECB holds rates steady"), ["European Central Bank"])
    assert new and a is not b


def test_unrelated_titles_do_not_merge():
    c = StoryClusterer()
    c.assign(ni("Apple unveils new iPhone with AI features"))
    _, new = c.assign(ni("Tesla recalls Cybertruck over faulty panel"))
    assert new


def test_story_expiry():
    c = StoryClusterer(window_s=60)
    s1, _ = c.assign(ni("Nvidia to acquire Foo for $10 billion"), now=1000)
    s2, new = c.assign(ni("Nvidia to acquire Foo for $10 billion"), now=2000)
    assert new and s1 is not s2 and len(c) == 1


# --------------------------------------------------------------------------- LLM


def test_parse_verdict_variants():
    v = parse_verdict(
        '{"impact": 8, "direction": "DOWN", "tickers": ["$crm", "now", "bad ticker!"], "summary": "AI eats SaaS"}'
    )
    assert v.impact == 8 and v.direction == "down" and v.tickers == ["CRM", "NOW"]
    v = parse_verdict(
        'Sure! ```json\n{"impact": 14.6, "direction": "sideways", "tickers": [], "summary": "x"}\n```'
    )
    assert v.impact == 10 and v.direction == "mixed"
    with pytest.raises(ValueError):
        parse_verdict("no json here")


def test_apply_verdict_is_bounded():
    a = Analysis(score=90, severity=Severity.CRITICAL, tickers=["MSFT"], direction="unknown")
    delta = apply_verdict(
        a, LLMVerdict(impact=0, direction="down", tickers=["CRM"], summary="meh"), adjust=True
    )
    assert delta == -20  # can't drag a critical item to zero
    assert a.direction == "down" and a.tickers[:2] == ["CRM", "MSFT"] and a.llm_used and a.summary == "meh"
    b = Analysis(score=20, severity=Severity.LOW)
    assert apply_verdict(b, LLMVerdict(impact=10, direction="up"), adjust=True) == 25
    c = Analysis(score=20, severity=Severity.LOW, direction="up")
    assert apply_verdict(c, LLMVerdict(impact=10, direction="down"), adjust=False) == 0
    assert c.direction == "up"  # rules' clear direction kept


def test_user_prompt_contains_context():
    it = NewsItem(
        source="openai-news", title="Introducing X", summary="An agent for accountants", published=None
    )
    p = build_user_prompt(
        it, Analysis(score=50, severity=Severity.MEDIUM, entities=["OpenAI"], tickers=["INTU"])
    )
    assert "Introducing X" in p and "OpenAI" in p and "INTU" in p and "accountants" in p


async def test_ollama_and_openai_wire_formats(server, http):
    server.on(
        "/api/chat",
        {"message": {"content": '{"impact": 7, "direction": "down", "tickers": ["INTU"], "summary": "s"}'}},
    )
    server.on(
        "/v1/chat/completions",
        {
            "choices": [
                {"message": {"content": '{"impact": 3, "direction": "none", "tickers": [], "summary": "t"}'}}
            ]
        },
    )
    item = NewsItem(source="s", title="t")
    an = Analysis(score=40, severity=Severity.MEDIUM)

    ollama = LLMClient(LLMConfig(enabled=True, provider="ollama", base_url=server.url("")), http)
    v = await ollama.assess(item, an)
    assert v.impact == 7 and ollama.calls == 1
    body = server.requests[-1]["json"]
    assert body["stream"] is False and body["format"]["type"] == "object" and body["keep_alive"] == "24h"

    openai = LLMClient(
        LLMConfig(enabled=True, provider="openai", base_url=server.url("/v1"), api_key="k"), http
    )
    v = await openai.assess(item, an)
    assert v.impact == 3
    assert server.requests[-1]["headers"]["Authorization"] == "Bearer k"
    assert server.requests[-1]["json"]["response_format"] == {"type": "json_object"}
    assert openai.stats()["median_ms"] is not None


async def test_llm_failure_and_timeout_return_none(server, http):
    async def slow(request):
        await asyncio.sleep(2)

    server.on("/api/chat", slow)
    client = LLMClient(LLMConfig(enabled=True, base_url=server.url(""), timeout_s=0.2), http)
    assert (
        await client.assess(NewsItem(source="s", title="t"), Analysis(score=1, severity=Severity.LOW)) is None
    )
    assert client.failures == 1 and client.waiting == 0
    dead = LLMClient(LLMConfig(enabled=True, base_url="http://127.0.0.1:9"), http)
    assert (
        await dead.assess(NewsItem(source="s", title="t"), Analysis(score=1, severity=Severity.LOW)) is None
    )
