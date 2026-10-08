"""The pipeline: sources → score → cluster → (LLM) → alert, plus price moves ↔ news correlation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from typing import Any

from .analysis.dedup import Story, StoryClusterer
from .analysis.llm import LLMClient, apply_verdict
from .analysis.scorer import Scorer
from .config import Config
from .http import HttpClient
from .market.detector import MoveDetector
from .market.prices import PriceMonitor
from .models import Alert, Analysis, NewsItem, PriceMove, Severity
from .notify import Dispatcher
from .notify.format import move_headline, window_label
from .sources import Source, SourceContext, build_sources
from .sources.base import _sleep_or_stop
from .storage import Storage
from .util import fmt_age, fmt_pct

log = logging.getLogger(__name__)

BACKFILL_PUSH_MAX_AGE = 300.0  # at start-up, only push items published in the last 5 minutes
CONFIRM_BONUS, CONFIRM_CAP = 6.0, 18.0
CATALYST_MIN_SCORE = 25.0
MULTI_MOVE_MIN = 3
HIJACK_RE = re.compile(
    r"\b(airdrop|presale|pre-sale|token (launch|sale)|claim (your|now)|connect (your )?wallet|giveaway|"
    r"\$[A-Z]{2,10} token|memecoin|meme coin|mint now)\b",
    re.I,
)  # this many stocks moving the same way in one batch -> one combined alert


class Engine:
    def __init__(
        self,
        cfg: Config,
        *,
        http: HttpClient | None = None,
        storage: Storage | None = None,
        dispatcher: Dispatcher | None = None,
        sources: list[Source] | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.http = http or HttpClient(
            cfg.general.user_agent, cfg.general.http_timeout_s, cfg.general.max_concurrency
        )
        self.storage = storage or Storage(cfg.data_path / "news247.db")
        self.scorer = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
        self.clusterer = StoryClusterer(window_s=cfg.scoring.cluster_window_minutes * 60)
        self.dispatcher = dispatcher or Dispatcher(cfg.notify, self.http)
        self.detector = MoveDetector(cfg.market)
        self.prices = PriceMonitor(cfg.market, self.http, self.detector) if cfg.market.enabled else None
        self.llm = llm if llm is not None else (LLMClient(cfg.llm, self.http) if cfg.llm.enabled else None)
        ctx = SourceContext(
            http=self.http,
            user_agent=cfg.general.user_agent,
            max_item_age_s=cfg.general.max_item_age_minutes * 60,
            data_dir=cfg.data_path,
        )
        self.sources = sources if sources is not None else build_sources(cfg.sources, ctx)
        self.recent: deque[tuple[NewsItem, Analysis]] = deque()
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.started = time.time()
        self.stats = {"items": 0, "alerts": 0, "pushed": 0, "price_moves": 0, "llm_reviewed": 0}
        self._all_down_alerted = False
        self.coalesce_s = 2.0
        self._move_buffer: list[PriceMove] = []
        self._flush_task: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def run(self, stop: asyncio.Event) -> None:
        from .web.server import WebServer  # local import: aiohttp.web is only needed when serving

        await self.http.start()
        web = WebServer(self, self.cfg.web) if self.cfg.web.enabled else None
        if web:
            try:
                await web.start()
            except OSError as exc:  # e.g. port already in use: keep monitoring without the dashboard
                log.error(
                    "dashboard disabled: cannot listen on %s:%d (%s)",
                    self.cfg.web.host,
                    self.cfg.web.port,
                    exc,
                )
                web = None
        log.info(
            "News247 running: %d sources, market=%s (%d symbols), llm=%s, channels=%s",
            len(self.sources),
            self.cfg.market.provider if self.prices else "off",
            len(self.cfg.market.symbols) if self.prices else 0,
            f"{self.cfg.llm.model}@{self.cfg.llm.base_url}" if self.llm else "off",
            ", ".join(f"{c.name}≥{c.min_severity.name.lower()}" for c in self.dispatcher.channels) or "none",
        )
        runners = [asyncio.create_task(s.run(self.on_item, stop), name=f"src:{s.name}") for s in self.sources]
        if self.prices:
            runners.append(asyncio.create_task(self.prices.run(self.on_moves, stop), name="prices"))
        runners.append(asyncio.create_task(self._maintenance(stop), name="maintenance"))
        try:
            await stop.wait()
        finally:
            for t in runners:
                t.cancel()
            await asyncio.gather(*runners, return_exceptions=True)
            await self.drain(timeout=5)
            if web:
                await web.stop()
            await self.http.close()
            self.storage.close()
            log.info("News247 stopped")

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed: %r", task.exception())

    async def drain(self, timeout: float = 30) -> None:
        """Wait for in-flight LLM reviews and notifications (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while self._tasks and time.monotonic() < deadline:
            await asyncio.wait(set(self._tasks), timeout=max(0.01, deadline - time.monotonic()))

    # ------------------------------------------------------------------ news pipeline

    async def on_item(self, item: NewsItem) -> Analysis | None:
        if self.storage.seen(item.uid):
            return None
        self.stats["items"] += 1
        analysis = self.scorer.score(item)
        story, is_new = self.clusterer.assign(item, analysis.entities)
        if not is_new and story.confirmations >= 2:
            bonus = min(CONFIRM_CAP, CONFIRM_BONUS * (story.confirmations - 1))
            self.scorer.rescore(analysis, bonus, f"confirmed by {story.confirmations} sources")
        vip = self._apply_floor(item, analysis)
        self.storage.add_item(item, analysis, story.id)
        self._remember(item, analysis)
        self._broadcast("item", {"item": item.to_dict(), "analysis": analysis.to_dict(), "story": story.id})

        if self.llm and analysis.score >= self.cfg.llm.min_score and not self.llm.busy:
            task = self._spawn(self.llm.assess(item, analysis))
            # CRITICAL and VIP-source items go out instantly; the model only annotates afterwards
            instant = vip or analysis.severity >= Severity.CRITICAL
            wait = 0.0 if instant else self.cfg.llm.budget_ms / 1000
            if wait:
                # wait for the model in the background so this source keeps flowing
                self._spawn(self._finish_with_llm(item, analysis, story, task, wait))
                return analysis
            await self._finalize(item, analysis, story)
            self._spawn(self._late_llm(item, analysis, story, task))
            return analysis
        await self._finalize(item, analysis, story)
        return analysis

    def _apply_floor(self, item: NewsItem, analysis: Analysis) -> bool:
        """Raise items from VIP sources/authors to their guaranteed severity. Returns True if VIP."""
        floor_name = item.extra.get("floor")
        if not floor_name or any(r.startswith("muted") for r in analysis.reasons):
            return False
        if HIJACK_RE.search(item.text):
            # official accounts get hijacked to shill crypto (e.g. @OpenAINewsroom, Sep 2024):
            # never fast-track those, let the normal score decide
            analysis.reasons.append("VIP floor skipped: looks like a crypto/airdrop scam post")
            return False
        floor = Severity.parse(floor_name)
        if analysis.severity < floor:
            threshold = {
                Severity.MEDIUM: self.cfg.scoring.medium,
                Severity.HIGH: self.cfg.scoring.high,
                Severity.CRITICAL: self.cfg.scoring.critical,
            }.get(floor, 0.0)
            self.scorer.rescore(analysis, threshold - analysis.score, f"VIP source: always {floor.name}")
        return True

    async def _finish_with_llm(
        self, item: NewsItem, analysis: Analysis, story: Story, task: asyncio.Task[Any], wait: float
    ) -> None:
        done, _ = await asyncio.wait({task}, timeout=wait)
        if done:
            self._apply_llm(item, analysis, task.result())
            await self._finalize(item, analysis, story)
        else:
            await self._finalize(item, analysis, story)
            await self._late_llm(item, analysis, story, task)

    async def _late_llm(
        self, item: NewsItem, analysis: Analysis, story: Story, task: asyncio.Task[Any]
    ) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            verdict = await task
            if verdict is None:
                return
            self._apply_llm(item, analysis, verdict)
            await self._finalize(item, analysis, story, late=True)

    def _apply_llm(self, item: NewsItem, analysis: Analysis, verdict: Any) -> None:
        if verdict is None:
            return
        self.stats["llm_reviewed"] += 1
        delta = apply_verdict(analysis, verdict, self.cfg.llm.adjust)
        if delta:
            self.scorer.rescore(analysis, delta, f"AI review (impact {verdict.impact}/10)")
        self.storage.update_analysis(item.uid, analysis)
        self._broadcast("item_update", {"uid": item.uid, "analysis": analysis.to_dict()})

    async def _finalize(self, item: NewsItem, analysis: Analysis, story: Story, late: bool = False) -> None:
        story.max_score = max(story.max_score, analysis.score)
        severity = self.cfg.scoring.severity_for(story.max_score)
        if severity < Severity.MEDIUM:
            return
        if story.alerted is not None and severity <= story.alerted:
            return
        escalation = story.alerted is not None
        story.alerted = severity
        analysis.severity = severity
        notes = []
        if story.confirmations >= 2:
            notes.append(f"Confirmed by {story.confirmations} sources")
        if escalation:
            notes.append("⬆ escalated" + (" after AI review" if late else ""))
        push = True
        if item.extra.get("backfill"):
            age = time.time() - item.published if item.published else None
            if age is None or age > BACKFILL_PUSH_MAX_AGE:
                push = False
        alert = Alert(
            kind="news",
            severity=severity,
            title=item.title,
            body=" · ".join(notes),
            url=item.url,
            tickers=analysis.tickers[:12],
            item=item,
            analysis=analysis,
            related=self._price_context(analysis.tickers),
        )
        story.alert_id = alert.id
        self.storage.mark_alerted(item.uid)
        await self.publish(alert, push=push)

    def _remember(self, item: NewsItem, analysis: Analysis) -> None:
        self.recent.append((item, analysis))
        cutoff = time.time() - self.cfg.market.correlate_minutes * 60
        while self.recent and self.recent[0][0].detected < cutoff:
            self.recent.popleft()

    def _price_context(self, tickers: list[str]) -> list[dict[str, Any]]:
        parts = []
        for t in tickers[:12]:
            c = self.detector.change(t, 300)
            if c is not None and abs(c) >= 0.25:  # only show tickers that are actually moving
                parts.append(f"{t} {fmt_pct(c)}")
        if not parts:
            return []
        return [{"kind": "price", "text": " · ".join(parts) + " (5m)"}]

    # ------------------------------------------------------------------ price pipeline

    def find_catalysts(self, symbols: list[str], now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        wanted = set(symbols)
        hits: list[tuple[float, NewsItem, Analysis]] = []
        cutoff = now - self.cfg.market.correlate_minutes * 60
        for item, an in self.recent:
            if item.detected < cutoff or an.score < CATALYST_MIN_SCORE:
                continue
            overlap = wanted & set(an.tickers)
            if not overlap:
                continue
            recency = 1.0 - (now - item.detected) / (self.cfg.market.correlate_minutes * 60)
            hits.append((an.score * (0.5 + 0.5 * recency) + 3 * len(overlap), item, an))
        hits.sort(key=lambda h: h[0], reverse=True)
        return [
            {
                "kind": "news",
                "title": it.title,
                "source": it.source,
                "url": it.url,
                "score": an.score,
                "age": fmt_age(now - it.detected),
            }
            for _, it, an in hits[:3]
        ]

    async def on_moves(self, moves: list[PriceMove]) -> None:
        """Buffer moves briefly so a sector-wide selloff becomes one alert, not twenty."""
        self._move_buffer.extend(moves)
        if self.coalesce_s <= 0:
            await self.flush_moves()
        elif self._flush_task is None or self._flush_task.done():
            self._flush_task = self._spawn(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self.coalesce_s)
        await self.flush_moves()

    async def flush_moves(self) -> None:
        moves, self._move_buffer = self._move_buffer, []
        if not moves:
            return
        # evaluate baskets right now so they land in the same batch as their members
        known = {m.symbol for m in moves}
        moves += [g for g in self.detector.check_groups() if g.symbol not in known]
        groups = [m for m in moves if m.is_group]
        covered = {(sym, g.direction) for g in groups for sym in g.members}
        singles = [m for m in moves if not m.is_group and (m.symbol, m.direction) not in covered]
        self.stats["price_moves"] += len(moves)

        for g in groups:
            await self.publish(self._move_alert(g))
        for direction in ("down", "up"):
            same = [m for m in singles if m.direction == direction and m.window_s < 86400]
            if len(same) >= MULTI_MOVE_MIN:
                await self.publish(self._multi_alert(same))
                singles = [m for m in singles if m not in same]
        for m in singles:
            await self.publish(self._move_alert(m))

    def _move_alert(self, mv: PriceMove) -> Alert:
        severity = self.detector.severity(mv)
        syms = list(mv.members) if mv.is_group else [mv.symbol]
        catalysts = self.find_catalysts(syms, mv.detected)
        if mv.is_group:
            body = ", ".join(f"{s} {fmt_pct(c)}" for s, c in list(mv.members.items())[:10])
        elif mv.window_s >= 86400:
            body = f"{mv.price:.2f} vs. previous close {mv.ref_price:.2f}"
        else:
            body = f"{mv.price:.2f} (from {mv.ref_price:.2f} {window_label(mv.window_s)} ago)"
            if mv.relative_pct is not None:
                body += f" · vs {self.cfg.market.benchmark} {fmt_pct(mv.relative_pct)} relative"
        if not catalysts:
            body += "\nNo matching headline yet — the news may not have hit public feeds."
        url = (
            catalysts[0]["url"]
            if catalysts
            else ("" if mv.is_group else f"https://finance.yahoo.com/quote/{mv.symbol}")
        )
        return Alert(
            kind="price",
            severity=severity,
            title=move_headline(mv),
            body=body,
            url=url,
            tickers=syms[:12],
            move=mv,
            related=catalysts,
        )

    def _multi_alert(self, moves: list[PriceMove]) -> Alert:
        moves = sorted(moves, key=lambda m: abs(m.change_pct), reverse=True)
        avg = sum(m.change_pct for m in moves) / len(moves)
        severity = max(self.detector.severity(m) for m in moves)
        syms = [m.symbol for m in moves]
        catalysts = self.find_catalysts(syms, moves[0].detected)
        arrow = "▼" if avg < 0 else "▲"
        body = ", ".join(f"{m.symbol} {fmt_pct(m.change_pct)}/{window_label(m.window_s)}" for m in moves[:12])
        if not catalysts:
            body += "\nNo matching headline yet — the news may not have hit public feeds."
        combined = PriceMove(
            "GROUP:watchlist",
            round(avg, 3),
            max(m.window_s for m in moves),
            0.0,
            0.0,
            detected=moves[0].detected,
            members={m.symbol: m.change_pct for m in moves},
        )
        return Alert(
            kind="price",
            severity=severity,
            title=f"{arrow} {len(moves)} watched stocks moving together (avg {fmt_pct(avg)})",
            body=body,
            url=catalysts[0]["url"] if catalysts else "",
            tickers=syms[:12],
            move=combined,
            related=catalysts,
        )

    # ------------------------------------------------------------------ output

    async def publish(self, alert: Alert, push: bool = True) -> None:
        self.stats["alerts"] += 1
        self.storage.add_alert(alert)
        self._broadcast("alert", alert.to_dict())
        if push:
            self.stats["pushed"] += 1
            self._spawn(self.dispatcher.dispatch(alert))
        else:
            log.info("startup backfill (dashboard only): %s", alert.title[:100])

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        msg = {"event": event, "data": data}
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow browser tab: drop rather than block the pipeline

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(q)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "uptime_s": time.time() - self.started,
            "stats": dict(self.stats),
            "stories_tracked": len(self.clusterer),
            "sources": [s.describe() for s in self.sources],
            "market": {
                "enabled": self.prices is not None,
                "provider": self.cfg.market.provider,
                "mode": self.prices.mode if self.prices else None,
                **(self.prices.health.to_dict() if self.prices else {}),
            },
            "llm": self.llm.stats() if self.llm else {"enabled": False},
            "channels": self.dispatcher.describe(),
            "db": self.storage.counts(),
        }

    async def _maintenance(self, stop: asyncio.Event) -> None:
        last_prune = 0.0
        while not stop.is_set():
            await _sleep_or_stop(stop, 60)
            now = time.time()
            if now - last_prune > 3600:
                last_prune = now
                with contextlib.suppress(Exception):
                    n = self.storage.prune(self.cfg.general.db_retention_days)
                    if n:
                        log.info("pruned %d old items", n)
            await self._check_connectivity(now)

    async def _check_connectivity(self, now: float) -> None:
        """Warn (once) if *no* source has succeeded for 5 minutes — usually a dead network."""
        if now - self.started < 300 or not self.sources:
            return
        last_ok = max((s.health.last_ok or 0.0) for s in self.sources)
        down = now - last_ok > 300
        if down and not self._all_down_alerted:
            self._all_down_alerted = True
            await self.publish(
                Alert(
                    kind="system",
                    severity=Severity.HIGH,
                    title="News247: no source has responded for 5+ minutes",
                    body="Check the machine's internet connection. Monitoring resumes automatically.",
                )
            )
        elif not down and self._all_down_alerted:
            self._all_down_alerted = False
            await self.publish(
                Alert(
                    kind="system",
                    severity=Severity.HIGH,
                    title="News247: sources are reachable again",
                    body="",
                )
            )
