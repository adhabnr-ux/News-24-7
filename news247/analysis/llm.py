"""Optional local-LLM triage (Ollama, LM Studio, llama.cpp server, vLLM, …).

The rule-based scorer decides first — it is instant and never goes down. The model then
acts as a second opinion: it rates impact 0-10, names affected tickers, gives direction
and writes a one-line "why it matters". Its opinion can move the score by a bounded amount,
so a confused model can never silence a market-wide circuit breaker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import LLMConfig
from ..http import HttpClient
from ..models import Analysis, NewsItem

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a real-time markets news triage desk. For each headline decide how likely it is \
to move US stock prices within the next hour, and which stocks.
Rules:
- impact 0-10: 0-2 noise/opinion/marketing, 3-4 minor company news, 5-6 notable for one stock, 7-8 \
moves a sector or a mega-cap, 9-10 moves the whole market (Fed surprise, war, circuit breaker, huge AI launch \
that threatens an industry).
- Think about second-order effects: e.g. an AI lab launching an agent for accounting can hit INTU, an AI \
coding launch can hit software stocks, a cheap open model can hit NVDA.
- tickers: US tickers most affected (max 8), most affected first.
- direction: "up", "down", "mixed" or "none" for the main affected stocks.
- Old news, recaps, previews, opinion, law-firm class-action ads and event schedules are impact <= 2.
Reply with JSON only: {"impact": int, "direction": str, "tickers": [str], "summary": str (<= 25 words, why it \
matters to traders), "category": str}"""

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "impact": {"type": "integer", "minimum": 0, "maximum": 10},
        "direction": {"type": "string", "enum": ["up", "down", "mixed", "none"]},
        "tickers": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "category": {"type": "string"},
    },
    "required": ["impact", "direction", "tickers", "summary"],
}

_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z])?$")


@dataclass
class LLMVerdict:
    impact: int
    direction: str
    tickers: list[str] = field(default_factory=list)
    summary: str = ""
    category: str = ""
    latency_ms: float = 0.0


def build_user_prompt(item: NewsItem, analysis: Analysis) -> str:
    age = ""
    if item.published:
        age = f"\nPublished: {max(0, time.time() - item.published) / 60:.0f} min ago"
    hints = ""
    if analysis.entities:
        hints += f"\nEntities detected: {', '.join(analysis.entities)}"
    if analysis.tickers:
        hints += f"\nCandidate tickers: {', '.join(analysis.tickers[:10])}"
    summary = f"\nSummary: {item.summary[:700]}" if item.summary else ""
    return f"Source: {item.source} ({item.tier.name.lower()}){age}\nHeadline: {item.title}{summary}{hints}"


def parse_verdict(content: str) -> LLMVerdict:
    """Parse model output, tolerating code fences and chatter around the JSON."""
    content = content.strip()
    try:
        data = json.loads(content)
    except ValueError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise ValueError(f"no JSON in model output: {content[:200]!r}") from None
        data = json.loads(m.group(0))
    impact = int(round(float(data.get("impact", 0))))
    impact = max(0, min(10, impact))
    direction = str(data.get("direction", "none")).lower()
    if direction not in ("up", "down", "mixed", "none"):
        direction = "mixed"
    tickers = []
    for t in data.get("tickers") or []:
        t = str(t).strip().upper().lstrip("$")
        if _TICKER_RE.match(t):
            tickers.append(t)
    return LLMVerdict(
        impact=impact,
        direction=direction,
        tickers=list(dict.fromkeys(tickers))[:8],
        summary=str(data.get("summary", "")).strip()[:300],
        category=str(data.get("category", "")).strip()[:40],
    )


class LLMClient:
    def __init__(self, cfg: LLMConfig, http: HttpClient) -> None:
        self.cfg = cfg
        self.http = http
        self._sem = asyncio.Semaphore(max(1, cfg.max_concurrency))
        self.waiting = 0
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.latencies: list[float] = []

    @property
    def busy(self) -> bool:
        """True when the model is backed up — callers should skip low-priority items."""
        return self.waiting > 4 * max(1, self.cfg.max_concurrency)

    def _endpoint(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if self.cfg.provider == "ollama":
            return f"{base}/api/chat"
        return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"

    def _payload(self, user: str) -> dict[str, Any]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        if self.cfg.provider == "ollama":
            return {
                "model": self.cfg.model,
                "messages": messages,
                "stream": False,
                "format": JSON_SCHEMA,
                "keep_alive": "24h",  # keep weights in memory: cold loads cost seconds
                "options": {"temperature": self.cfg.temperature, "num_predict": 200},
            }
        return {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
        }

    async def complete(self, user: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        resp = await self.http.post(
            self._endpoint(), json=self._payload(user), headers=headers, timeout_s=self.cfg.timeout_s
        )
        data = resp.json()
        if self.cfg.provider == "ollama":
            return data["message"]["content"]
        return data["choices"][0]["message"]["content"]

    async def assess(self, item: NewsItem, analysis: Analysis) -> LLMVerdict | None:
        self.waiting += 1
        acquired = False
        try:
            async with self._sem:
                self.waiting -= 1
                acquired = True
                t0 = time.monotonic()
                try:
                    content = await asyncio.wait_for(
                        self.complete(build_user_prompt(item, analysis)), timeout=self.cfg.timeout_s
                    )
                    verdict = parse_verdict(content)
                except Exception as exc:  # noqa: BLE001 - the model is optional; never break the pipeline
                    self.failures += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                    if self.failures in (1, 5) or self.failures % 50 == 0:
                        log.warning("LLM triage failed (%d so far): %s", self.failures, self.last_error)
                    return None
                verdict.latency_ms = (time.monotonic() - t0) * 1000
                self.calls += 1
                self.latencies = (self.latencies + [verdict.latency_ms])[-100:]
                return verdict
        finally:
            if not acquired:
                self.waiting -= 1

    def stats(self) -> dict[str, Any]:
        lat = sorted(self.latencies)
        return {
            "enabled": True,
            "model": self.cfg.model,
            "provider": self.cfg.provider,
            "calls": self.calls,
            "failures": self.failures,
            "last_error": self.last_error,
            "median_ms": lat[len(lat) // 2] if lat else None,
            "queue": self.waiting,
        }


def apply_verdict(analysis: Analysis, verdict: LLMVerdict, adjust: bool) -> float:
    """Merge the model's opinion into ``analysis``; returns the score delta to apply."""
    analysis.llm_used = True
    analysis.summary = verdict.summary
    # the model decides direction only where the rules had no clear view
    if verdict.direction in ("up", "down", "mixed") and analysis.direction in ("unknown", "mixed"):
        analysis.direction = verdict.direction
    if verdict.tickers:
        analysis.tickers = list(dict.fromkeys(verdict.tickers + analysis.tickers))[:12]
    if not adjust:
        return 0.0
    target = verdict.impact * 10.0
    # bounded: the model can nudge, not overrule (max -20 / +25)
    return max(-20.0, min(25.0, (target - analysis.score) * 0.5))
