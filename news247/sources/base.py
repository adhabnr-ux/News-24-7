"""Source plumbing: polling loop, backoff, health tracking and first-poll baselining."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..http import BROWSER_HEADERS, HttpClient, HTTPError
from ..models import NewsItem, SourceTier

log = logging.getLogger(__name__)

Emit = Callable[[NewsItem], Awaitable[None]]


@dataclass
class SourceContext:
    http: HttpClient
    user_agent: str
    max_item_age_s: float = 1800.0
    data_dir: Path | None = None
    universe: Any = None  # market.universe.Universe: who a filing or release is about, and how big


@dataclass
class SourceHealth:
    polls: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    items: int = 0
    last_ok: float | None = None
    last_error: str = ""
    last_error_at: float | None = None
    last_item_at: float | None = None
    last_fetch_ms: float | None = None
    connected: bool | None = None  # streaming sources only
    latencies: list[float] = field(default_factory=list)  # recent publish->detect lags (s)

    def record_latency(self, lag: float | None) -> None:
        if lag is None:
            return
        self.latencies.append(lag)
        if len(self.latencies) > 200:
            del self.latencies[:100]

    @property
    def median_latency(self) -> float | None:
        if not self.latencies:
            return None
        s = sorted(self.latencies)
        return s[len(s) // 2]

    @property
    def status(self) -> str:
        if self.connected is False:
            return "down"
        if self.consecutive_errors >= 3:
            return "failing"
        if self.consecutive_errors:
            return "degraded"
        if self.last_ok is None and self.connected is None:
            return "starting"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "polls": self.polls,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
            "items": self.items,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_item_at": self.last_item_at,
            "last_fetch_ms": self.last_fetch_ms,
            "connected": self.connected,
            "median_latency_s": self.median_latency,
        }


class SeenSet:
    """Bounded insertion-ordered set."""

    def __init__(self, maxlen: int = 5000) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self.maxlen = maxlen

    def add(self, key: str) -> bool:
        """Add key; return True if it was new."""
        if key in self._d:
            return False
        self._d[key] = None
        if len(self._d) > self.maxlen:
            self._d.popitem(last=False)
        return True

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)


_TIER_NAMES = {t.name.lower(): t for t in SourceTier}


class Source(ABC):
    """A configured news source. Subclasses implement ``run`` (streaming) or ``fetch`` (polling)."""

    type_name = "base"
    default_interval = 60.0
    default_tier = SourceTier.MEDIA

    def __init__(self, cfg: dict[str, Any], ctx: SourceContext) -> None:
        self.cfg = cfg
        self.ctx = ctx
        self.name: str = cfg["name"]
        self.enabled: bool = bool(cfg.get("enabled", True))
        tier = cfg.get("tier")
        self.tier = _TIER_NAMES[str(tier).lower()] if tier else self.default_tier
        self.interval: float = float(cfg.get("interval", self.default_interval))
        self.entities: list[str] = list(cfg.get("entities", []))
        self.tickers: list[str] = [t.upper() for t in cfg.get("tickers", [])]
        self.boost: float = float(cfg.get("boost", 0))
        # VIP handling: items from this source (or from these authors/handles) are always pushed
        # at least at this severity, instantly — e.g. anything OpenAI posts on its own channels.
        floor = cfg.get("alert_floor")
        self.alert_floor: str | None = str(floor).upper() if floor else None
        self.vip_authors = {str(a).lower().lstrip("@") for a in cfg.get("vip_authors", [])}
        self.vip_floor: str = str(cfg.get("vip_floor", "high")).upper()
        self.browser: bool = bool(cfg.get("browser_headers", False))
        self.extra_headers: dict[str, str] = dict(cfg.get("headers", {}))
        inc = cfg.get("include")
        exc = cfg.get("exclude")
        self.include = re.compile(inc, re.I) if inc else None
        self.exclude = re.compile(exc, re.I) if exc else None
        self.health = SourceHealth()
        self.seen = SeenSet(int(cfg.get("seen_cache", 5000)))

    # ------------------------------------------------------------------ helpers

    @property
    def headers(self) -> dict[str, str]:
        base = dict(BROWSER_HEADERS) if self.browser else {}
        base.update(self.extra_headers)
        return base

    def make_item(self, **kw: Any) -> NewsItem:
        kw.setdefault("tier", self.tier)
        kw.setdefault("source", self.name)
        item = NewsItem(**kw)
        if self.tickers:
            item.tickers = list(dict.fromkeys([*item.tickers, *self.tickers]))
        if self.entities:
            item.extra.setdefault("entities", list(self.entities))
        if self.boost:
            item.extra["boost"] = self.boost
        if self.alert_floor:
            item.extra["floor"] = self.alert_floor
        elif self.vip_authors and item.author.lower().lstrip("@") in self.vip_authors:
            item.extra["floor"] = self.vip_floor
        return item

    def passes_filters(self, item: NewsItem) -> bool:
        text = item.text
        if self.include and not self.include.search(text):
            return False
        return not (self.exclude and self.exclude.search(text))

    def too_old(self, item: NewsItem, now: float | None = None) -> bool:
        if item.published is None:
            return False
        return (now or time.time()) - item.published > self.ctx.max_item_age_s

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type_name,
            "tier": self.tier.name,
            "interval": self.interval,
            "enabled": self.enabled,
            **self.health.to_dict(),
        }

    def _note_error(self, exc: BaseException) -> None:
        self.health.errors += 1
        self.health.consecutive_errors += 1
        self.health.last_error = f"{type(exc).__name__}: {exc}"[:300]
        self.health.last_error_at = time.time()

    def _note_ok(self) -> None:
        self.health.consecutive_errors = 0
        self.health.last_ok = time.time()

    async def _deliver(self, item: NewsItem, emit: Emit) -> bool:
        if not self.seen.add(item.uid):
            return False
        if not self.passes_filters(item):
            return False
        self.health.items += 1
        self.health.last_item_at = time.time()
        self.health.record_latency(item.latency)
        await emit(item)
        return True

    @abstractmethod
    async def run(self, emit: Emit, stop: asyncio.Event) -> None: ...

    async def check(self) -> list[NewsItem]:
        """One-shot fetch used by ``news247 check``; streaming sources override."""
        raise NotImplementedError


class PollingSource(Source):
    """Calls ``fetch`` every ``interval`` seconds with jitter and error backoff.

    The first successful poll is a *baseline*: anything without a publish date, or older
    than ``max_item_age``, is remembered but not emitted, so starting the monitor doesn't
    replay yesterday's feed. Fresh items from the baseline are emitted flagged ``backfill``.
    """

    max_backoff = 900.0

    @abstractmethod
    async def fetch(self) -> list[NewsItem]: ...

    async def check(self) -> list[NewsItem]:
        return await self.fetch()

    async def poll_once(self, emit: Emit, baseline: bool) -> int:
        t0 = time.monotonic()
        items = await self.fetch()
        self.health.last_fetch_ms = (time.monotonic() - t0) * 1000
        self.health.polls += 1
        now = time.time()
        emitted = 0
        # oldest first so downstream sees stories in publication order
        items.sort(key=lambda i: i.published or now)
        for item in items:
            if self.too_old(item, now):
                self.seen.add(item.uid)
                continue
            if baseline:
                if item.published is None:
                    self.seen.add(item.uid)
                    continue
                item.extra["backfill"] = True
            if await self._deliver(item, emit):
                emitted += 1
        self._note_ok()
        return emitted

    def burst_state(self, now: float | None = None) -> tuple[bool, float | None]:
        """(inside a burst window?, seconds until the next window opens) for scheduled releases.

        ``burst_times: ["08:30", "14:00"]`` (weekdays, ``burst_tz``, default US/Eastern): from
        ``burst_before`` s before to ``burst_after`` s after each time, poll every
        ``burst_interval`` s — CPI/jobs data and FOMC statements land at exact times."""
        times = self.cfg.get("burst_times") or []
        if not times:
            return False, None
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(str(self.cfg.get("burst_tz", "America/New_York")))
        before = float(self.cfg.get("burst_before", 10))
        after = float(self.cfg.get("burst_after", 120))
        now_dt = datetime.fromtimestamp(now if now is not None else time.time(), tz)
        soonest: float | None = None
        for day_offset in (0, 1, 2, 3):
            day = (now_dt + timedelta(days=day_offset)).date()
            if day.weekday() >= 5:  # releases are on weekdays
                continue
            for hhmm in times:
                h, m = (int(x) for x in str(hhmm).split(":"))
                t = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                delta = (t - now_dt).total_seconds()
                if -after <= delta <= before:
                    return True, 0.0
                start = delta - before
                if start > 0 and (soonest is None or start < soonest):
                    soonest = start
        return False, soonest

    def next_delay(self) -> float:
        errs = self.health.consecutive_errors
        if errs:
            return min(self.max_backoff, self.interval * (2 ** min(errs, 8))) * random.uniform(0.9, 1.1)
        delay = self.interval * random.uniform(0.9, 1.1)
        inside, until = self.burst_state()
        if inside:
            return float(self.cfg.get("burst_interval", 1.0))
        if until is not None and until < delay:
            return max(0.0, until)  # wake up exactly when the burst window opens
        return delay

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        baseline = True
        # stagger start-up so 40 sources don't all fire in the same instant
        await _sleep_or_stop(stop, random.uniform(0, min(5.0, self.interval)))
        while not stop.is_set():
            delay: float
            try:
                await self.poll_once(emit, baseline)
                baseline = False
                delay = self.next_delay()
            except asyncio.CancelledError:
                raise
            except HTTPError as exc:
                self._note_error(exc)
                delay = self.next_delay()
                if exc.retry_after:
                    delay = max(delay, min(exc.retry_after, self.max_backoff))
                _log_failure(self, exc)
            except Exception as exc:  # noqa: BLE001 - a source must never kill the monitor
                self._note_error(exc)
                delay = self.next_delay()
                _log_failure(self, exc)
            await _sleep_or_stop(stop, delay)


def _log_failure(src: Source, exc: BaseException) -> None:
    n = src.health.consecutive_errors
    # log loudly the first time and then only occasionally, to keep logs readable
    if n in (1, 3) or n % 20 == 0:
        log.warning("[%s] fetch failed (%d in a row): %s", src.name, n, src.health.last_error)
    else:
        log.debug("[%s] fetch failed: %s", src.name, exc)


async def _sleep_or_stop(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, delay))
    except asyncio.TimeoutError:
        pass
