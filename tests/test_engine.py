from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from news247.analysis.llm import LLMVerdict
from news247.config import Config
from news247.engine import Engine
from news247.models import NewsItem, PriceMove, Severity, SourceTier
from news247.notify import Dispatcher
from news247.storage import Storage
from news247.web.server import WebServer

from .conftest import CaptureNotifier


def make_engine(cfg: Config, llm=None, storage: Storage | None = None) -> tuple[Engine, CaptureNotifier]:
    cfg.market.enabled = False
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    cap = CaptureNotifier()
    disp.channels = [cap]
    eng = Engine(cfg, storage=storage or Storage(":memory:"), dispatcher=disp, sources=[], llm=llm)
    eng.coalesce_s = 0
    return eng, cap


def news(
    title: str, source: str = "openai-news", tier=SourceTier.PRIMARY, ents=("OpenAI",), **kw
) -> NewsItem:
    kw.setdefault("published", time.time() - 3)
    return NewsItem(
        source=source,
        title=title,
        tier=tier,
        url=f"https://ex.com/{source}/{abs(hash(title))}",
        extra={"entities": list(ents), **kw.pop("extra", {})},
        **kw,
    )


async def test_news_alert_end_to_end(cfg):
    eng, cap = make_engine(cfg)
    a = await eng.on_item(news("Introducing ChatGPT agents for accounting and legal teams"))
    await eng.drain()
    assert a.severity >= Severity.HIGH
    assert len(cap.alerts) == 1
    alert = cap.alerts[0]
    assert alert.kind == "news" and "CRM" in alert.tickers and alert.item.source == "openai-news"
    assert eng.storage.recent_alerts()[0]["title"] == alert.title
    # identical item again (e.g. after restart) is ignored
    assert await eng.on_item(news("Introducing ChatGPT agents for accounting and legal teams")) is None


async def test_low_score_items_stored_not_alerted(cfg):
    eng, cap = make_engine(cfg)
    await eng.on_item(news("5 AI stocks to buy now: here's why", "cnbc", SourceTier.MEDIA, ()))
    await eng.drain()
    assert cap.alerts == [] and eng.storage.recent_items()[0]["severity"] == "LOW"


async def test_confirmation_escalates_once(cfg):
    eng, cap = make_engine(cfg)
    await eng.on_item(news("OpenAI launches ChatGPT agents for accounting teams"))
    await eng.on_item(
        news("OpenAI launches ChatGPT agents for accounting teams, report says", "cnbc", SourceTier.MEDIA, ())
    )
    await eng.on_item(
        news("@DeItaone: *OPENAI LAUNCHES CHATGPT AGENTS FOR ACCOUNTING TEAMS", "x", SourceTier.PRIMARY, ())
    )
    await eng.on_item(
        news("OpenAI launches ChatGPT agents for accounting teams - live", "hn", SourceTier.SOCIAL, ())
    )
    await eng.drain()
    sev = [a.severity for a in cap.alerts]
    assert sev == sorted(sev) and len(sev) == len(set(sev)) >= 2  # only strictly increasing escalations
    assert cap.alerts[-1].severity is Severity.CRITICAL
    assert "Confirmed by" in cap.alerts[-1].body and "escalated" in cap.alerts[-1].body


async def test_backfill_not_pushed_unless_fresh(cfg):
    eng, cap = make_engine(cfg)
    await eng.on_item(news("Introducing GPT-6", published=time.time() - 1200, extra={"backfill": True}))
    await eng.on_item(
        news(
            "Federal Reserve issues FOMC statement",
            "fed",
            ents=("Federal Reserve",),
            published=time.time() - 60,
            extra={"backfill": True},
        )
    )
    await eng.drain()
    assert [a.item.source for a in cap.alerts] == ["fed"]
    assert len(eng.storage.recent_alerts()) == 2  # both visible on the dashboard


class FakeLLM:
    def __init__(self, verdict: LLMVerdict | None, delay: float = 0.0) -> None:
        self.verdict, self.delay, self.busy, self.calls = verdict, delay, False, 0

    async def assess(self, item, analysis):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.verdict

    def stats(self):
        return {"enabled": True}


async def test_llm_within_budget_enriches_alert(cfg):
    cfg.llm.budget_ms = 1000
    llm = FakeLLM(
        LLMVerdict(impact=9, direction="down", tickers=["INTU"], summary="Hits tax software"), delay=0.01
    )
    eng, cap = make_engine(cfg, llm=llm)
    await eng.on_item(news("Introducing GPT-6"))
    await eng.drain()
    assert llm.calls == 1 and len(cap.alerts) == 1
    an = cap.alerts[0].analysis
    assert an.llm_used and an.summary == "Hits tax software" and an.tickers[0] == "INTU"
    assert any("AI review" in r for r in an.reasons)


async def test_llm_slow_does_not_delay_alert_then_escalates(cfg):
    cfg.llm.budget_ms = 50
    llm = FakeLLM(LLMVerdict(impact=10, direction="down", summary="huge"), delay=0.3)
    eng, cap = make_engine(cfg, llm=llm)
    t0 = time.monotonic()
    await eng.on_item(news("Introducing GPT-6"))
    await asyncio.sleep(0.15)
    assert len(cap.alerts) == 1 and cap.alerts[0].severity is Severity.HIGH  # went out on time
    assert time.monotonic() - t0 < 0.3
    await eng.drain()
    assert len(cap.alerts) == 2 and cap.alerts[1].severity is Severity.CRITICAL
    assert "after AI review" in cap.alerts[1].body


async def test_llm_can_demote_before_alert(cfg):
    cfg.llm.budget_ms = 1000
    llm = FakeLLM(LLMVerdict(impact=0, direction="none", summary="marketing fluff"))
    eng, cap = make_engine(cfg, llm=llm)
    await eng.on_item(news("Introducing ChatGPT for Teams in Brazil"))
    await eng.drain()
    assert all(a.severity < Severity.HIGH for a in cap.alerts)


async def test_critical_never_waits_for_llm(cfg):
    llm = FakeLLM(None, delay=5)
    eng, cap = make_engine(cfg, llm=llm)
    await eng.on_item(
        news(
            "TRADING HALT — MARKET-WIDE CIRCUIT BREAKER Level 1", "nasdaq-halts", ents=(), extra={"boost": 90}
        )
    )
    await asyncio.sleep(0.05)
    assert cap.alerts and cap.alerts[0].severity is Severity.CRITICAL
    for t in list(eng._tasks):
        t.cancel()


async def test_price_moves_coalesce_and_find_catalyst(cfg):
    eng, cap = make_engine(cfg)
    await eng.on_item(news("Introducing ChatGPT agents for accounting and legal teams"))
    await eng.drain()
    cap.alerts.clear()
    now = time.time()
    moves = [
        PriceMove(s, -2.5 - i * 0.1, 60, 97.0, 100.0, detected=now)
        for i, s in enumerate(["CRM", "INTU", "NOW", "WDAY"])
    ]
    moves.append(PriceMove("AAPL", 2.0, 60, 102, 100, detected=now))
    await eng.on_moves(moves)
    await eng.drain()
    titles = [a.title for a in cap.alerts]
    assert titles[0].startswith("▼ 4 watched stocks moving together")
    assert titles[1] == "▲ AAPL +2.00% in 1m"
    combined = cap.alerts[0]
    assert combined.related and combined.related[0]["source"] == "openai-news"
    assert combined.url == combined.related[0]["url"]
    assert "No matching headline" in cap.alerts[1].body


async def test_group_move_suppresses_member_alerts(cfg):
    eng, cap = make_engine(cfg)
    now = time.time()
    group = PriceMove(
        "GROUP:semis", -2.4, 600, 0, 0, detected=now, members={"NVDA": -3.0, "AMD": -2.5, "AVGO": -1.8}
    )
    single = PriceMove("NVDA", -3.0, 300, 97, 100, detected=now)
    await eng.on_moves([single, group])
    await eng.drain()
    assert [a.title.split(" ")[1] for a in cap.alerts] == ["SEMIS"]


async def test_debounced_flush(cfg):
    eng, cap = make_engine(cfg)
    eng.coalesce_s = 0.05
    now = time.time()
    await eng.on_moves([PriceMove("CRM", -3, 60, 97, 100, detected=now)])
    await eng.on_moves([PriceMove("NOW", -3, 60, 97, 100, detected=now)])
    await eng.on_moves([PriceMove("ADBE", -3, 60, 97, 100, detected=now)])
    assert cap.alerts == []
    await eng.drain()
    assert len(cap.alerts) == 1 and "3 watched stocks" in cap.alerts[0].title


async def test_connectivity_alert(cfg):
    from news247.sources.base import PollingSource

    class Dead(PollingSource):
        async def fetch(self):
            return []

    eng, cap = make_engine(cfg)
    src = Dead({"name": "dead"}, None)  # type: ignore[arg-type]
    eng.sources = [src]
    eng.started = time.time() - 1000
    await eng._check_connectivity(time.time())
    src.health.last_ok = time.time()
    await eng._check_connectivity(time.time())
    await eng.drain()
    assert [a.kind for a in cap.alerts] == ["system", "system"]
    assert "reachable again" in cap.alerts[1].title


async def test_full_run_with_fake_source_and_stop(cfg, tmp_path):
    from news247.sources.base import PollingSource

    class OneShot(PollingSource):
        async def fetch(self):
            return [
                self.make_item(
                    title="Introducing GPT-6", url="https://openai.com/gpt-6", published=time.time() - 1
                )
            ]

    cfg.web.enabled = False
    eng, cap = make_engine(cfg, storage=Storage(tmp_path / "db.sqlite"))
    from news247.sources import SourceContext

    ctx = SourceContext(http=None, user_agent="t")  # type: ignore[arg-type]
    src = OneShot({"name": "openai-news", "interval": 0.01, "tier": "primary", "entities": ["OpenAI"]}, ctx)
    eng.sources = [src]
    stop = asyncio.Event()
    task = asyncio.create_task(eng.run(stop))
    for _ in range(200):
        if cap.alerts:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert cap.alerts and cap.alerts[0].title == "Introducing GPT-6"
    # persisted: a new engine on the same DB won't alert the same item again
    eng2, cap2 = make_engine(cfg, storage=Storage(tmp_path / "db.sqlite"))
    assert (
        await eng2.on_item(src.make_item(title="Introducing GPT-6", url="https://openai.com/gpt-6")) is None
    )


async def test_web_api_and_sse(cfg):
    cfg.web.token = "s3cret"
    eng, cap = make_engine(cfg)
    web = WebServer(eng, cfg.web)
    async with TestClient(TestServer(web.app)) as client:
        assert (await client.get("/api/status")).status == 401
        assert (await client.get("/health")).status == 200
        page = await client.get("/?token=s3cret")
        assert page.status == 200 and "NEWS 24/7" in await page.text()
        resp = await client.get("/events", headers={"Authorization": "Bearer s3cret"})
        assert (await resp.content.readline()).startswith(b"event: hello")
        await eng.on_item(news("Introducing GPT-6"))
        await eng.drain()
        seen = b""
        for _ in range(20):
            seen += await asyncio.wait_for(resp.content.readline(), 2)
            if b"event: alert" in seen:
                break
        assert b"event: item" in seen and b"event: alert" in seen
        resp.close()
        status = await (await client.get("/api/status?token=s3cret")).json()
        assert status["stats"]["alerts"] == 1 and status["llm"] == {"enabled": False}
        alerts = await (await client.get("/api/alerts?token=s3cret")).json()
        assert alerts[0]["title"] == "Introducing GPT-6"
        items = await (await client.get("/api/items?token=s3cret&min_score=50")).json()
        assert items[0]["tickers"]
        assert await (await client.get("/api/market?token=s3cret")).json() == {}
        assert isinstance(await (await client.get("/api/latency?token=s3cret")).json(), list)


def test_storage_roundtrip_and_stats(tmp_path):
    st = Storage(tmp_path / "x.db")
    from news247.models import Analysis

    it = NewsItem(source="s", title="t", url="https://a/b", published=time.time() - 30, detected=time.time())
    st.add_item(it, Analysis(score=50, severity=Severity.MEDIUM, tickers=["A"]))
    assert st.seen(it.uid)
    assert st.latency_stats()[0]["median_s"] == pytest.approx(30, abs=2)
    assert st.counts()["items_24h"] == 1
    assert st.prune(0) == 1
    st.close()
    assert Storage(tmp_path / "x.db").seen(it.uid) is False  # pruned rows are forgotten


def test_first_seen_stats(tmp_path):
    from news247.models import Analysis

    st = Storage(tmp_path / "x.db")
    now = time.time()
    an = Analysis(score=50, severity=Severity.MEDIUM)
    for i, (src, dt) in enumerate([("x-stream", 0), ("bloomberg", 60), ("google", 1800)]):
        st.add_item(
            NewsItem(source=src, title=f"t{i}", url=f"https://a/{i}", detected=now + dt), an, story_id=1
        )
    st.add_item(NewsItem(source="ft", title="solo", url="https://a/solo", detected=now), an, story_id=2)
    (w,) = st.first_seen_stats()
    assert w == {"source": "x-stream", "first": 1, "median_lead_s": pytest.approx(60, abs=1)}
