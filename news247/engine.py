"""The pipeline: sources → score → cluster → (LLM) → alert, plus price moves ↔ news correlation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections import deque
from typing import Any

from .analysis.dedup import Story, StoryClusterer
from .analysis.llm import LLMClient, apply_verdict
from .analysis.scorer import Scorer
from .config import Config
from .edge import Calendar, EdgeDesk, brief_push, build_brief
from .http import HttpClient
from .market.detector import MoveDetector
from .market.prices import PriceMonitor
from .market.radar import MoversRadar, RadarHit, SmallCapFeed
from .market.universe import Universe, fmt_cap
from .models import Alert, Analysis, NewsItem, PriceMove, Severity, SourceTier
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
        self.edge = EdgeDesk(self.scorer)
        self.calendar = Calendar()
        # the little things: every listed company's size, so small caps are scored by how big
        # the news is for them, plus a radar for small caps moving before any headline
        sc = cfg.smallcap
        self.universe = SmallCapFeed.load_cached(cfg.data_path) or Universe()
        self.radar: MoversRadar | None = None
        self.smallcap_feed: SmallCapFeed | None = None
        self.radar_seen: dict[str, tuple[float, float]] = {}  # symbol -> (flagged at, % move)
        if sc.enabled:
            self.scorer.attach_universe(self.universe, **sc.filters())
            if cfg.market.enabled:
                self.radar = MoversRadar(sc, self.universe) if sc.radar else None
                self.smallcap_feed = SmallCapFeed(
                    sc,
                    self.http,
                    self.universe,
                    cfg.data_path,
                    self.radar,
                    market_phase=lambda now: self.calendar.market_status(now)["phase"],
                )
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
        saved_phone = self.storage.get_setting("phone")
        if saved_phone:  # a number saved on the dashboard's Setup page wins over IMESSAGE_TO
            self.dispatcher.set_phone(saved_phone.split(","))
        self.relay_hub = next(
            (getattr(c, "hub", None) for c in self.dispatcher.channels if c.name == "relay"), None
        )
        if self.relay_hub is not None:
            self.relay_hub.attach(self.storage)
            self.relay_hub.on_inbound = self.handle_phone_command
        wa = next((c for c in self.dispatcher.channels if c.name == "whatsapp"), None)
        self.whatsapp = getattr(wa, "session", None)
        self._wa_channel = wa
        self._wa_save_soon = asyncio.Event()
        if wa is not None and self.whatsapp is not None:
            wa.attach(cfg.data_path)  # type: ignore[attr-defined]
            self.whatsapp.on_inbound = self.handle_phone_command
            self.whatsapp.on_state = self._whatsapp_state
        wac = next((c for c in self.dispatcher.channels if c.name == "whatsapp_cloud"), None)
        self.whatsapp_cloud = getattr(wac, "cloud", None)
        if self.whatsapp_cloud is not None:
            self.whatsapp_cloud.on_inbound = self.handle_phone_command
        self.state_store = None
        if cfg.general.state_db:
            from .statestore import StateStore

            self.state_store = StateStore(cfg.general.state_db)
        self.webpush = next((c for c in self.dispatcher.channels if c.name == "webpush"), None)
        if self.webpush is not None:
            from .web.public_pages import contact_email

            contact = contact_email(cfg.general.user_agent, os.environ.get("CONTACT_EMAIL", ""))
            subject = f"mailto:{contact}" if contact else (cfg.web.public_url or "")
            self.webpush.attach(self.storage, secret=cfg.web.token, state=self.state_store, subject=subject)  # type: ignore[attr-defined]
        self._load_phone_controls()
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
        self._spawn(asyncio.to_thread(self.edge.warm))  # score the history off the event loop
        if self.webpush is not None:
            await self.webpush.restore()  # type: ignore[attr-defined]  # fresh container: phones come back
        snap = getattr(self._wa_channel, "snapshot", None)
        if self.whatsapp is not None:
            if snap is not None:
                await snap.restore()  # fresh container: put the saved pairing back first
            await self.whatsapp.start()
        runners = [asyncio.create_task(s.run(self.on_item, stop), name=f"src:{s.name}") for s in self.sources]
        if self.prices:
            runners.append(asyncio.create_task(self.prices.run(self.on_moves, stop), name="prices"))
        runners.append(asyncio.create_task(self._maintenance(stop), name="maintenance"))
        runners.append(asyncio.create_task(self._edge_loop(stop), name="edge"))
        if self.smallcap_feed is not None:
            runners.append(asyncio.create_task(self.smallcap_feed.run(self.on_radar, stop), name="smallcap"))
        if self.whatsapp_cloud is not None:
            runners.append(asyncio.create_task(self.whatsapp_cloud.maintain(stop), name="whatsapp-cloud"))
        if self.cfg.general.keepalive_url:
            runners.append(asyncio.create_task(self._keepalive(stop), name="keepalive"))
        if snap is not None and self.whatsapp is not None:
            runners.append(asyncio.create_task(snap.run(stop, self._wa_save_soon), name="whatsapp-state"))
        try:
            await stop.wait()
        finally:
            for t in runners:
                t.cancel()
            await asyncio.gather(*runners, return_exceptions=True)
            await self.drain(timeout=5)
            if self.relay_hub is not None:
                await self.relay_hub.close()
            if self.whatsapp is not None:
                await self.whatsapp.stop()
                if snap is not None:
                    await snap.save()  # last copy before the host wipes the disk
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
        if not is_new and story.alert_id and self.edge.is_mainstream(item.source):
            self._record_lead(story, item)
        if not is_new and story.unconfirmed and story.alerted is not None and not item.extra.get("relay"):
            await self._confirm(item, story)
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
        elif item.extra.get("relay") and not escalation:
            # Apr 7 2025: a squawk's misread "90-day pause" swung the S&P by 8% before it was denied
            story.unconfirmed = True
            notes.insert(0, "⚠️ UNCONFIRMED — single social/squawk post, no official source yet")
        if escalation:
            notes.append("⬆ escalated" + (" after AI review" if late else ""))
        radar = self._radar_note(analysis.tickers[:4], time.time())
        if radar:
            notes.append(radar)
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
        self.edge.annotate(alert, self.detector.last_price)
        self._track_symbols(alert.edge.get("play", {}).get("direct", []))
        if not story.alert_id:
            story.alert_created = alert.created
        story.alert_id = alert.id
        self.storage.mark_alerted(item.uid)
        await self.publish(alert, push=push)

    async def _confirm(self, item: NewsItem, story: Story) -> None:
        """A real outlet or the primary source now reports a story we flagged as unconfirmed."""
        story.unconfirmed = False
        alert = Alert(
            kind="news",
            severity=story.alerted or Severity.HIGH,
            title=f"✅ CONFIRMED: {item.title}",
            body=f"Now reported by {item.source} ({item.tier.name.lower()}) — {story.confirmations} sources in total",
            url=item.url,
            item=item,
        )
        await self.publish(alert)

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

    # ------------------------------------------------------------------ the radar

    async def on_radar(self, hits: list[RadarHit]) -> None:
        for hit in hits:
            self.radar_seen[hit.symbol] = (hit.detected, hit.quote.change_pct or 0.0)
            alert = self._radar_alert(hit)
            await self.publish(alert)
            if self.radar is not None and hit.symbol in self.radar.board:
                self.radar.board[hit.symbol]["alert_id"] = alert.id  # the board row opens this alert
        cutoff = time.time() - 12 * 3600
        for sym, (ts, _) in list(self.radar_seen.items()):
            if ts < cutoff:
                del self.radar_seen[sym]

    def _radar_alert(self, hit: RadarHit) -> Alert:
        q = hit.quote
        d = hit.to_dict()
        pct = q.change_pct or 0.0
        arrow = "▲" if hit.direction == "up" else "▼"
        when = {"pre": " pre-market", "post": " after hours"}.get(q.session, "")
        if hit.kind == "jump" and hit.jump_pct is not None:
            headline = (
                f"{fmt_pct(hit.jump_pct)} in {window_label(hit.jump_window_s)} ({fmt_pct(pct)} on the day)"
            )
        else:
            headline = f"{fmt_pct(pct)}{when}"
        name = (q.name or hit.symbol)[:48]
        title = f"{arrow} {hit.symbol} {headline} · {name} · {d['cap']} {d['band']}"
        parts = []
        if q.price and q.prev_close:
            parts.append(f"${q.price:,.2f} vs ${q.prev_close:,.2f} prev close")
        if q.dollar_volume:
            vol = f"{fmt_cap(q.dollar_volume)} traded"
            if q.rvol and q.rvol >= 1.5:
                vol += f" ({q.rvol:.0f}× normal volume)"
            parts.append(vol)
        body = " · ".join(parts)
        if hit.flags:
            body += "\n⚠ " + "; ".join(hit.flags)
        catalysts = self.find_catalysts([hit.symbol], hit.detected)
        if not catalysts:
            body += "\nNo headline yet — on the radar before the news. Watch halts, 8-Ks and the wires."
        move = PriceMove(
            hit.symbol,
            round(hit.jump_pct if hit.kind == "jump" and hit.jump_pct is not None else pct, 3),
            hit.jump_window_s if hit.kind == "jump" else 86400,
            float(q.price or 0.0),
            float(hit.ref_price or q.prev_close or 0.0),
            detected=hit.detected,
        )
        alert = Alert(
            kind="price",
            severity=Severity.parse(hit.severity),
            title=title,
            body=body,
            url=catalysts[0]["url"] if catalysts else f"https://finance.yahoo.com/quote/{hit.symbol}",
            tickers=[hit.symbol],
            move=move,
            related=catalysts,
        )
        alert.edge["radar"] = d
        alert.edge["smallcap"] = {
            "symbol": hit.symbol,
            "name": d["name"],
            "cap": d["cap"],
            "band": d["band"],
            "market_cap": d["market_cap"],
            "label": "Radar: moving before the news" if not catalysts else "Radar: moving on the news",
            "direction": hit.direction,
            "radar": True,
        }
        self._track_symbols([hit.symbol])
        return alert

    def _radar_note(self, tickers: list[str], now: float) -> str:
        for t in tickers:
            seen = self.radar_seen.get(t)
            if seen and 0 <= now - seen[0] <= 6 * 3600:
                return (
                    f"📡 Radar flagged {t} {fmt_pct(seen[1])} {fmt_age(now - seen[0])} before this headline"
                )
        return ""

    def radar_board(self, limit: int = 40) -> dict[str, Any]:
        rows = self.radar.snapshot(limit) if self.radar is not None else []
        now = time.time()
        for r in rows:
            r["news"] = self.find_catalysts([r["symbol"]], now)[:1]
        return {
            "enabled": self.radar is not None,
            "market": self.calendar.market_status(now),
            "rows": rows,
            "status": self.smallcap_feed.status() if self.smallcap_feed is not None else None,
        }

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

    # ------------------------------------------------------------------ setup

    def set_phone(self, number: str) -> list[str]:
        numbers = [n for n in (x.strip() for x in number.split(",")) if n]
        applied = self.dispatcher.set_phone(numbers)
        self.storage.set_setting("phone", ",".join(applied))
        return applied

    def phone_status(self) -> dict[str, Any]:
        chans = self.dispatcher.phone_channels
        return {
            "channels": [
                {
                    "name": c.name,
                    "min_severity": c.min_severity.name,
                    "sent": c.sent,
                    "failed": c.failed,
                    "last_error": c.last_error,
                    "backup": c.backup,
                }
                for c in chans
            ],
            "phone": (chans[0].recipients if chans else []),  # type: ignore[attr-defined]
            "from_number": next(
                (getattr(c, "from_number", "") for c in chans if getattr(c, "from_number", "")), ""
            ),
            "phone_mode": self.phone_mode(),
            "relay": self.relay_hub.status() if self.relay_hub is not None else None,
        }

    async def send_test(self) -> dict[str, str]:
        item = NewsItem(
            source="news247-test",
            title="News247 test: alerts will arrive here",
            url="",
            tier=SourceTier.PRIMARY,
            published=time.time(),
        )
        analysis = Analysis(
            score=99, severity=Severity.CRITICAL, summary="If you can read this, setup worked."
        )
        alert = Alert(
            kind="system",
            severity=Severity.CRITICAL,
            title=item.title,
            body="Test message",
            item=item,
            analysis=analysis,
        )
        results = await asyncio.gather(
            *(self.dispatcher._send(c, alert) for c in self.dispatcher.phone_channels)
        )
        return dict(zip((c.name for c in self.dispatcher.phone_channels), results))

    # ------------------------------------------------------------------ texting the relay

    def _whatsapp_state(self, state: str, detail: str) -> None:
        """Tell you (through the other channels) when WhatsApp needs attention."""
        from .whatsapp.session import BANNED, CONNECTED, LOGGED_OUT

        if state in (CONNECTED, LOGGED_OUT):
            self._wa_save_soon.set()  # newly paired (or unlinked): back the session up soon
        if state not in (LOGGED_OUT, BANNED) or detail == "unlinked from the dashboard":
            return
        title = (
            "News247: WhatsApp was unlinked, pair it again on the Setup page"
            if state == LOGGED_OUT
            else "News247: WhatsApp restricted the sending account"
        )
        self._spawn(self.publish(Alert(kind="system", severity=Severity.HIGH, title=title, body=detail)))

    def _load_phone_controls(self) -> None:
        until = self.storage.get_setting("pause_until")
        if until:
            self.dispatcher.paused_until = float(until)
        level = self.storage.get_setting("phone_min")
        if level:
            self.dispatcher.set_phone_min(Severity.parse(level))

    def _tz(self) -> Any:
        from zoneinfo import ZoneInfo

        name = (self.cfg.notify.quiet_hours or {}).get("timezone") or "America/New_York"
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - unknown zone: fall back to the machine's
            return None

    def phone_mode(self) -> str:
        d = self.dispatcher
        if d.paused:
            if d.paused_until == float("inf"):
                return "paused until you text RESUME"
            from datetime import datetime

            when = datetime.fromtimestamp(d.paused_until, self._tz()).strftime("%I:%M %p %Z").lstrip("0")
            return f"paused until {when}"
        if d.phone_min is not None:
            return f"{d.phone_min.name} and above"
        return "normal"

    async def handle_phone_command(self, sender: str, text: str) -> str | None:
        """Runs a command texted to the relay and returns the reply (None = not a command)."""
        from .relay.commands import HELP, parse_command

        cmd = parse_command(text)
        if cmd is None:
            return None
        log.info("phone command from %s: %s", sender, text[:60])
        d = self.dispatcher
        if cmd.action == "pause":
            until = d.pause(cmd.seconds)
            self.storage.set_setting("pause_until", "inf" if until == float("inf") else str(until))
            return f"⏸ Texts {self.phone_mode()}. The dashboard keeps recording. Text RESUME to restart."
        if cmd.action == "resume":
            d.resume()
            self.storage.set_setting("pause_until", "0")
            return f"▶️ Texts are back on ({self.phone_mode()})."
        if cmd.action == "level":
            sev = Severity.parse(cmd.level) if cmd.level else None
            d.set_phone_min(sev)
            self.storage.set_setting("phone_min", sev.name if sev else "")
            if sev is Severity.CRITICAL:
                return "OK: only CRITICAL alerts by text now. Text NORMAL to undo."
            if sev is Severity.MEDIUM:
                return "OK: MEDIUM alerts by text too (more messages). Text NORMAL to undo."
            return "OK: back to normal (important + critical alerts)."
        if cmd.action == "status":
            return self.status_text()
        return HELP

    def status_text(self) -> str:
        sources = [s.describe() for s in self.sources]
        ok = sum(1 for s in sources if s["status"] in ("ok", "starting"))
        last = self.storage.recent_alerts(1)
        last_txt = (
            f"last: {last[0]['title'][:80]} ({fmt_age(time.time() - last[0]['created'])} ago)"
            if last
            else "none yet"
        )
        counts = self.storage.counts()
        return (
            f"✅ News247 up {fmt_age(time.time() - self.started)} · {ok}/{len(sources)} sources OK\n"
            f"{counts['alerts_24h']} alerts in 24h, {last_txt}\n"
            f"Texts: {self.phone_mode()}"
        )

    # ------------------------------------------------------------------ status

    def whatsapp_status(self) -> dict[str, Any] | None:
        if self.whatsapp is None:
            return None
        from .whatsapp.neonize_backend import qr_svg

        st = self.whatsapp.status(qr_svg)
        snap = getattr(self._wa_channel, "snapshot", None)
        st["state_db"] = snap.status() if snap is not None else None
        return st

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
            "smallcap": (
                self.smallcap_feed.status()
                if self.smallcap_feed is not None
                else {"listings": len(self.universe), "radar": {"enabled": False}}
            ),
            "channels": self.dispatcher.describe(),
            "phone_mode": self.phone_mode(),
            "relay": self.relay_hub.status() if self.relay_hub is not None else None,
            "whatsapp": self.whatsapp_status(),
            "whatsapp_cloud": self.whatsapp_cloud.status() if self.whatsapp_cloud is not None else None,
            "db": self.storage.counts(),
        }

    async def _keepalive(self, stop: asyncio.Event) -> None:
        """Request our own public URL so free hosts that sleep when idle keep us running."""
        url = self.cfg.general.keepalive_url.rstrip("/") + "/health"
        log.info("keep-alive: requesting %s every %.0f s", url, self.cfg.general.keepalive_s)
        while not stop.is_set():
            await _sleep_or_stop(stop, self.cfg.general.keepalive_s)
            if stop.is_set():
                break
            try:
                await self.http.get(url, timeout_s=30)
                self.stats["keepalive_ok"] = self.stats.get("keepalive_ok", 0) + 1
            except Exception as exc:  # noqa: BLE001 - next round tries again
                log.warning("keep-alive request failed: %s", exc)

    # ------------------------------------------------------------------ the edge desk

    def _record_lead(self, story: Story, item: NewsItem) -> None:
        """A mainstream outlet just carried a story Foretape already alerted on: that's the lead."""
        alerted_at = getattr(story, "alert_created", 0.0) or 0.0
        lead = item.detected - alerted_at
        if not alerted_at or lead < 5 or not self.storage.add_lead(story.alert_id, item.source, lead):
            return
        patch = {"lead": {"source": item.source, "lead_s": round(lead, 1), "at": item.detected}}
        if self.storage.update_alert_edge(story.alert_id, patch) is not None:
            self._broadcast("edge", {"id": story.alert_id, **patch})
            log.info(
                "edge: %s carried story %s %s after Foretape", item.source, story.alert_id, fmt_age(lead)
            )

    def _track_symbols(self, symbols: list[str]) -> None:
        """Start quoting an alert's direct tickers so 'since the alert' moves can be shown."""
        if not self.prices or self.cfg.market.provider != "yahoo":
            return
        tracked = self.cfg.market.symbols
        for sym in symbols[:4]:
            if sym not in tracked and len(tracked) < 150 and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
                tracked.append(sym)

    def price_series(self, alert: dict[str, Any], points: int = 90) -> dict[str, list[list[float]]]:
        """Recent prices of an alert's direct tickers (from just before the alert to now), for the
        app's chart. Only what the price monitor still holds in memory (about the last hour)."""
        play = (alert.get("edge") or {}).get("play") or {}
        since = float(alert.get("created", 0)) - 900
        out: dict[str, list[list[float]]] = {}
        for sym in (play.get("direct") or [])[:3]:
            pts = [[t, p] for t, p in self.detector.history.get(sym, ()) if t >= since]
            if len(pts) < 3:
                continue
            step = max(1, len(pts) // points)
            sampled = pts[::step]
            if sampled[-1] is not pts[-1]:
                sampled.append(pts[-1])
            out[sym] = [[round(t, 1), round(p, 4)] for t, p in sampled]
        return out

    def edge_since(self, alert: dict[str, Any]) -> dict[str, float]:
        return EdgeDesk.since((alert.get("edge") or {}).get("refs") or {}, self.detector.last_price)

    def brief(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        return build_brief(
            self.storage.recent_alerts(200, since=now - 4 * 86400),
            self.calendar,
            self.detector.snapshot(),
            self.storage.leads(since=now - 7 * 86400),
            now,
        )

    def _brief_due(self, now: float) -> bool:
        from datetime import datetime

        from .edge import ET

        if self.webpush is None or not getattr(self.webpush, "subs", None):
            return False
        at = str(self.webpush.options.get("brief_time", "08:15") or "").strip()
        if not at:
            return False
        dt = datetime.fromtimestamp(now, ET)
        if dt.weekday() >= 5 or dt.date().isoformat() in self.calendar.holidays:
            return False
        h, m = (int(x) for x in at.split(":"))
        if (dt.hour, dt.minute) < (h, m) or dt.hour >= h + 2:  # a missed brief isn't sent hours late
            return False
        return self.storage.get_setting("brief_sent") != dt.date().isoformat()

    async def send_brief(self, now: float | None = None) -> bool:
        from datetime import datetime

        from .edge import ET

        now = now or time.time()
        self.storage.set_setting("brief_sent", datetime.fromtimestamp(now, ET).date().isoformat())
        payload = brief_push(self.brief(now))
        if payload is None or self.webpush is None or self.dispatcher.paused:
            return False
        results = await self.webpush.push(payload, urgency="normal")  # type: ignore[attr-defined]
        return any(r == "ok" for r in results.values())

    async def _edge_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await _sleep_or_stop(stop, 20)
            now = time.time()
            with contextlib.suppress(Exception):
                for aid, refs in self.edge.fill_refs(self.detector.price_at, now).items():
                    if self.storage.update_alert_edge(aid, {"refs": refs}) is not None:
                        self._broadcast("edge", {"id": aid, "refs": refs})
            if self._brief_due(now):
                try:
                    await self.send_brief(now)
                except Exception as exc:  # noqa: BLE001 - the brief must never stop the engine
                    log.warning("morning brief failed: %s", exc)

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
