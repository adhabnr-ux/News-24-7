"""The pipeline: sources → score → cluster → (LLM) → alert, plus price moves ↔ news correlation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections import deque
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from .analysis.catalyst_dates import extract_event
from .analysis.dedup import Story, StoryClusterer
from .analysis.llm import LLMClient, apply_verdict
from .analysis.scorer import Scorer
from .config import CASH_HOURS_ONLY_QUIET, PROXIES, Config
from .desk import BURST_WINDOW_S
from .edge import ET, Calendar, EdgeDesk, brief_push, build_brief
from .http import HttpClient
from .market.detector import MoveDetector
from .market.follow import FollowEvent, Play
from .market.ignition import IgnitionHit, IgnitionScanner
from .market.options import MISPRINT_LABEL, OptionsFeed, OptionsHit, OptionsRadar, prev_session
from .market.prices import PriceMonitor
from .market.radar import MoversRadar, RadarHit, SmallCapFeed
from .market.tape import TapeFeed
from .market.universe import Universe, fmt_cap
from .market.why import WhyHunter
from .models import Alert, Analysis, NewsItem, PriceMove, Severity, SourceTier
from .notify import Dispatcher
from .notify.channels import HELD_TEXT
from .notify.format import move_headline, window_label
from .scorecard import Scorecard, scorecard_line
from .sources import Source, SourceContext, build_sources
from .sources.base import _sleep_or_stop
from .storage import Storage
from .trends import TrendDesk, TrendSignal, signal_alert_text
from .util import fmt_age, fmt_pct

log = logging.getLogger(__name__)

BACKFILL_PUSH_MAX_AGE = 300.0  # at start-up, only push items published in the last 5 minutes
CONFIRM_BONUS, CONFIRM_CAP = 6.0, 18.0
KW_MIN_SCORE = 12.0  # a keyword push still needs a headline the scorer found some substance in
MARKET = "SPY"  # what every grade is also read against (market-adjusted records)
CONFIRM_ODDS = 0.8  # live odds this high, an hour or more in, and the news is "holding" (liveodds.py)
CATALYST_MIN_SCORE = 25.0
OVERNIGHT_MIN_SCORE = 35.0  # MEDIUM and up: kept back to the previous session for the radar/options
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
        self.line_default = (
            cfg.scoring.medium
        )  # the alert line as configured; an override moves it (shadow.py)
        with contextlib.suppress(Exception):
            from .shadow import apply_line

            apply_line(self)
        self.line_offsets: dict[str, float] = {}  # each kind of news' line, from the main one (linekinds.py)
        with contextlib.suppress(Exception):
            from .linekinds import offsets as line_offsets

            self.line_offsets = line_offsets(self)
        self.scorer = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
        self.edge = EdgeDesk(self.scorer)
        self.calendar = Calendar()
        today = datetime.now(ET).date().isoformat()
        for ev in self.storage.catalysts(today):  # binary events captured before a restart
            self.calendar.add(ev)
        # the little things: every listed company's size, so small caps are scored by how big
        # the news is for them, plus a radar for small caps moving before any headline
        sc = cfg.smallcap
        self.universe = SmallCapFeed.load_cached(cfg.data_path) or Universe()
        # after every big move: did we have it first? (scorecard.py)
        self.scorecard = Scorecard(self.storage, self.universe, self.calendar.holidays)
        self.why = WhyHunter(self.http, cfg.general.user_agent, sc.why_per_hour) if sc.why_lookup else None
        self.radar: MoversRadar | None = None
        self.smallcap_feed: SmallCapFeed | None = None
        self.radar_seen: dict[str, tuple[float, float]] = {}  # symbol -> (flagged at, % move)
        # every live stock price read anywhere (radar sweep, Alpaca snapshots), shared with the
        # options tape so its movers and model values come from now, not the 10-minute universe
        from .market.live import LiveQuotes

        self.live_quotes = LiveQuotes()
        if sc.enabled:
            self.scorer.attach_universe(self.universe, **sc.filters())
            if cfg.market.enabled:
                self.radar = (
                    MoversRadar(sc, self.universe, state_path=cfg.data_path / "radar_state.json")
                    if sc.radar
                    else None
                )
                self.smallcap_feed = SmallCapFeed(
                    sc,
                    self.http,
                    self.universe,
                    cfg.data_path,
                    self.radar,
                    market_phase=lambda now: self.calendar.market_status(now)["phase"],
                    watch=lambda: self.calendar.binary_symbols(),
                    live=self.live_quotes,
                )
        # the options tape: unusual volume and contracts up thousands of percent on every $1B+
        # company (needs the universe the small-cap feed keeps fresh)
        self.options_radar: OptionsRadar | None = None
        self.options_feed: OptionsFeed | None = None
        if cfg.options.enabled and self.smallcap_feed is not None:
            self.options_radar = OptionsRadar(cfg.options, state_path=cfg.data_path / "options_state.json")
            self.options_feed = OptionsFeed(
                cfg.options,
                self.http,
                self.universe,
                self.options_radar,
                market_phase=lambda now: self.calendar.market_status(now)["phase"],
                movers=lambda: list(self.radar.board) if self.radar is not None else [],
                live=self.live_quotes,
            )
        # ignition: the whole market's live tape, read for stocks starting to move (market/ignition.py)
        self.ignition: IgnitionScanner | None = None
        self.tape: TapeFeed | None = None
        self.options_seen: dict[str, tuple[float, str, str]] = {}  # symbol -> (when, up/down, what)
        self.ignition_pending: dict[str, tuple[IgnitionHit, float]] = {}  # unconfirmed, waiting on its chain
        self.ignition_day: dict[str, Any] = {"day": "", "pushed": 0, "shown": 0}
        if self.smallcap_feed is not None and sc.radar and sc.ignition:
            self.ignition = IgnitionScanner(sc)
            alpaca = None
            pace = None
            if self.options_feed is not None:
                alpaca = next(
                    (x for x in self.options_feed.sources if getattr(x, "name", "") == "alpaca"), None
                )
                pace = self.options_feed._pace  # one budget for Alpaca's 200 requests a minute
            elif cfg.options.alpaca_key and cfg.options.alpaca_secret:
                from .market.options import AlpacaSource

                alpaca = AlpacaSource(cfg.options, self.http)
            self.tape = TapeFeed(
                sc,
                self.http,
                self.universe,
                self.ignition,
                live=self.live_quotes,
                market_phase=lambda now: self.calendar.market_status(now)["phase"],
                alpaca=alpaca,
                pace=pace,
                yahoo_json=self.smallcap_feed.yahoo_json,
                data_dir=cfg.data_path,
            )
            if self.options_feed is not None and self.tape.provider == "alpaca":
                self.options_feed.tape_driven = True  # the tape already reads every price: no second sweep
        # the trend lane: what consumers are doing (Wikipedia attention, App Store climbs, tracker
        # data, sell-outs), tied to the listed company before its earnings report says it (trends.py)
        self.trends: TrendDesk | None = None
        if cfg.trends.enabled:
            self.trends = TrendDesk(
                cfg.trends,
                self.http,
                self.universe,
                self.storage,
                state_path=cfg.data_path / "trends_state.json",
            )
            from .camillo import ChatterTags, EarningsBook, NarrativeWatch, load_narratives

            narratives = load_narratives() if cfg.trends.narratives else []
            self.trends.attach_camillo(
                EarningsBook(self.http) if cfg.trends.earnings else None,
                ChatterTags(self.trends.brands, cfg.trends.chatter_sample_s, narratives=narratives)
                if cfg.trends.chatter
                else None,
                NarrativeWatch(narratives, cfg.trends) if narratives else None,
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
            universe=self.universe,
            live=self.live_quotes,
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
        # your desk: each phone's watchlist, push preferences, price alerts and tracked calls (desk.py)
        from .desk import Desk

        self.desk = Desk(self.storage, self.state_store)
        self.catalyst_rates: dict[str, Any] = {}  # (catalyst, size band) -> graded record (catalysts.py)
        self.regime_rates: dict[str, Any] = {}  # (catalyst, market regime) -> graded record
        self._regime_now: tuple[float, str] = (0.0, "unknown")  # the live regime, read every 5 minutes
        self.odds: dict[str, Any] = {}  # the calibrated odds model and its out-of-sample test (odds.py)
        self.firm_rates: dict[str, Any] = {}  # analyst firm x kind of call -> graded record (firms.py)
        self.h1_rates: dict[str, Any] = {}  # move after an hour -> how the day finished (liveodds.py)
        self.heat_rates: dict[str, Any] = {}  # how far winners dipped and losers ran first (heat.py)
        self.flow_rates: dict[str, Any] = {}  # options flow by shape -> graded record (flowrec.py)
        self.persona_rates: dict[str, Any] = {}  # each stock's own news record and its trait (persona.py)
        self.clock_rates: dict[str, Any] = {}  # how fast each kind of news plays out (newsclock.py)
        self.open_rates: dict[str, Any] = {}  # outside-session news: the gap, and after the open (opening.py)
        self.insider_rates: dict[str, Any] = {}  # insider buys by who, how much, how unusual (insiders.py)
        self.drift_rates: dict[str, Any] = {}  # each kind of news a month on: drift or give back (drift.py)
        self.coverage_rates: dict[str, Any] = {}  # how late to the story, how far it spread (coverage.py)
        self.call_rates: dict[str, Any] = {}  # the call's own record by verb (thecall.py)
        self.climate_rates: dict[str, Any] = {}  # each kind's last two weeks against its record (climate.py)
        self.call_replay: dict[str, Any] = {}  # the call's rule run on every graded alert (thecall.py)
        self.macro_rates: dict[str, Any] = {}  # each kind on Fed and jobs days vs other days (macrodays.py)
        self.vol_rates: dict[str, Any] = {}  # options cheap or rich against the news, graded (volnews.py)
        self.call_muted: list[str] = []  # facts the replay shows sorting nothing (thecall.py)
        self.pair_rates: dict[str, Any] = {}  # who follows whose news, per pair (peerlearn.py)
        self.wait_rates: dict[str, Any] = {}  # did waiting for the open or a pullback pay (waits.py)
        self.float_rates: dict[str, Any] = {}  # news by float and short interest, graded (floatshort.py)
        # news on stocks selling shares or with a shelf (dilution.py)
        self.dilution_rates: dict[str, Any] = {}
        self.catchup_rates: dict[str, Any] = {}  # what's left after the mainstream catches up (catchup.py)
        self.halt_rates: dict[str, Any] = {}  # what stocks did after a halt resumed (haltrec.py)
        self.leg_rates: dict[str, Any] = {}  # new highs well after the news, graded (secondleg.py)
        self.theme_rates: dict[str, Any] = {}  # ETFs after market-wide headlines, per theme (macrotape.py)
        # news by relative volume, at the alert and 15 minutes in (volconfirm.py)
        self.rvol_rates: dict[str, Any] = {}
        self.turn_rates: dict[str, Any] = {}  # calls that turned during the day, graded (callnow.py)
        # into FDA decisions and readouts: the run-up, graded (runup.py)
        self.runup_rates: dict[str, Any] = {}
        self.entry_rates: dict[str, Any] = {}  # every entry style per kind of news, side by side (entries.py)
        self.tape_rates: dict[str, Any] = {}  # cold, normal and hot tapes: the rest of the day (tapeday.py)
        self.overnight_rates: dict[
            str, Any
        ] = {}  # close to the next close, by kind and standing (overnight.py)
        self.ev_hist: tuple[list[float], int] = (
            [],
            1,
        )  # 10 days of alerts' expected moves, and how many days
        self._odds_at = 0.0  # when it was last fitted (hourly)
        self.base_rates: dict[
            str, Any
        ] = {}  # record_key -> graded stats (dossier.track_record), for each new alert
        self.implied_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # symbol -> (when, implied move)
        self.confluence_day: dict[str, Any] = {"day": "", "fired": [], "pushed": 0}
        self.earnings_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # symbol -> (when, reactions)
        self._kw_sent: dict[tuple[str, int], float] = {}  # (device, story id) -> when a keyword push went
        self._board_seen: dict[str, Any] = {"day": "", "keys": []}  # watchlist board pushes sent today
        self._sympathy_day: dict[str, Any] = {"day": "", "keys": [], "count": {}}  # sympathy pushes today
        self._retest_best: dict[
            str, float
        ] = {}  # alert id -> its best move its way so far today (second chance)
        self._rule_refs: dict[
            tuple[str, str], float
        ] = {}  # (symbol, headline uid) -> price when the rule first saw it
        self._vwap_side: dict[str, str] = {}  # symbol -> "above" | "below" VWAP at the last check
        self.day_marks: dict[
            str, list[tuple[float, float]]
        ] = {}  # SPY/QQQ every 5 minutes today (the day timeline)
        if self.webpush is not None:
            self.webpush.allow = lambda sub, alert: self.desk.allows(sub.device, alert)  # type: ignore[attr-defined]
            self.webpush.hold = lambda sub, alert: self._held(sub.device, alert)  # type: ignore[attr-defined]
            self.webpush.forward = lambda device, payload: self._spawn(self._forward(device, payload))  # type: ignore[attr-defined]
            self.webpush.why = lambda sub, alert: self.desk.block_reason(sub.device, alert)  # type: ignore[attr-defined]
        self.desk.calendar = self.calendar
        self._load_phone_controls()
        self.recent: deque[tuple[NewsItem, Analysis]] = deque()
        # meaningful news kept back to the previous session, so a move at 09:45 can cite the
        # deal announced at 18:00 the evening before (CCI, Oct 8-9 2026)
        self.recent_long: deque[tuple[NewsItem, Analysis]] = deque(maxlen=3000)
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
        with contextlib.suppress(Exception):
            await self.desk.restore()
        with contextlib.suppress(Exception):
            from .uptime import on_start as uptime_start
            from .uptime import restore as uptime_restore
            from .uptime import wake_later

            await uptime_restore(self)
            outage = uptime_start(self)  # how long was it down, and how much of that was the session?
            if outage:
                self._spawn(wake_later(self, outage))
        snap = getattr(self._wa_channel, "snapshot", None)
        if self.whatsapp is not None:
            if snap is not None:
                await snap.restore()  # fresh container: put the saved pairing back first
            await self.whatsapp.start()
        runners = [asyncio.create_task(s.run(self.on_item, stop), name=f"src:{s.name}") for s in self.sources]
        if self.prices:
            runners.append(asyncio.create_task(self.prices.run(self.on_moves, stop), name="prices"))
        runners.append(asyncio.create_task(self._maintenance(stop), name="maintenance"))
        runners.append(asyncio.create_task(self._desk_loop(stop), name="desk"))
        runners.append(asyncio.create_task(self._edge_loop(stop), name="edge"))
        if self.smallcap_feed is not None:
            runners.append(asyncio.create_task(self.smallcap_feed.run(self.on_radar, stop), name="smallcap"))
        if self.tape is not None:
            runners.append(asyncio.create_task(self.tape.run(self.on_ignition, stop), name="tape"))
        if self.options_feed is not None:
            runners.append(
                asyncio.create_task(
                    self.options_feed.run(self.on_options, stop, self.on_options_problem, self.on_follow),
                    name="options",
                )
            )
        if self.trends is not None and self.cfg.trends.scan:
            runners.append(asyncio.create_task(self.trends.run(self.on_trends, stop), name="trends"))
        if self.trends is not None and self.trends.narratives is not None:
            runners.append(asyncio.create_task(self._narrative_loop(stop), name="narratives"))
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
        self._capture_catalyst(item, analysis)
        if self.trends is not None:
            touched = self.trends.from_news(item, analysis)
            if touched:
                self._spawn(self._trend_check(touched))
        self._broadcast("item", {"item": item.to_dict(), "analysis": analysis.to_dict(), "story": story.id})
        if not item.extra.get("backfill"):
            if self.desk.profiles and any(p.get("keywords") for p in self.desk.profiles.values()):
                self._spawn(self._keyword_push(item, analysis, story.id))
            if (
                item.tickers
                and ("target" in item.title.lower() or " PT" in item.title)
                and self.desk.profiles
            ):
                self._spawn(self._target_push(item))
            if (item.extra.get("halt_code") or item.extra.get("halt_resume")) and item.tickers:
                with contextlib.suppress(Exception):
                    from .haltrec import on_halt, on_resume

                    # the halt's price and direction, or when it trades again: graded after (haltrec.py)
                    (on_resume if item.extra.get("halt_resume") else on_halt)(self, item)
                self._spawn(self._halt_watch(item, analysis))

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
        kline, kind = None, None
        if self.line_offsets and severity <= Severity.MEDIUM:  # its kind of news may have its own line
            with contextlib.suppress(Exception):
                from .linekinds import severity_line

                clears, kline, kind = severity_line(self, item.title, analysis.tickers, story.max_score)
                severity = Severity.MEDIUM if clears else min(severity, Severity.LOW)
        if severity < Severity.MEDIUM:
            with contextlib.suppress(Exception):
                from .shadow import record as shadow_record

                # just under the line: graded in the shadow
                shadow_record(self, item, analysis, story.max_score, line=kline, kind=kind)
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
        if analysis.score >= OVERNIGHT_MIN_SCORE and analysis.tickers:
            self.recent_long.append((item, analysis))
            # a long weekend at most, counted from this item's detection (= now when live) so a
            # replayed day keeps its own overnight news whatever the wall clock says
            old = item.detected - 4 * 86400
            while self.recent_long and self.recent_long[0][0].detected < old:
                self.recent_long.popleft()

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

    def find_catalysts(
        self, symbols: list[str], now: float | None = None, overnight: bool = False
    ) -> list[dict[str, Any]]:
        """Recent headlines naming these symbols. ``overnight``: for the radar and the options
        tape, also news since the middle of the previous session (a deal announced after the
        close is the reason for the next morning's move)."""
        now = now or time.time()
        wanted = set(symbols)
        hits: list[tuple[float, NewsItem, Analysis]] = []
        window = self.cfg.market.correlate_minutes * 60
        cutoff = now - window
        pool: list[tuple[NewsItem, Analysis]] = list(self.recent)
        if overnight:
            window = max(window, now - (Calendar.last_close(now, self.calendar.holidays) - 6 * 3600))
            cutoff = now - window
            seen = {id(it) for it, _ in pool}
            pool += [(it, an) for it, an in self.recent_long if id(it) not in seen]
        for item, an in pool:
            if item.detected < cutoff or item.detected > now + 60 or an.score < CATALYST_MIN_SCORE:
                continue
            overlap = wanted & set(an.tickers)
            if not overlap:
                continue
            recency = 1.0 - (now - item.detected) / window
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
        if self.calendar.market_status(time.time())["phase"] == "open":
            # in the session SPY/QQQ/IWM/DIA say it first; futures are for the other 17 hours
            moves = [m for m in moves if m.symbol not in CASH_HOURS_ONLY_QUIET]
            if not moves:
                return
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
        proxies = list(dict.fromkeys(p for s in syms for p in PROXIES.get(s, [])))
        catalysts = self.find_catalysts(syms + proxies, mv.detected, overnight=bool(proxies))
        if mv.is_group:
            body = ", ".join(f"{s} {fmt_pct(c)}" for s, c in list(mv.members.items())[:10])
        elif mv.window_s >= 86400:
            body = f"{mv.price:.2f} vs. previous close {mv.ref_price:.2f}"
        else:
            body = f"{mv.price:.2f} (from {mv.ref_price:.2f} {window_label(mv.window_s)} ago)"
            if mv.relative_pct is not None:
                body += f" · vs {self.cfg.market.benchmark} {fmt_pct(mv.relative_pct)} relative"
        if proxies:
            body += f"\nWatch: {' '.join(proxies[:6])}"
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

    # ------------------------------------------------------------------ binary events, in advance

    def _capture_catalyst(self, item: NewsItem, analysis: Analysis) -> None:
        """ "…to Present Topline Results on September 28" / "PDUFA date of June 30": put it on the
        calendar so the Brief says "Tomorrow: KOD Phase 3 readout" the night before."""
        sc = analysis.smallcap or {}
        symbol = sc.get("symbol") or self._subject_symbol(item)
        if not symbol:
            return
        ev = extract_event(
            item.title, item.summary, symbol, now=min(item.published or item.detected, item.detected)
        )
        if ev is None:
            return
        li = self.universe.get(symbol)
        ev.update(
            {
                # any binary event or index entry is worth the morning Brief, whatever the size
                "impact": 2,
                "note": f"{li.name} · {li.cap_label} {li.band}" if li is not None and li.market_cap else "",
                "url": item.url,
                "source": item.source,
                # where the date came from and when: the release time and the moment we read it
                "published": item.published,
                "seen": item.detected,
            }
        )
        if self.storage.add_catalyst(ev):
            self.calendar.add(ev)
            log.info("binary event on the calendar: %s on %s", ev["title"], ev["date"])
            self._broadcast("calendar", ev)

    # index providers announce additions; they are never the subject of one
    _ANNOUNCERS = frozenset({"NDAQ", "SPGI", "ICE", "CBOE", "LNSTY", "MSCI"})

    def _subject_symbol(self, item: NewsItem) -> str:
        """The listed company a release is about, at any size: the ticker the wire attached,
        "(Nasdaq: MRNA)" in the text, the company the headline starts with or names."""
        text = f"{item.title} {item.summary[:700]}"
        cands = [*item.tickers, *self.scorer.entities.find_tickers(text, item.title)]
        if len(self.universe):
            lead = self.universe.find_issuer(item.title)
            cands += ([lead.symbol] if lead else []) + [
                li.symbol for li in self.universe.find_named(item.title)
            ]
        for c in self.scorer.entities.find_companies(item.title):
            if c.ticker:
                cands.append(c.ticker)
        for sym in dict.fromkeys(c.upper() for c in cands):
            if sym not in self._ANNOUNCERS:
                return sym
        return ""

    def _calendar_today(self, symbol: str, now: float) -> list[dict[str, Any]]:
        return [
            e
            for e in self.calendar.upcoming(days=1, now=now)
            if e.get("symbol") == symbol and e.get("in_days") == 0
        ]

    # ------------------------------------------------------------------ the radar

    async def on_radar(self, hits: list[RadarHit]) -> None:
        for hit in hits:
            self.radar_seen[hit.symbol] = (hit.detected, hit.quote.change_pct or 0.0)
            alert = self._radar_alert(hit)
            await self.publish(alert)
            self._maybe_hunt(alert, hit.quote.name or "")
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
        catalysts = self.find_catalysts([hit.symbol], hit.detected, overnight=True)
        today = self._calendar_today(hit.symbol, hit.detected)
        for e in today:  # e.g. "MRNA joins the Nasdaq-100 (effective at the open)"
            body += f"\nOn the calendar today: {e['title']}"
        if not catalysts and not today:
            body += (
                "\nNo headline yet — on the radar before the news. Watch halts, 8-Ks and the wires. "
                "Until one lands, be careful chasing it: moves this big with no news tended to give back "
                "(in the Sep-Oct 2026 study, 63% of 68 such alerts closed the day back against the move)."
            )
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
            "label": "Radar: moving on the news"
            if catalysts
            else "Radar: on the calendar today"
            if today
            else "Radar: moving before the news",
            "direction": hit.direction,
            "radar": True,
        }
        self._track_symbols([hit.symbol])
        return alert

    # ------------------------------------------------------------------ ignition (the live tape)

    async def on_ignition(self, hits: list[IgnitionHit]) -> None:
        """A stock starting to move. Confirmed (a headline, an insider buy, options on the same
        side): an alert, pushed up to ``ignition_daily_pushes`` a day. Unconfirmed: on the radar
        board, its option chain read at once (a flow hit there confirms it), and the strongest
        (3x volume, 8x burst) shown in the app."""
        sc = self.cfg.smallcap
        for hit in hits:
            self._board_ignition(hit)
            confirmed = self._ignition_confirmations(hit)
            day = datetime.fromtimestamp(hit.detected, ET).date().isoformat()
            if self.ignition_day["day"] != day:
                self.ignition_day = {"day": day, "pushed": 0, "shown": 0}
            if confirmed:
                await self._publish_ignition(hit, confirmed)
                continue
            self.ignition_pending[hit.symbol] = (hit, hit.detected)
            if self.options_feed is not None:
                self.options_feed.urgent([hit.symbol], f"igniting: {self._ignition_line(hit)}")
            if hit.strong and self.ignition_day["shown"] < sc.ignition_daily_shown:
                self.ignition_day["shown"] += 1
                alert = self._ignition_alert(hit, [], Severity.MEDIUM)
                await self.publish(alert)
                self._maybe_hunt(alert, hit.name)
        cut = (
            max(h.detected for h in hits) if hits else time.time()
        ) - 1800  # the tape's clock, not the wall's
        for sym, (_, ts) in list(self.ignition_pending.items()):
            if ts < cut:
                del self.ignition_pending[sym]

    async def _publish_ignition(self, hit: IgnitionHit, confirmed: list[str]) -> None:
        sc = self.cfg.smallcap
        self.ignition_pending.pop(hit.symbol, None)
        push = self.ignition_day["pushed"] < sc.ignition_daily_pushes
        if push:
            self.ignition_day["pushed"] += 1
        await self.publish(self._ignition_alert(hit, confirmed, Severity.HIGH if push else Severity.MEDIUM))

    def _ignition_confirmations(self, hit: IgnitionHit) -> list[str]:
        """What backs the move: primes (options bought, an insider buying, a headline that put it
        on watch), headlines since the last session, and options volume on its side today."""
        out = list(hit.primed)
        for c in self.find_catalysts([hit.symbol], hit.detected, overnight=True)[:2]:
            line = f"news: {c['title'][:110]} ({c['source']}, {c['age']} ago)"
            if not any(c["title"][:40] in x for x in out):
                out.append(line)
        seen = self.options_seen.get(hit.symbol)
        if seen and hit.detected - seen[0] < 3 * 3600 and seen[1] == hit.direction and seen[2] not in out:
            out.append(seen[2])
        return out

    @staticmethod
    def _ignition_line(hit: IgnitionHit) -> str:
        if hit.kind == "buildup":
            return f"{fmt_pct(hit.day_pct)} on the day, at a new session {'high' if hit.direction == 'up' else 'low'}"
        return f"{fmt_pct(hit.move_pct)} in {window_label(int(hit.window_s))}"

    def _ignition_alert(self, hit: IgnitionHit, confirmed: list[str], severity: Severity) -> Alert:
        d = hit.to_dict()
        arrow = "▲" if hit.direction == "up" else "▼"
        vol = ""
        if hit.burst is not None:
            vol = f" on {hit.burst:.0f}× normal volume"
        elif hit.rvol is not None:
            vol = f" on {hit.rvol:.1f}× the day's normal volume"
        what = "igniting" if hit.kind == "ignition" else "building"
        name = (hit.name or hit.symbol)[:40]
        title = f"⚡ {arrow} {hit.symbol} {what}: {self._ignition_line(hit)}{vol} · {name} · {d['cap']} {d['band']}"
        side = "high" if hit.direction == "up" else "low"
        parts = [
            f"${hit.price:,.2f} ({fmt_pct(hit.day_pct)} on the day)"
            + (
                f", from ${hit.launch:,.2f} {window_label(int(hit.window_s))} ago"
                if hit.kind == "ignition"
                else ""
            )
            + f": a new session {side}."
        ]
        if hit.rvol is not None:
            parts.append(f"Volume so far {hit.rvol:.1f}× normal for this time of day.")
        lines = [" ".join(parts)]
        if confirmed:
            lines.append("What backs it: " + " · ".join(confirmed[:3]))
        else:
            lines.append(
                "Nothing backs it yet (no headline, insider buy or options flow found): its option chain is "
                "being read now. On their own, moves like this were a coin flip by the close."
            )
        stop_side = "under" if hit.direction == "up" else "over"
        lines.append(
            f"If you trade it: the move has failed {stop_side} ${hit.stop:,.2f} (the launch or the VWAP), "
            f"{hit.risk_pct:.1f}% away. In the study behind this alert (106 stocks, Sep-Oct 2026) the follow-through "
            "came in the first 15-60 minutes (median +1.3% at best within the hour, -1.0% against); by the close "
            "half of it was given back. Confirmed moves are the ones that kept going (news drifts, no-news reverses)."
        )
        move = PriceMove(
            hit.symbol,
            round(hit.move_pct, 3),
            int(hit.window_s) if hit.kind == "ignition" else 86400,
            float(hit.price),
            float(hit.launch),
            detected=hit.detected,
        )
        catalysts = self.find_catalysts([hit.symbol], hit.detected, overnight=True)
        alert = Alert(
            kind="price",
            severity=severity,
            title=title,
            body="\n".join(lines),
            url=catalysts[0]["url"] if catalysts else f"https://finance.yahoo.com/quote/{hit.symbol}",
            tickers=[hit.symbol],
            move=move,
            related=catalysts,
        )
        alert.edge["ignition"] = {**d, "confirmed_by": list(confirmed)}
        alert.edge["smallcap"] = {
            "symbol": hit.symbol,
            "name": hit.name,
            "cap": d["cap"],
            "band": d["band"],
            "market_cap": hit.market_cap,
            "label": "Ignition: confirmed" if confirmed else "Ignition: nothing backs it yet",
            "direction": hit.direction,
            "radar": True,
        }
        self._track_symbols([hit.symbol])
        return alert

    def _board_ignition(self, hit: IgnitionHit) -> None:
        if self.radar is None:
            return
        row = self.radar.board.get(hit.symbol, {})
        self.radar.board[hit.symbol] = {
            "symbol": hit.symbol,
            "name": hit.name,
            "price": hit.price,
            "prev_close": hit.prev_close,
            "change_pct": hit.day_pct,
            "session": "regular",
            "kind": hit.kind,
            "level": 0,
            "market_cap": hit.market_cap,
            "cap": fmt_cap(hit.market_cap),
            "band": hit.band,
            "severity": "MEDIUM",
            "flags": [],
            "detected": hit.detected,
            "source": "tape",
            **row,
            "ignition": hit.to_dict(),
            "updated": hit.detected,
            "flagged": hit.detected,
        }

    def _prime(self, alert: Alert) -> None:
        """What points at a stock before it moves: its first push on volume is read with lower
        thresholds and the ignition alert says why."""
        if self.ignition is None or alert.severity < Severity.MEDIUM:
            return
        if alert.kind == "news" and alert.item is not None and alert.analysis is not None:
            ib = alert.item.extra.get("insider_buy")
            if ib and alert.tickers:
                why = f"insider: {ib.get('role', 'insider')} {ib.get('owner', '')} bought {fmt_cap(ib.get('value') or 0)} (Form 4)"
                self.ignition.prime(alert.tickers[0], "up", why, hold_s=3 * 86400.0)
                return
            direction = alert.analysis.direction if alert.analysis.direction in ("up", "down") else "either"
            for sym in alert.edge.get("play", {}).get("direct", [])[:3]:
                self.ignition.prime(sym, direction, f"news: {alert.title[:100]}")

    def _options_side(self, hit: OptionsHit) -> str:
        if hit.kind == "blowup":
            return "up" if hit.side == "calls" else "down"
        if hit.kind == "spike" and hit.spikes:
            return "up" if hit.spikes[0].contract.kind == "call" else "down"
        if hit.call_premium or hit.put_premium:
            return "up" if hit.call_premium >= hit.put_premium else "down"
        if hit.flow:
            calls = sum(r.contract.premium for r in hit.flow if r.contract.kind == "call")
            puts = sum(r.contract.premium for r in hit.flow if r.contract.kind == "put")
            return "up" if calls >= puts else "down"
        return ""

    def _options_prime(self, hit: OptionsHit, alert: Alert) -> IgnitionHit | None:
        """Options volume on one side: remember it (it confirms an ignition on that side for three
        hours) and, when the stock hasn't moved yet, put it on watch. Returns a waiting ignition it
        confirms."""
        if self.ignition is None or hit.kind not in ("spike", "flow", "blowup", "surge"):
            return None
        side = self._options_side(hit)
        if not side or alert.severity < Severity.MEDIUM:
            return None
        money = hit.call_premium if side == "up" else hit.put_premium
        what = f"options: {'calls' if side == 'up' else 'puts'} {hit.kind}" + (
            f", {fmt_cap(money)} premium" if money else ""
        )
        self.options_seen[hit.symbol] = (hit.detected, side, what)
        chg = hit.chain.change_pct
        line = self.ignition.day_threshold(hit.market_cap)
        if chg is not None and abs(chg) < line / 2:
            self.ignition.prime(hit.symbol, side, what)
            alert.body += (
                f"\nThe stock hasn't moved yet ({fmt_pct(chg)}): it is on watch, and its first push "
                f"{'up' if side == 'up' else 'down'} on volume alerts at once."
            )
        waiting = self.ignition_pending.get(hit.symbol)
        if waiting and waiting[0].direction == side and hit.detected - waiting[1] < 1800:
            return waiting[0]
        return None

    # ------------------------------------------------------------------ the options tape

    async def on_options(self, hits: list[OptionsHit]) -> None:
        for hit in hits:
            alert = self._options_alert(hit)
            ignited = self._options_prime(hit, alert)
            await self.publish(alert)
            if ignited is not None:  # the chain read at ignition found buying on the same side
                await self._publish_ignition(ignited, self._ignition_confirmations(ignited))
            self._maybe_hunt(alert, hit.to_dict().get("name") or "")
            if self.options_feed is not None and alert.severity >= Severity.MEDIUM:
                self.options_feed.track(hit)

    async def on_follow(self, events: list[FollowEvent]) -> None:
        """Follow-ups on alerted contracts: still running, fading, stopped."""
        for ev in events:
            await self.publish(self._follow_alert(ev))

    # ------------------------------------------------------------------ the trend lane

    async def _trend_check(self, tickers: list[str]) -> None:
        assert self.trends is not None
        signals = [s for s in [await self.trends.evaluate(t) for t in dict.fromkeys(tickers)] if s]
        if signals:
            await self.on_trends(signals)

    async def on_trends(self, signals: list[TrendSignal]) -> None:
        """MEDIUM and HIGH trend signals become alerts (HIGH is pushed by the usual channel rules).
        LOW ones (too big to matter, already priced in, mixed) stay on the trend board only."""
        for sig in signals:
            if sig.severity == "LOW":
                continue
            title, body = signal_alert_text(sig)
            urls = [e.get("url") for e in sig.evidence if e.get("url")]
            alert = Alert(
                kind="trend",
                severity=Severity.parse(sig.severity),
                title=title,
                body=body,
                url=urls[0] if urls else f"https://finance.yahoo.com/quote/{sig.ticker}",
                tickers=[sig.ticker],
                related=self.find_catalysts([sig.ticker], sig.created, overnight=True),
            )
            alert.edge["trend"] = sig.to_dict()
            await self.publish(alert)

    async def check_narratives(self, now: float | None = None) -> list[Alert]:
        """The attention trade: one alert when a narrative starts accelerating (MEDIUM, in the
        app), HIGH (pushed) when two or more fear narratives in one sector accelerate together."""
        if self.trends is None or self.trends.narratives is None:
            return []
        from .camillo import narrative_alert_text

        out = []
        for row in self.trends.narratives.check(self.storage, self.trends.chatter, now):
            title, body = narrative_alert_text(row)
            ex = row.get("examples") or []
            alert = Alert(
                kind="narrative",
                severity=Severity.parse(row["severity"]),
                title=title,
                body=body,
                url=ex[0]["url"] if ex and ex[0].get("url") else "",
                tickers=list(row.get("hits") or [])[:6],
            )
            alert.edge["narrative"] = {
                k: row[k]
                for k in ("id", "name", "side", "sector", "level", "news", "social", "sources_3d", "together")
            }
            await self.publish(alert)
            out.append(alert)
        if out:
            self.trends.save()
        return out

    async def _narrative_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await _sleep_or_stop(stop, 1800)
            if stop.is_set():
                break
            try:
                await self.check_narratives()
            except Exception as exc:  # noqa: BLE001 - the attention trade must never stop the engine
                log.warning("narratives: %s", exc)

    def camillo_page(self) -> dict[str, Any]:
        from .camillo import load_playbook, page

        if self.trends is None:
            return {
                "enabled": False,
                "playbook": load_playbook(),
                "ideas": [],
                "attention": [],
                "chatter": [],
                "narratives": [],
            }
        return {"enabled": True, **page(self.trends)}

    def trends_board(self, days: float = 30) -> dict[str, Any]:
        if self.trends is None:
            return {"enabled": False, "rows": [], "scorecard": None, "status": None}
        return {
            "enabled": True,
            "rows": self.trends.board(days),
            "scorecard": self.trends.scorecard(),
            "status": self.trends.status(),
        }

    # ------------------------------------------------------------------ why is it moving?

    def _maybe_hunt(self, alert: Alert, name: str = "") -> None:
        """A move with no known headline: look up the company's filings and news right away."""
        if self.why is None or len(alert.tickers) != 1 or alert.severity < Severity.MEDIUM:
            return
        if any(r.get("kind") == "news" for r in alert.related) or alert.edge.get("smallcap", {}).get(
            "label", ""
        ).endswith("on the calendar today"):
            return
        sym = alert.tickers[0]
        if not self.why.allow(sym):
            return
        li = self.universe.get(sym)
        self._spawn(self._hunt(alert, sym, (li.name if li is not None else "") or name))

    async def _hunt(self, alert: Alert, symbol: str, name: str) -> dict[str, Any] | None:
        assert self.why is not None
        since = Calendar.last_close(alert.created, self.calendar.holidays) - 6 * 3600
        started = time.time()
        found = await self.why.hunt(symbol, name, since)
        best: tuple[float, NewsItem, Analysis] | None = None
        for item in found:
            an = await self.on_item(item)  # scored, stored and alerted like any other item
            if an is None:  # already seen through a regular source
                an = self.scorer.score(item)
            if best is None or an.score > best[0]:
                best = (an.score, item, an)
        took = round(time.time() - started, 1)
        if best is None:
            why = {"found": False, "searched": ["EDGAR filings", "news search"], "took_s": took}
        else:
            _, item, an = best
            why = {
                "found": True,
                "title": item.title,
                "source": item.source,
                "url": item.url,
                "score": an.score,
                "severity": an.severity.name,
                "published": item.published,
                "took_s": took,
                "others": len(found) - 1,
            }
        if self.storage.update_alert_edge(alert.id, {"why": why}) is not None:
            self._broadcast("edge", {"id": alert.id, "why": why})
        return why

    async def on_options_problem(self, note: str) -> None:
        await self.publish(
            Alert(kind="system", severity=Severity.HIGH, title="Options tape: feed problem", body=note)
        )

    @staticmethod
    def _option_ages(c: Any, now: float) -> str:
        """How old the contract's quote and last trade were when the alert went out."""

        def age(t: Any) -> str:
            s = max(0.0, now - t.timestamp())
            return f"{s:.0f}s" if s < 90 else f"{s / 60:.0f} min"

        bits = []
        if getattr(c, "quote_time", None) is not None:
            bits.append(f"quote {age(c.quote_time)} old")
        if getattr(c, "last_trade", None) is not None:
            bits.append(f"last trade {age(c.last_trade)} old")
        return "Data: " + ", ".join(bits) if bits else ""

    def _blowup_lines(self, hit: OptionsHit, who: str) -> tuple[str, list[str]]:
        """Title and lines for many contracts of one company up 4x+ at once."""
        ch = hit.chain
        lo = min(r.multiple for r in hit.blowups)
        hi = max(r.multiple for r in hit.blowups)
        span = f"{lo:,.0f}x" if round(lo) == round(hi) else f"{lo:,.0f}x–{hi:,.0f}x"
        jump = bool(
            hit.iv_prev and hit.iv_now and hit.iv_now >= self.cfg.options.blowup_iv_jump * hit.iv_prev
        )
        flat = ch.change_pct is not None and abs(ch.change_pct) < 3.0
        move = f" with the stock {fmt_pct(ch.change_pct)}" if ch.change_pct is not None else ""
        if jump and flat:
            title = (
                f"🧨 {hit.symbol} {hit.side} repriced: {hit.count} contracts up {span}{move} · implied vol "
                f"{(hit.iv_prev or 0) * 100:.0f}% → {(hit.iv_now or 0) * 100:.0f}% · {who}"
            )
        else:
            title = (
                f"🧨 {hit.symbol} {hit.side} exploding: {hit.count} contracts up {span} today{move} · {who}"
            )
        lines: list[str] = []
        today = datetime.fromtimestamp(hit.detected, ET).date()
        for r in hit.blowups:
            c = r.contract
            rep = ""
            if c.prev_close and abs(c.prev_close - r.base) > 0.005:
                rep = f" (reported close ${c.prev_close:.2f}: {(r.price / c.prev_close - 1) * 100:+,.0f}% on a broker)"
            lines.append(
                f"• {c.label()}: ${r.base:.2f} → ${r.price:.2f} {r.how}, {r.multiple:,.1f}x{rep} · "
                f"{c.dte(today)}d · {c.volume:,} traded"
            )
        if hit.count > len(hit.blowups):
            lines.append(
                f"…and {hit.count - len(hit.blowups)} more {hit.side} up {self.cfg.options.blowup_multiple:g}x+."
            )
        if hit.iv_prev and hit.iv_now:
            lines.append(
                f"Implied vol: {hit.iv_prev * 100:.0f}% at yesterday's close → {hit.iv_now * 100:.0f}% now."
            )
        if jump and flat:
            lines.append(
                "The options were repriced while the stock barely moved: the market is pricing an event "
                "(a deal, a ruling, results) that hasn't reached the stock yet."
            )
        lines.append(
            "Bases are yesterday's real closes; a stale $0.01 close or one from another day is replaced by the contract's value at yesterday's close."
        )
        return title, lines

    def _misprint_lines(self, hit: OptionsHit, who: str) -> tuple[str, list[str]]:
        """Title and lines for a misprint: what to do, at what price, and why it's mispriced."""

        def size(n: Any) -> str:
            return f" ({n:,} offered)" if n else ""

        lines: list[str] = []
        top = hit.misprints[0]
        c = top.contract
        if top.kind in ("vertical", "calendar") and top.other is not None:
            o = top.other
            title = (
                f"🎯 Arbitrage: buy {c.label()} at ${top.price:.2f}, sell {o.label()} at "
                f"${(top.other_price or 0):.2f} (+${top.edge:.2f} a share) · {who}"
            )
        elif top.kind == "intrinsic":
            title = (
                f"🎯 Below intrinsic: {c.label()} offered at ${top.price:.2f}, worth ${(top.fair or 0):.2f} "
                f"exercised · {who}"
            )
        elif top.kind == "cheap_ask":
            title = (
                f"🎯 Misprint: {c.label()} offered at ${top.price:.2f}, worth ~${(top.fair or 0):.2f} · {who}"
            )
        elif top.kind == "rich_bid":
            title = f"🎯 Misprint: {c.label()} bid ${top.price:.2f}, worth ~${(top.fair or 0):.2f} · {who}"
        else:
            title = f"🎯 Misprinted trade: {c.label()} traded ${top.price:.2f}, worth ~${(top.fair or 0):.2f} · {who}"
        for r in hit.misprints:
            rc = r.contract
            quote = f"bid ${rc.bid:.2f} / ask ${rc.ask:.2f}" if rc.bid is not None and rc.ask else "no quote"
            if r.kind in ("vertical", "calendar") and r.other is not None:
                oq = r.other
                lines.append(
                    f"• {MISPRINT_LABEL[r.kind]}: buy {rc.label()} at ${r.price:.2f}{size(rc.ask_size)}, sell "
                    f"{oq.label()} at ${(r.other_price or 0):.2f}: +${r.edge:.2f} a share (${r.edge * 100:,.0f} a "
                    f"contract) locked in now. Why: {r.ref}."
                )
            elif r.kind == "print":
                when = f" at {rc.last_trade:%H:%M} ET" if rc.last_trade else ""
                lines.append(
                    f"• {MISPRINT_LABEL[r.kind]}: {rc.label()} printed ${r.price:.2f}{when}, worth ~${(r.fair or 0):.2f} "
                    f"({quote}) by {r.ref}. A thin market: resting limit orders can fill this far from value."
                )
            else:
                verb = "buy" if r.kind in ("cheap_ask", "intrinsic") else "sell"
                lines.append(
                    f"• {MISPRINT_LABEL[r.kind]}: {verb} {rc.label()} at ${r.price:.2f}"
                    f"{size(rc.ask_size if verb == 'buy' else rc.bid_size)} ({quote}), worth ~${(r.fair or 0):.2f} "
                    f"by {r.ref}: ${r.edge:.2f} a share in your favor."
                )
            ages = self._option_ages(rc, hit.detected)
            if ages:
                lines.append(f"  {ages}")
        lines.append("Misprints vanish in seconds: check the live quote before you act.")
        return title, lines

    def _play_lines(self, play: Play, hit: OptionsHit) -> list[str]:
        """'If you buy now': where in the move the ask is, what the contract does as the stock
        moves, the breakeven, and the exits the follow-ups will watch."""
        o = self.cfg.options
        sym = hit.symbol
        mult = f" ({play.entry / play.base:,.1f}x yesterday's ${play.base:.2f})" if play.base > 0 else ""
        lines = [f"▶ If you buy now at the ${play.entry:.2f} {play.entry_how}{mult}:"]
        p = play.path
        if p is not None:
            began = (
                f"began running {datetime.fromtimestamp(p.began_ts, ET):%H:%M} ET · " if p.began_ts else ""
            )
            lines.append(
                f"• Its day: {began}high ${p.high:.2f} at {datetime.fromtimestamp(p.high_ts, ET):%H:%M} ET · "
                f"low ${p.low:.2f} · {p.volume:,} traded (trades through {datetime.fromtimestamp(p.to_ts, ET):%H:%M} ET)"
            )
            if play.verdict:
                lines.append(f"• Where you are: {play.verdict}.")
        if play.per_1pct is not None and play.delta is not None:
            iv = f" · implied vol {play.iv * 100:.0f}%" if play.iv else ""
            lines.append(
                f"• Each 1% {sym} moves your way: about ${abs(play.per_1pct):.2f} "
                f"({abs(play.per_1pct) / play.entry * 100:.0f}% of the entry) · delta {abs(play.delta):.2f}{iv}"
            )
        be = f" ({play.breakeven_pct:+.1f}% from here)" if play.breakeven_pct is not None else ""
        lines.append(f"• Breakeven at expiry: {sym} ${play.breakeven:,.2f}{be}")
        if play.scenarios:
            lines.append(
                "• If " + " · ".join(f"{what}: ${v:.2f} ({pct:+.0f}%)" for what, v, pct in play.scenarios)
            )
        stock = ""
        if play.stock_fade is not None and play.stock_stop is not None:
            stock = (
                f" · on the stock: half its move gone at ${play.stock_fade:,.2f}, all of it at "
                f"${play.stock_stop:,.2f}"
            )
        lines.append(
            f"• Exits: fading under a ${play.fade:.2f} bid, stop under ${play.stop:.2f} "
            f"({o.follow_fade * 100:.0f}% / {o.follow_stop * 100:.0f}% under the mid; both trail the best bid){stock}"
        )
        if o.follow:
            lines.append(
                f"• Followed until the close: a follow-up when it runs (+{o.follow_run_step * 100:.0f}% steps), "
                "fades or hits the stop."
            )
        return lines

    def _daymove_lines(self, hit: OptionsHit, who: str) -> tuple[str, list[str]]:
        """A big stock's directional day: the call, its pullback zone, or its fade; with the
        contracts to buy, at what price, and the stock level that ends it."""
        c = hit.daycall
        o = self.cfg.options
        sym = hit.symbol
        px = c.last or c.price
        at = datetime.fromtimestamp(c.ts, ET)
        fade = c.line(o.daymove_fade)
        verb = "up" if c.cp == "C" else "down"
        if hit.stage == "faded":
            title = (
                f"⛔ {sym} day move faded: {c.move_pct(px):+.1f}%, under 70% of its move for "
                f"{o.daymove_fade_s / 60:.0f} min · {who}"
            )
            lines = [
                f"{sym} ${px:,.2f}: best ${c.best:,.2f} today, yesterday ${c.prev:,.2f}; the 70% line was ${fade:,.2f}.",
                f"The {c.side} call from {at:%H:%M} ET is off: no new entries. If you hold, the follow-ups (fading / stop) still apply.",
            ]
            return title, lines
        if hit.stage == "zone":
            title = (
                f"🔁 {sym} pullback zone: {c.move_pct(px):+.1f}%, {c.kept(px) * 100:.0f}% of its move held · "
                f"the {c.side}' buy zone · {who}"
            )
        else:
            title = f"📊 {sym} day move: {c.side} · {sym} {verb} {abs(c.move_pct(px)):.1f}% and holding ({c.how}) · {who}"
        lines = [
            f"{sym} ${px:,.2f} ({c.move_pct(px):+.2f}%): best ${c.best:,.2f} today, yesterday ${c.prev:,.2f}. "
            f"Called at {at:%H:%M} ET ({c.how})."
        ]
        names = {"day": "Day trade", "swing": "Trend (multi-day)", "lotto": "Lotto (expires today/tomorrow)"}
        for p in c.picks:
            when = date.fromisoformat(p.expiry)
            vol = self.options_feed.daymove.volume_state(p.occ) if self.options_feed is not None else ""
            lines.append(
                f"• {names.get(p.tier, p.tier)}: {p.label} ({p.dte}d): ask ${p.ask:.2f} / bid ${p.bid:.2f} · "
                f"delta {p.delta:.2f} · +${p.per_1pct:.2f} per 1% {sym} moves ({p.leverage:.0f}%) · "
                f"breakeven at expiry ${p.breakeven:,.2f}"
            )
            lines.append(
                f"  Pullback buy zone ${p.zone_hi:.2f}–${p.zone_lo:.2f} (with {sym} ${c.line(0.85):,.2f}–${fade:,.2f}) · "
                f"implied vol {p.iv * 100:.0f}% · {p.volume:,} traded"
                + (f" · volume {vol}" if hit.stage == "zone" and vol else "")
                + (f" · expires {when:%b} {when.day}" if p.tier == "lotto" else "")
            )
        lines.append(
            f"How to use it: buy at the ask now, or rest a limit inside the pullback zone. The call is off if {sym} "
            f"stays {'under' if c.cp == 'C' else 'over'} ${fade:,.2f} (70% of its move) for {o.daymove_fade_s / 60:.0f} "
            "minutes. After you buy, the follow-ups say when it runs, fades or hits its stop."
        )
        lines.append(
            "Study (real OPRA tape, Sep 21–Oct 12 2026): options bought at this call made a median +14% by the close "
            "(7 of 11 up); at the pullback zone, +14% (8 of 11). On META's +4% day, 10%-out calls bought at 9:45 made "
            "+54% (2 days out), +29% (4), +10% (11), +1% (18), −1% (39): the month-out contracts had repriced in the "
            "first 15 minutes. The system never trades."
        )
        return title, lines

    def _follow_alert(self, ev: FollowEvent) -> Alert:
        f = ev.f
        o = self.cfg.options
        bid = f.bid or 0.0
        since = (bid / f.entry - 1.0) * 100.0 if f.entry else 0.0
        at = datetime.fromtimestamp(f.alert_ts, ET)
        stock = ""
        if f.stock is not None:
            stock = f" · {f.underlying} ${f.stock:,.2f}"
        if ev.kind == "running":
            title = f"📈 {f.label} still running: bid ${bid:.2f}, {since:+.0f}% since the {at:%H:%M} ET alert{stock}"
            sev = Severity.MEDIUM
        elif ev.kind == "fading":
            title = f"⚠️ {f.label} fading: bid ${bid:.2f}, {since:+.0f}% since the {at:%H:%M} ET alert{stock}"
            sev = Severity.HIGH
        elif ev.kind == "rerun":
            title = f"🔁 {f.label} running again after its stop: bid ${bid:.2f}, a new high{stock}"
            sev = Severity.MEDIUM
        else:
            title = (
                f"🛑 {f.label} stop hit: bid ${bid:.2f}, {since:+.0f}% since the {at:%H:%M} ET alert{stock}"
            )
            sev = Severity.HIGH
        lines = [f"Why: {ev.why}."]
        ask = f" / ask ${f.ask:.2f}" if f.ask else ""
        lines.append(
            f"Alert {at:%H:%M} ET: ask ${f.entry:.2f} (mid ${f.mark:.2f}) · best bid since ${f.high:.2f} at "
            f"{datetime.fromtimestamp(f.high_ts, ET):%H:%M} ET · now bid ${bid:.2f}{ask} "
            f"(quote {datetime.fromtimestamp(f.ts, ET):%H:%M:%S} ET)"
        )
        if f.stock is not None and f.stock_prev and f.stock_peak is not None:
            move = f.stock_peak - f.stock_prev
            kept = (f.stock - f.stock_prev) / move if move else 0.0
            if kept >= 0.75:
                read = "holding near its best level today"
                if ev.kind != "running":
                    read += (
                        ": the option is losing value faster than the stock (implied vol coming out, or time)"
                    )
            elif kept > 0:
                read = f"has given back {(1 - kept) * 100:.0f}% of its move today"
            else:
                read = "has given back its whole move today"
            lines.append(
                f"{f.underlying} ${f.stock:,.2f}: {read} (yesterday ${f.stock_prev:,.2f}, best ${f.stock_peak:,.2f}"
                + (f", at the alert ${f.stock_alert:,.2f})" if f.stock_alert else ")")
            )
        if f.day_high:
            when = f" at {datetime.fromtimestamp(f.day_high_ts, ET):%H:%M} ET" if f.day_high_ts else ""
            lines.append(f"Its high of the day before the alert: ${f.day_high:.2f}{when}.")
        if ev.kind == "stopped":
            lines.append(
                f"The stop rule ({o.follow_stop * 100:.0f}% under the best bid, or the stock's move erased) says out. "
                f"It stays watched until the close: a bid back above ${f.high:.2f} sends 'running again'."
            )
        elif ev.kind == "rerun":
            lines.append(
                f"New lines from here: ask ${f.entry:.2f} · fading under ${f.fade:.2f} · stop under "
                f"${f.stop:.2f} · next 'still running' at ${f.next_run:.2f}."
            )
        else:
            lines.append(
                f"Lines now: fading under ${f.fade:.2f} · stop under ${f.stop:.2f} · next 'still running' at "
                f"${f.next_run:.2f}."
            )
        lines.append("The system never trades: these are the rules' levels, the same every time.")
        alert = Alert(
            kind="options",
            severity=sev,
            title=title,
            body="\n".join(lines),
            url=f"https://www.cboe.com/delayed_quotes/{f.underlying.lower()}/quote_table",
            tickers=[f.underlying],
        )
        alert.edge["follow"] = {**f.to_dict(), "event": ev.kind, "why": ev.why}
        return alert

    def _options_alert(self, hit: OptionsHit) -> Alert:
        d = hit.to_dict()
        ch = hit.chain
        name = (d["name"] or hit.symbol)[:40]
        who = f"{name} · {d['cap']} {d['band']}"
        stock = ""
        if ch.price:
            stock = f"{hit.symbol} ${ch.price:,.2f}" + (
                f" ({fmt_pct(ch.change_pct)})" if ch.change_pct is not None else ""
            )
        when = ""
        if ch.quote_time is not None:
            when = f" · quotes as of {ch.quote_time:%H:%M:%S} ET"
        if ch.stock_source and ch.stock_source != "universe":
            stamp = f" {ch.stock_time:%H:%M:%S} ET" if ch.stock_time else ""
            when += f" · stock price: {ch.stock_source}{stamp}"
        footer = f"{ch.source}{when}. Option prices move fast; check the live bid/ask."
        lines: list[str] = []
        if hit.kind == "spike":
            top = hit.spikes[0]
            c = top.contract
            pct = top.honest_move_pct if top.stale_base else top.move_pct
            title = (
                f"🔥 {c.label()} {pct:+,.0f}% today" + ("" if top.real else " (unconfirmed)") + f" · {who}"
            )
            quote = f"bid ${c.bid:.2f} / ask ${c.ask:.2f}" if c.bid is not None and c.ask else "no live quote"
            ages = self._option_ages(c, hit.detected)
            if top.quote_led:
                first = (
                    f"${(c.prev_close or 0):.2f} → bid ${(c.bid or 0):.2f} live ({quote}) · last print "
                    f"${(c.last or 0):.2f}, {c.volume:,} contracts so far"
                )
            else:
                first = (
                    f"${(c.prev_close or 0):.2f} → ${(c.last or 0):.2f} ({quote}) · {c.volume:,} contracts "
                    f"({fmt_cap(c.premium)})"
                )
            lines.append(first + (f" · {stock}" if stock else ""))
            if ages:
                lines.append(ages)
            if top.quote_led:
                lines.append(
                    "Caught on the live quote: this feed's trades print about 15 minutes late, the bid does not."
                )
            if ch.price and c.strike:
                otm = (c.strike / ch.price - 1) * 100 if c.cp == "C" else (1 - c.strike / ch.price) * 100
                where = f"{abs(otm):.1f}% {'out of' if otm > 0 else 'in'} the money"
                lines.append(
                    f"{c.dte(datetime.fromtimestamp(hit.detected, ET).date())} days to expiry · strike {where}"
                )
            for r in hit.spikes[1:3]:
                rc = r.contract
                lines.append(
                    f"Also: {rc.label()} {r.move_pct:+,.0f}% (${(rc.prev_close or 0):.2f} → ${(rc.last or 0):.2f}, "
                    f"{rc.volume:,} contracts)"
                )
            if top.no_prior:
                lines.append(
                    f"No trade before today: the % is measured from its model value at yesterday's close "
                    f"(${(c.prev_close or 0):.2f})."
                )
            elif top.stale_base and top.real and top.fair_prev is not None:
                today = datetime.fromtimestamp(hit.detected, ET).date()
                if c.prev_close_date is not None and c.prev_close_date < prev_session(today):
                    lines.append(
                        f"No trade yesterday: the last close (${(c.prev_close or 0):.2f}) is from "
                        f"{c.prev_close_date:%b} {c.prev_close_date.day}; the % is measured from its value at "
                        f"yesterday's close (${top.fair_prev:.2f})."
                    )
                else:
                    lines.append(
                        f"The ${(c.prev_close or 0):.2f} previous close looks stale (model value that day "
                        f"${top.fair_prev:.2f}); the % is measured from the model value."
                    )
            for reason in hit.reasons:
                lines.append(f"⚠ {reason}")
        elif hit.kind == "surge":
            mult = hit.volume_today / max(hit.volume_prev, self.cfg.options.surge_floor_volume)
            share = hit.call_premium / hit.flow_premium * 100 if hit.flow_premium else 0
            title = (
                f"🌊 Options volume {mult:,.0f}× yesterday: {hit.symbol} {hit.volume_today:,} contracts "
                f"vs {hit.volume_prev:,} · {fmt_cap(hit.flow_premium)} traded · {who}"
            )
            lines.append(
                f"{fmt_cap(hit.flow_premium)} across the company's options ({share:.0f}% in calls)"
                + (f" · {stock}" if stock else "")
            )
            for r in hit.flow[:3]:
                rc = r.contract
                oi = f" vs {rc.open_interest:,} open" if rc.open_interest is not None else ""
                side = f", {rc.side()}" if rc.side() else ""
                move = f" ({rc.move_pct:+,.0f}%)" if rc.move_pct is not None else ""
                lines.append(
                    f"{rc.label()}: {rc.volume:,} contracts{oi} · {fmt_cap(rc.premium)} at ${rc.price:.2f}{move}{side}"
                )
            for reason in hit.reasons:
                lines.append(f"ⓘ {reason}")
        elif hit.kind == "blowup":
            title, more = self._blowup_lines(hit, who)
            lines.extend(more)
            if stock:
                lines.append(stock)
            for reason in hit.reasons:
                lines.append(f"ⓘ {reason}")
        elif hit.kind == "daymove":
            title, more = self._daymove_lines(hit, who)
            lines.extend(more)
            for reason in hit.reasons:
                if hit.stage != "faded":
                    lines.append(f"ⓘ {reason}")
        elif hit.kind == "misprint":
            title, more = self._misprint_lines(hit, who)
            lines.extend(more)
            if stock:
                lines.append(stock)
            for reason in hit.reasons:
                lines.append(f"ⓘ {reason}")
        else:
            top = hit.flow[0]
            c = top.contract
            ratio = (
                f"{top.vol_oi:,.0f}× open interest"
                if top.vol_oi is not None
                else "no open interest before today"
            )
            title = (
                f"🐋 Unusual options: {hit.symbol} {fmt_cap(hit.flow_premium)} · {c.label()} "
                f"{c.volume:,} contracts, {ratio} · {who}"
            )
            mix = f"calls {fmt_cap(hit.call_premium)} / puts {fmt_cap(hit.put_premium)}"
            lines.append(
                f"{fmt_cap(hit.flow_premium)} in unusual contracts ({mix})" + (f" · {stock}" if stock else "")
            )
            for r in hit.flow[:3]:
                rc = r.contract
                side = f", {rc.side()}" if rc.side() else ""
                lines.append(
                    f"{rc.label()}: {rc.volume:,} contracts vs {rc.open_interest:,} open · "
                    f"{fmt_cap(rc.premium)} at ${rc.price:.2f}{side}"
                )
            lines.append("Side is estimated from where the last print sat in the bid/ask spread.")
        if hit.play is not None and hit.kind in ("spike", "blowup"):
            lines.extend(self._play_lines(hit.play, hit))
        news = self.find_catalysts([hit.symbol], hit.detected, overnight=True)
        if not news:
            lines.append("No headline yet.")
        lines.append(footer)
        alert = Alert(
            kind="options",
            severity=Severity.parse(hit.severity),
            title=title,
            body="\n".join(lines),
            url=f"https://www.cboe.com/delayed_quotes/{hit.symbol.lower()}/quote_table",
            tickers=[hit.symbol],
            related=news,
        )
        alert.edge["options"] = d
        from .flowrec import flow_base, stamp

        stamp(alert)  # its direction, shape and reference price: the options lane gets graded
        fb = flow_base(self.flow_rates, (alert.edge.get("flow") or {}) or None)
        if fb:
            alert.edge["flow_base"] = fb
        self._track_symbols([hit.symbol])
        return alert

    def follow_for(self, alert: dict[str, Any]) -> dict[str, Any] | None:
        """The live follow-through of an options alert's contract (its state now), for the app."""
        if self.options_feed is None:
            return None
        e = alert.get("edge") or {}
        occ = (e.get("follow") or {}).get("occ") or ((e.get("options") or {}).get("play") or {}).get(
            "contract"
        )
        if not occ:
            o = e.get("options") or {}
            rows = o.get("spikes") or o.get("blowups") or []
            occ = rows[0].get("contract") if rows else None
        f = self.options_feed.follow.items.get(occ) if occ else None
        return f.to_dict() if f is not None else None

    def options_board(self, limit: int = 40) -> dict[str, Any]:
        return {
            "enabled": self.options_feed is not None,
            "market": self.calendar.market_status(time.time()),
            "rows": self.options_radar.snapshot(limit) if self.options_radar is not None else [],
            "status": self.options_feed.status() if self.options_feed is not None else None,
        }

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
            r["news"] = self.find_catalysts([r["symbol"]], now, overnight=True)[:1]
        return {
            "enabled": self.radar is not None,
            "market": self.calendar.market_status(now),
            "rows": rows,
            "status": self.smallcap_feed.status() if self.smallcap_feed is not None else None,
        }

    # ------------------------------------------------------------------ output

    async def publish(self, alert: Alert, push: bool = True) -> None:
        self.stats["alerts"] += 1
        self._prime(alert)
        if self.options_feed is not None and alert.kind in ("news", "price"):
            # read their option chains next; news that lands overnight stays hot until two hours
            # into the next session (a 2-hour hold from 18:00 would expire before the open)
            mk = self.calendar.market_status(time.time())
            until_open = 0.0 if mk.get("open") else float(mk.get("until_s") or 0.0)
            self.options_feed.prioritize(alert.tickers[:6], hold_s=2 * 3600.0 + until_open)
            if mk.get("open"):  # in session: read them now, not at their turn in the rotation
                why = "on the radar" if alert.kind == "price" else "in the news"
                self.options_feed.urgent(alert.tickers[:6], f"{why}: {alert.title[:90]}")
        if alert.kind != "system" and alert.tickers and "pre" not in alert.edge:
            pre = self.pre_move(alert.tickers[:3], alert.created)
            if pre:
                alert.edge["pre"] = pre  # how far it had moved in the 15 minutes before: priced in?
        if alert.kind != "system" and "mkt_ref" not in alert.edge:
            spy = self.price_of(MARKET, 900)
            if spy and spy[1]:
                alert.edge["mkt_ref"] = spy[1]  # the market then: each grade is also read against it
        if alert.kind != "system" and alert.tickers and "sector_ref" not in alert.edge:
            from .misses import sector_ref

            sr = sector_ref(self, alert.tickers[0])
            if sr:
                alert.edge["sector_ref"] = sr  # its sector then: a miss can be told from a swamped call
        if alert.kind != "system" and "base" not in alert.edge:
            from .dossier import base_rate, record_key

            alert.edge.setdefault("rk", record_key(alert))  # its kind, for each phone's taste

            base = base_rate(self.base_rates, alert)
            if base:
                alert.edge["base"] = base  # how alerts like this did: on the card, the sheet, the push
            from .catalysts import catalyst_base, classify

            cat = classify(alert.to_dict(), self.universe)
            if cat:
                alert.edge["catalyst"] = cat  # what kind of news, at what size: graded per catalyst
                cb = catalyst_base(self.catalyst_rates, cat)
                if cb:
                    alert.edge["cat_base"] = cb
                from .catalysts import regime_base

                rb = regime_base(self.regime_rates, cat["key"], self.regime_label())
                if rb:
                    alert.edge["regime_base"] = rb  # the same kind of news, on days like today
            from .firms import firm_base, read_call

            call = read_call(alert.to_dict())
            if call:
                alert.edge["firm"] = call  # an analyst's call by a named firm: graded per firm
                fb = firm_base(self.firm_rates, call)
                if fb:
                    alert.edge["firm_base"] = fb
            if alert.tickers:
                from .persona import stamp as persona_stamp

                ps = persona_stamp(self.persona_rates, alert.tickers[0])
                if ps:
                    alert.edge["persona"] = ps  # how this stock has taken its own news here
            from .newsclock import clock_for
            from .newsclock import stamp as clock_stamp

            ck = clock_stamp(clock_for(self.clock_rates, alert.to_dict(), self.universe))
            if ck:
                alert.edge["clock"] = ck  # how fast its kind of news plays out: chase or no rush
            from .insiders import insider_base, read_buy

            buy = read_buy(alert.to_dict())
            if buy:
                alert.edge["insider"] = buy  # who bought, how much, how unusual: graded per shape
                ib = insider_base(self.insider_rates, buy)
                if ib:
                    alert.edge["insider_base"] = ib
            from .drift import drift_for

            dr = drift_for(self.drift_rates, alert.to_dict(), self.universe)
            if dr:  # its kind of news a month on: kept drifting, gave it back, or held
                alert.edge["drift"] = {"key": dr["key"], "label": dr["label"], "verdict": dr["verdict"],
                                       "d5": dr["d5"], "d20": dr["d20"]}  # fmt: skip
            from .opening import outside

            if outside(alert.created, self.calendar.holidays):
                ck_key = (alert.edge.get("catalyst") or {}).get("key")
                ob = self.open_rates.get(f"{ck_key}|*") or self.open_rates.get("*|*")
                if ob:
                    alert.edge["open_base"] = ob  # news from outside the session: what it did after the bell
            if alert.item is not None:
                from .coverage import late_bin
                from .coverage import read as read_coverage

                cov = read_coverage(self, alert.to_dict())
                if cov and cov["late"] >= 2:  # the story was already out: how late, and how late alerts did
                    alert.edge["late"] = {"min": cov["late"], "source": cov["first_source"],
                                          "rec": self.coverage_rates.get(f"late:{late_bin(cov['late'])}")}  # fmt: skip
            from .climate import climate_for

            cl = climate_for(self.climate_rates, (alert.edge.get("catalyst") or {}).get("key"))
            if cl:  # its kind is running hot or cold lately, beyond chance
                alert.edge["climate"] = cl
            cbv = alert.edge.get("cat_base") or {}
            if alert.tickers and cbv.get("typical"):
                from .personal import IMPLIED_TTL_S
                from .volnews import read as vol_read
                from .volnews import stamp_later as vol_later

                hit = self.implied_cache.get(alert.tickers[0])
                if hit and time.time() - hit[0] < IMPLIED_TTL_S:
                    v = vol_read(hit[1], cbv)
                    if v:  # what options price against what this news usually moves
                        alert.edge["vol"] = v
                        if (self.vol_rates.get(v["verdict"]) or {}).get("n", 0) >= 8:
                            alert.edge["vol_rec"] = self.vol_rates[v["verdict"]]
                elif self.options_feed is not None and alert.severity >= Severity.HIGH:
                    self._spawn(vol_later(self, alert.id, alert.tickers[0], cbv))  # the sheet gets it
            from .peerlearn import stamp as peer_stamp

            # its peers' prices now: who follows this stock's news is learned from them (peerlearn.py)
            peer_refs = peer_stamp(self, alert)
            if peer_refs:
                alert.edge["peer_refs"] = peer_refs
            from .macrotape import movers, theme_of
            from .macrotape import stamp as etf_stamp

            if alert.tickers and alert.kind != "system":
                from .volconfirm import read as rvol_read
                from .volconfirm import rvol_for

                r0 = rvol_read(self, alert.tickers[0])
                if r0 is not None:  # the market's vote on the news: its volume for the time of day
                    alert.edge["rvol"] = {"r0": r0}
                    rb = rvol_for(self.rvol_rates, (alert.edge.get("catalyst") or {}).get("key"), r0, "r0")
                    if rb:
                        alert.edge["rvol_base"] = rb
            etf_refs = etf_stamp(self, alert)
            if etf_refs:  # a market-wide headline: the ETF basket now, graded after (macrotape.py)
                alert.edge["etf_refs"] = etf_refs
            th = theme_of(alert)
            if th and th in self.theme_rates:
                tr = self.theme_rates[th]
                alert.edge["theme_rec"] = {
                    "theme": th,
                    "label": tr["label"],
                    "n": tr["n"],
                    "movers": movers(tr),
                }
            if alert.tickers and alert.kind == "news":
                from .floatshort import cached as float_cached
                from .floatshort import float_for, stamp_later, stamp_of

                kind_ = (alert.edge.get("catalyst") or {}).get("key")
                fl = stamp_of(float_cached(self, alert.tickers[0]))
                if fl:  # its float and short interest, and how news on stocks like it did (floatshort.py)
                    alert.edge["float"] = fl
                    fb = float_for(self.float_rates, kind_, fl)
                    if fb:
                        alert.edge["float_base"] = fb
                elif self.smallcap_feed is not None:
                    self._spawn(stamp_later(self, alert.id, alert.tickers[0], kind_))
                from .dilution import cached as dil_cached
                from .dilution import dilution_for
                from .dilution import stamp_later as dil_later
                from .dilution import stamp_of as dil_stamp_of

                dl = dil_stamp_of(dil_cached(self, alert.tickers[0]))
                if dl:  # selling shares now, a shelf on file, or neither, from its filings (dilution.py)
                    alert.edge["dilution"] = dl
                    db = dilution_for(self.dilution_rates, kind_, dl)
                    if db:
                        alert.edge["dilution_base"] = db
                elif any(getattr(s, "type_name", "") == "sec_edgar" and s.enabled for s in self.sources):
                    self._spawn(dil_later(self, alert.id, alert.tickers[0], kind_))
            from .macrodays import macro_for
            from .macrodays import today as macro_today

            mc = macro_for(
                self.macro_rates,
                (alert.edge.get("catalyst") or {}).get("key"),
                macro_today(self, alert.created),
            )
            if mc:  # a Fed or jobs-report day: how its kind did on days like it
                alert.edge["macro"] = mc
            # the odds and the plan read every fact stamped above (float, dilution, volume, lateness...)
            from .odds import predict

            odds = predict(self.odds, alert.to_dict(), self.regime_label())
            if not odds and self.odds.get("paused"):
                sh = predict(self.odds, alert.to_dict(), self.regime_label(), shadow=True)
                if sh:
                    alert.edge["odds_shadow"] = {"p": sh["p"]}  # not shown or used: how it recovers
            if odds:
                alert.edge["odds"] = odds  # one calibrated number, with what moved it
                if odds.get("ev") is not None and self.ev_hist[0]:
                    evs, days = self.ev_hist
                    # how many alerts a day are at least this good: what an attention budget reads
                    alert.edge["ev_top"] = round(sum(1 for v in evs if v >= odds["ev"]) / days, 1)
            from .plans import trade_plan

            try:
                plan = trade_plan(self, "", alert.to_dict(), time.time())
            except Exception:  # noqa: BLE001 - a plan is a bonus, never a reason not to publish
                plan = {}
            if plan.get("ok"):  # the plan from the live price, for the push: stop, target, reward-to-risk
                alert.edge["plan"] = {"dir": plan["direction"], "symbol": plan["symbol"], "entry": plan["entry"],
                                      "stop": plan["stop"], "target": plan["target"], "rr": plan["rr"]}  # fmt: skip
                pt = plan.get("patient")
                if pt:  # the patient entry, for the wait-for-a-pullback push and its grade (waits.py)
                    alert.edge["plan"]["patient"] = {
                        k: pt[k] for k in ("limit", "dip", "p_now", "p_filled") if k in pt
                    }
            ek = (alert.edge.get("catalyst") or {}).get("key")
            er = self.entry_rates.get(ek) if ek else None
            if er and er.get("best"):  # how to enter its kind of news here (entries.py)
                alert.edge["entry_rec"] = {"kind": ek, "label": er["label"], "best": er["best"], "n": er["n"],
                                           "styles": {s: er["styles"][s] for s in (er["best"], "alert") if s in er["styles"]}}  # fmt: skip
            from .thecall import the_call

            call = the_call(
                alert.edge, outside(alert.created, self.calendar.holidays), frozenset(self.call_muted)
            )
            if call:  # what the records add up to, in one line; graded as the call's own record
                rec = (self.call_rates.get(call["verb"]) or {}).get("close")
                if rec and rec["n"] >= 8:
                    call["rec"] = rec
                alert.edge["call"] = call
        self.storage.add_alert(alert)
        self._broadcast("alert", alert.to_dict())
        if push:
            self.stats["pushed"] += 1
            self._spawn(self.dispatcher.dispatch(alert))
            self._spawn(self._push_watchers(alert))
            if alert.edge.get("etf_refs") and any(p.get("holdings") for p in self.desk.profiles.values()):
                from .bookmacro import send_book

                self._spawn(send_book(self, alert.to_dict()))  # what headlines like it meant for each book
            if (alert.edge.get("catalyst") or alert.edge.get("odds")) and any(
                p.get("autopilots") for p in self.desk.profiles.values()
            ):
                self._spawn(self.autopilot_open(alert.to_dict()))
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
            "options": self.options_feed.status() if self.options_feed is not None else {"enabled": False},
            "tape": self.tape.status() if self.tape is not None else {"enabled": False},
            "why": {"enabled": True, **self.why.stats} if self.why is not None else {"enabled": False},
            "trends": self.trends.status() if self.trends is not None else {"enabled": False},
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
        patch: dict[str, Any] = {
            "lead": {"source": item.source, "lead_s": round(lead, 1), "at": item.detected}
        }
        a = self.storage.get_alert(story.alert_id) or {}
        e = a.get("edge") or {}
        sym = next(iter(e.get("outcome") or e.get("refs") or {}), None)
        q = self.price_of(sym, 900) if sym else None
        if q and q[1]:  # the price at the catch-up: what the head start captured, what's left (catchup.py)
            patch["lead"]["px"] = q[1]
            ref = (e.get("refs") or {}).get(sym)
            dirn = {"up": 1, "down": -1}.get(str((a.get("analysis") or {}).get("direction")), 0)
            if ref and dirn:
                patch["lead"]["its"] = round(dirn * (q[1] / float(ref) - 1) * 100, 2)
        if self.storage.update_alert_edge(story.alert_id, patch) is not None:
            if patch["lead"].get("px"):
                from .catchup import send_catchup

                self._spawn(send_catchup(self, story.alert_id))
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
        brief = build_brief(
            self.storage.recent_alerts(200, since=now - 4 * 86400),
            self.calendar,
            self.detector.snapshot(),
            self.storage.leads(since=now - 7 * 86400),
            now,
        )
        brief["scorecard"] = self.last_session_scorecard(now)
        brief["trends"] = (
            [
                {
                    k: r[k]
                    for k in ("ticker", "brands", "severity", "direction", "kinds", "move_pct", "created")
                }
                for r in self.trends.board(7, now)
                if r["severity"] != "LOW"
            ][:5]
            if self.trends is not None
            else []
        )
        watch = self.trends.narratives if self.trends is not None else None
        brief["narratives"] = (
            [
                {k: r[k] for k in ("id", "name", "side", "level", "sources_3d")}
                | {"ratio": r["news"]["ratio"]}
                for r in watch.scan(self.storage, self.trends.chatter, now)
                if r["level"] in ("accelerating", "elevated")
            ]
            if watch is not None
            else []
        )
        return brief

    def scorecard_for(self, day: str | None = None, now: float | None = None) -> dict[str, Any]:
        return self.scorecard.build(day, now)

    def last_session_scorecard(self, now: float | None = None) -> dict[str, Any] | None:
        """The previous session's scorecard, trimmed for the Brief: the line, the counts and the
        three gaps worth fixing first."""
        now = now or time.time()
        day = datetime.fromtimestamp(Calendar.last_close(now, self.calendar.holidays), ET).date()
        try:
            card = self.scorecard.build(day.isoformat(), now)
        except Exception as exc:  # noqa: BLE001 - the Brief must render without it
            log.warning("scorecard failed: %s", exc)
            return None
        if not card["moves"]:
            return None
        gaps = [v for v in card["verdicts"] if v["verdict"] in ("scored_low", "app", "unexplained")]
        return {
            "date": card["date"],
            "line": scorecard_line(card),
            "counts": card["counts"],
            "moves": card["moves"],
            "ahead_rate": card["ahead_rate"],
            "median_lead_s": card["median_lead_s"],
            "gaps": [
                {"symbol": v["symbol"], "verdict": v["verdict"], "hint": v["hint"], "headline": v["headline"]}
                for v in gaps[:3]
            ],
        }

    def save_scorecard(self, now: float | None = None) -> dict[str, Any] | None:
        """After 20:00 ET, keep the day's summary for the long-run record (items are pruned)."""
        now = now or time.time()
        dt = datetime.fromtimestamp(now, ET)
        if dt.hour < 20 or dt.weekday() >= 5:
            return None
        key = f"scorecard:{dt.date().isoformat()}"
        if self.storage.get_setting(key):
            return None
        card = self.scorecard.build(dt.date(), now)
        summary = {
            k: card[k] for k in ("date", "moves", "counts", "ahead_rate", "pushed_rate", "median_lead_s")
        }
        summary["line"] = scorecard_line(card)
        self.storage.set_setting(key, json.dumps(summary))
        return summary

    def scorecard_history(self, days: int = 30) -> list[dict[str, Any]]:
        rows = self.storage.db.execute(
            "SELECT value FROM settings WHERE key LIKE 'scorecard:%' ORDER BY key DESC LIMIT ?", (days,)
        )
        return [json.loads(r[0]) for r in rows]

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
        from .personal import your_stocks, your_stocks_line

        # phones with a watchlist get one more line: their own stocks this morning
        personal: dict[str, list[str]] = {}
        for sub in self.webpush.subs.values():  # type: ignore[attr-defined]
            line = your_stocks_line(your_stocks(self, sub.device, now)) if sub.device else ""
            if line:
                personal.setdefault(line, []).append(sub.endpoint)
        mine = {e for ends in personal.values() for e in ends}
        rest = [s.endpoint for s in self.webpush.subs.values() if s.endpoint not in mine]  # type: ignore[attr-defined]
        results: dict[str, str] = {}
        if rest:
            results |= await self.webpush.push(payload, urgency="normal", only=rest)  # type: ignore[attr-defined]
        for line, ends in personal.items():
            p = dict(payload, body=(payload.get("body", "") + "\n" + line).strip()[:600])
            results |= await self.webpush.push(p, urgency="normal", only=ends)  # type: ignore[attr-defined]
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

    # ------------------------------------------------------------------ your desk

    def price_of(
        self, symbol: str, max_age_s: float = 86400.0, now: float | None = None
    ) -> tuple[float, float, float | None] | None:
        """(when, price, % on the day) from the freshest source: the live tape, else the price monitor."""
        sym = (symbol or "").upper()
        q = self.live_quotes.get(sym, max_age_s, now)
        best = (q.ts, q.price, q.change_pct) if q is not None else None
        last = self.detector.last_price.get(sym)
        if last is not None and (best is None or last[0] > best[0]):
            pc = self.detector.prev_close.get(sym)
            best = (last[0], last[1], (last[1] / pc - 1.0) * 100.0 if pc else None)
        return best

    def _held(self, device: str, alert: Any) -> str | None:
        """Is this alert kept back from this phone's lock screen for a summary? "digest" (the phone
        is in digest mode) or "burst" (it already got its limit in the last 10 minutes); else None."""
        if self.desk.hold(device, alert):
            return "digest"
        wp = self.webpush
        n = int(((self.desk.profiles.get(device) or {}).get("prefs") or {}).get("burst") or 0)
        if wp is None or n <= 0:
            return None
        now = time.time()
        recent = sum(
            1
            for e in wp.log.get(device, ())  # type: ignore[attr-defined]
            if e["status"] == "sent" and len(e["id"]) == 16 and now - e["ts"] < BURST_WINDOW_S
        )
        return "burst" if self.desk.burst_hold(device, alert, recent, now) else None

    async def _push_watchers(self, alert: Alert) -> None:
        """A MEDIUM alert (shown in the app, not pushed) on a stock someone watches: push it to
        those phones, with their preferences (category, quiet hours) respected."""
        wp = self.webpush
        themes = list(alert.analysis.themes) if alert.analysis is not None else []
        if (
            wp is None
            or alert.severity != Severity.MEDIUM
            or not (alert.tickers or themes)
            or self.dispatcher.paused
        ):
            return
        if any(ch.name == "webpush" for ch in self.dispatcher.targets(alert)):
            return  # the phone floor is at MEDIUM: it went to everyone already
        secs = [
            li.sector
            for li in (self.universe.get(t) for t in alert.tickers[:6])
            if li is not None and li.sector
        ]
        payload = wp.payload(alert)  # type: ignore[attr-defined]
        payload["title"] = "★ " + payload["title"]
        devices = set()
        for d in dict.fromkeys(self.desk.watchers(alert.tickers[:6]) + self.desk.followers(themes, secs)):
            prof = self.desk.get(d)
            if not (prof.get("prefs") or {}).get("watch_push", True) and (
                self.desk.stock_level(prof, alert.tickers[:6]) != "all"
            ):
                continue
            why = self.desk.block_reason(d, alert)
            if why:
                wp.note(d, payload, "filtered", why)  # type: ignore[attr-defined]
            elif held := self._held(d, alert):
                wp.note(d, payload, "held", HELD_TEXT[held])  # type: ignore[attr-defined]
            else:
                devices.add(d)
        only = [s.endpoint for s in wp.subs.values() if s.device in devices]  # type: ignore[attr-defined]
        if not only:
            return
        for d in devices:
            self._spawn(self._forward(d, payload))
        with contextlib.suppress(Exception):
            results = await wp.push(payload, urgency="normal", only=only)  # type: ignore[attr-defined]
            wp.note_results(payload, results)  # type: ignore[attr-defined]

    async def _desk_loop(self, stop: asyncio.Event) -> None:
        """Every 15 s: price alerts that came true go to their phone. Every minute: alert outcomes
        (an hour later, at the close) are stamped, and the desk is saved to the state database."""
        from .dossier import record_outcomes

        last_outcomes = last_rates = last_month = last_bars = 0.0
        while not stop.is_set():
            await _sleep_or_stop(stop, 15)
            now = time.time()
            with contextlib.suppress(Exception):
                await self.check_price_alerts(now)
            with contextlib.suppress(Exception):
                await self.send_digests(now)
            try:
                await self.check_rules(now)
            except Exception as exc:  # noqa: BLE001 - never stop the desk loop
                log.warning("smart alerts failed: %s", exc)
            if now - last_outcomes >= 60:
                last_outcomes = now
                with contextlib.suppress(Exception):
                    from .uptime import beat

                    beat(self, now)  # the heartbeat: a gap in it is an outage (uptime.py)
                with contextlib.suppress(Exception):
                    record_outcomes(self, now)
                with contextlib.suppress(Exception):
                    from .shadow import grade as shadow_grade

                    shadow_grade(self, now)
                with contextlib.suppress(Exception):
                    from .peerlearn import grade as peers_grade

                    peers_grade(self, now)
                with contextlib.suppress(Exception):
                    from .volconfirm import send_confirms

                    await send_confirms(self, now)  # 15 minutes in: is the market trading it?
                with contextlib.suppress(Exception):
                    from .tapeday import send_tape

                    await send_tape(
                        self, now
                    )  # a cold or hot tape today, once the first alerts have their hour
                with contextlib.suppress(Exception):
                    from .pushbudget import send_digest

                    await send_digest(self, now)  # the signals a phone's budget folded, every half hour
                with contextlib.suppress(Exception):
                    from .runup import send_runups

                    await send_runups(self, now)  # ten sessions to an event on your stock
                with contextlib.suppress(Exception):
                    from .callnow import send_turns

                    await send_turns(self, now)  # the call re-read with the day's live facts
                with contextlib.suppress(Exception):
                    from .macrotape import grade as etfs_grade

                    etfs_grade(self, now)  # the ETF basket after market-wide headlines
                with contextlib.suppress(Exception):
                    from .secondleg import send_legs

                    await send_legs(self, now)  # new highs well after the news
                with contextlib.suppress(Exception):
                    from .haltrec import grade as halts_grade

                    halts_grade(self, now)  # 5 and 30 minutes after a resumption, and the close
                with contextlib.suppress(Exception):
                    from .waits import send_waits

                    await send_waits(self, now)  # the wait is over: at the open, at the patient entry
                if now - last_month >= 600:
                    last_month = now
                    with contextlib.suppress(Exception):
                        from .dossier import record_month

                        record_month(self, now)  # a month on: 20 sessions after each alert
                    with contextlib.suppress(Exception):
                        from .uptime import persist as uptime_persist

                        await uptime_persist(self)
                    if now - last_bars >= 1800:
                        last_bars = now
                        with contextlib.suppress(Exception):
                            from .bars import fill as bars_fill

                            await bars_fill(self, now)  # the closes a sleeping server missed, from daily bars
                        with contextlib.suppress(Exception):
                            from .runup import grade as runups_grade

                            await runups_grade(
                                self, now
                            )  # the run-up into events two days past, from daily bars
                        with contextlib.suppress(Exception):
                            from .intrafill import fill as intra_fill

                            await intra_fill(self, now)  # the minutes it missed, from one-minute bars
                    with contextlib.suppress(Exception):
                        from .coverage import stamp_coverage

                        stamp_coverage(self, now)  # how late each alert was to its story, how far it spread
                with contextlib.suppress(Exception):
                    await self.desk.persist()
                if self.webpush is not None:
                    with contextlib.suppress(Exception):
                        self.webpush.save_log()  # type: ignore[attr-defined]
                try:
                    await self.check_confluence(now)
                except Exception as exc:  # noqa: BLE001 - never stop the desk loop
                    log.warning("confluence scan failed: %s", exc)
                with contextlib.suppress(Exception):
                    await self.check_board_watch(now)
                with contextlib.suppress(Exception):
                    self.mark_day(now)
                with contextlib.suppress(Exception):
                    await self.auto_why(now)
                with contextlib.suppress(Exception):
                    await self.send_earnings_reminders(now)
                with contextlib.suppress(Exception):
                    await self.check_followups(now)
                with contextlib.suppress(Exception):
                    await self.check_reminders(now)
                with contextlib.suppress(Exception):
                    await self.check_sympathy(now)
                with contextlib.suppress(Exception):
                    await self.run_autopilots(now)
                with contextlib.suppress(Exception):
                    from .manage import check_manage

                    await check_manage(self, now)
                with contextlib.suppress(Exception):
                    await self.check_retests(now)
                with contextlib.suppress(Exception):
                    self.grade_retests(now)
                with contextlib.suppress(Exception):
                    await self.check_divergence(now)
                with contextlib.suppress(Exception):
                    self.grade_divergence(now)
                with contextlib.suppress(Exception):
                    await self.check_confirm(now)
                with contextlib.suppress(Exception):
                    from .stakes import grade_events, send_event_day

                    await send_event_day(self, now)
                    grade_events(self, now)
                with contextlib.suppress(Exception):
                    from .drift import send_drift_checks

                    await send_drift_checks(self, now)
                with contextlib.suppress(Exception):
                    from .overnight import send_overnight

                    await send_overnight(self, now)
                with contextlib.suppress(Exception):
                    await self.autopilot_pending(now)  # "wait for the open" calls, entered at the open
                with contextlib.suppress(Exception):
                    from .recall import snapshot as recall_snapshot

                    recall_snapshot(self, now)  # the session's big movers, caught or missed
                with contextlib.suppress(Exception):
                    self.grade_confirm(now)
                with contextlib.suppress(Exception):
                    from .exits import grade_exits

                    grade_exits(self, now)  # what each closed call's stock did after the exit
                with contextlib.suppress(Exception):
                    from .dossier import grade_sympathy

                    grade_sympathy(self, now)
                with contextlib.suppress(Exception):
                    await self.check_portfolio_risk(now)
                with contextlib.suppress(Exception):
                    self.save_memory(now)
                with contextlib.suppress(Exception):
                    self.warm_earnings_week(now)
                if self._tomorrow_due(now):
                    with contextlib.suppress(Exception):
                        await self.send_tomorrow(now)
                if self._close_due(now):
                    with contextlib.suppress(Exception):
                        await self.send_close(now)
                if self._week_due(now):
                    with contextlib.suppress(Exception):
                        await self.send_week_cards(now)
                with contextlib.suppress(Exception):
                    await self.send_open_plans(now)
                with contextlib.suppress(Exception):
                    await self.send_setups(now)
                if self._eod_due(now):
                    with contextlib.suppress(Exception):
                        await self.send_report_cards(now)
            if now - last_rates >= 300:
                last_rates = now
                with contextlib.suppress(Exception):
                    self.refresh_base_rates(now)

    async def _to_device(self, device: str, payload: dict[str, Any], urgency: str = "high") -> bool:
        """Push to one phone's subscriptions only (its price alerts, its levels, its report card)."""
        wp = self.webpush
        if wp is None:
            return False
        from .pushbudget import admit

        if not admit(self, device, payload):
            return False  # a signal over this phone's hourly budget: folded into the next digest
        only = [s.endpoint for s in wp.subs.values() if s.device == device]  # type: ignore[attr-defined]
        self._spawn(self._forward(device, payload))
        if not only:
            return False
        try:
            results = await wp.push(payload, urgency=urgency, only=only)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - one phone must never stop the loop
            wp.note(device, payload, "failed", f"{type(exc).__name__}: {exc}")  # type: ignore[attr-defined]
            return False
        wp.note_results(payload, results)  # type: ignore[attr-defined]
        return any(r == "ok" for r in results.values())

    async def check_price_alerts(self, now: float | None = None) -> list[tuple[str, dict[str, Any]]]:
        """Price alerts that came true, and tracked calls whose stop or target was reached: each
        goes to its own phone (and a toast in the app), once."""
        now = now or time.time()
        fired = self.desk.due_price_alerts(self.price_of, now)
        for device, a, q in fired:
            v = a["value"]
            what = {
                "above": f"is above ${v:,.2f}",
                "below": f"is below ${v:,.2f}",
                "move": f"is {fmt_pct(q[2] or 0)} on the day (your line: ±{v:g}%)",
                "trail": f"fell {v:g}% from its high of ${a.get('peak') or 0:,.2f}",
            }[a["kind"]]
            title = f"🔔 {a['symbol']} {what}"
            body = (
                f"${q[1]:,.2f}"
                + (f" ({fmt_pct(q[2])} today)" if q[2] is not None else "")
                + " · your price alert"
            )
            self._broadcast(
                "price_alert", {"device": device, "symbol": a["symbol"], "title": title, "body": body}
            )
            payload = {"id": f"pa-{a['id']}", "title": title, "body": body, "severity": "HIGH", "kind": "system",
                       "tickers": [a["symbol"]], "tag": f"pa-{a['id']}", "ts": now, "open": f"/app/#t={a['symbol']}"}  # fmt: skip
            await self._to_device(device, payload)
        for device, t, which, q in self.desk.due_levels(self.price_of, now):
            r = (q[1] / t["entry"] - 1.0) * 100.0 * (1 if t["direction"] == "long" else -1)
            title = f"{'🎯' if which == 'target' else '🛑'} {t['symbol']} hit your {which} (${t[which]:,.2f})"
            body = f"{t['direction'].title()} from ${t['entry']:,.2f}, now ${q[1]:,.2f} · {fmt_pct(r)} · paper call"
            self._broadcast(
                "price_alert", {"device": device, "symbol": t["symbol"], "title": title, "body": body}
            )
            payload = {"id": f"lv-{t['id']}-{which}", "title": title, "body": body, "severity": "HIGH", "kind": "system",
                       "tickers": [t["symbol"]], "tag": f"lv-{t['id']}", "ts": now, "open": f"/app/#t={t['symbol']}"}  # fmt: skip
            await self._to_device(device, payload)
        return [(d, a) for d, a, _ in fired]

    async def autopilot_open(self, alert: dict[str, Any], now: float | None = None) -> int:
        """A new alert matching a phone's autopilot opens a paper call there, with its plan's stop
        and target and the rule's exit time (autopilot.py). At most three a rule a day. A rule
        following the call schedules a "wait for the open" alert for the session's first print."""
        from .autopilot import PER_RULE_PER_DAY, matches, opened_today, waits_for_open

        now = now or time.time()
        opened = 0
        for dev, p in list(self.desk.profiles.items()):
            for rule in p.get("autopilots") or []:
                if opened_today(p, rule["id"], now) >= PER_RULE_PER_DAY:
                    continue
                if matches(rule, alert):
                    opened += await self._auto_enter(dev, p, rule, alert, now)
                elif waits_for_open(rule, alert):
                    ms = self.calendar.market_status(now)
                    if ms.get("open") or ms.get("until_s") is None:
                        continue  # in the session already: nothing to wait for
                    due = now + float(ms["until_s"])  # the next open
                    pend = p.setdefault("ap_pending", [])
                    if not any(x["alert_id"] == alert["id"] and x["rule"] == rule["id"] for x in pend):
                        pend.append(
                            {"rule": rule["id"], "alert_id": alert["id"], "due": float(due), "created": now}
                        )
                        self.desk.save(dev)
        return opened

    async def autopilot_signal(
        self, alert: dict[str, Any], signal: str, how: str, now: float | None = None
    ) -> int:
        """A live signal on an alert (a second leg, the call turning to act): rules following it open
        a paper call now, at the live price, with a fresh plan. At most three a rule a day."""
        from .autopilot import PER_RULE_PER_DAY, opened_today, signal_matches

        now = now or time.time()
        opened = 0
        for dev, p in list(self.desk.profiles.items()):
            for rule in p.get("autopilots") or []:
                if (
                    signal_matches(rule, alert, signal)
                    and opened_today(p, rule["id"], now) < PER_RULE_PER_DAY
                ):
                    opened += await self._auto_enter(dev, p, rule, alert, now, how)
        return opened

    async def _auto_enter(self, dev: str, p: dict[str, Any], rule: dict[str, Any], alert: dict[str, Any], now: float,
                          how: str = "") -> int:  # fmt: skip
        """Open one autopilot paper call from an alert's plan, now (with a push)."""
        from .autopilot import exit_time
        from .plans import trade_plan

        plan = trade_plan(self, dev, alert, now)
        if not plan.get("ok"):
            return 0
        direction = "long" if plan["direction"] == "up" else "short"
        try:
            self.desk.apply(dev, "track_add", {"symbol": plan["symbol"], "direction": direction, "alert_id": alert["id"],
                                               "stop": plan["stop"], "target": plan["target"], "entry": plan["entry"],
                                               "note": f"#autopilot {rule['label']}"[:200]}, self.price_of)  # fmt: skip
        except ValueError:
            return 0
        t = p["tracked"][-1]
        t["auto"], t["exit_at"] = rule["id"], exit_time(rule["exit"], now, self.calendar)
        self.desk.save(dev)
        payload = {"id": f"ap-{t['id']}", "title": f"🤖 Autopilot: {direction} {plan['symbol']} at ${plan['entry']:,.2f}{how}",
                   "body": f"{alert.get('title', '')[:120]}\nStop ${plan['stop']:,.2f} · target ${plan['target']:,.2f} · {rule['label']}",
                   "severity": "HIGH", "kind": "system", "tickers": [plan["symbol"]], "tag": f"ap-{t['id']}",
                   "ts": now, "open": f"/app/#t={plan['symbol']}"}  # fmt: skip
        await self._to_device(dev, payload, urgency="normal")
        return 1

    async def autopilot_pending(self, now: float | None = None) -> int:
        """Enter each "wait for the open" call a rule scheduled, at the session's first 15 minutes;
        one not entered within half an hour of the open is dropped."""
        now = now or time.time()
        if self.calendar.market_status(now).get("phase") != "open":
            for p in self.desk.profiles.values():  # long past due (a missed session): drop
                if p.get("ap_pending"):
                    p["ap_pending"] = [x for x in p["ap_pending"] if now - x["due"] < 1800]
            return 0
        opened = 0
        for dev, p in list(self.desk.profiles.items()):
            pend = p.get("ap_pending") or []
            if not pend:
                continue
            keep = []
            for x in pend:
                if now < x["due"]:
                    keep.append(x)
                    continue
                rule = next(
                    (r for r in p.get("autopilots") or [] if r["id"] == x["rule"] and r.get("on")), None
                )
                a = self.storage.get_alert(x["alert_id"])
                if rule and a and now - x["due"] <= 900:
                    opened += await self._auto_enter(dev, p, rule, a, now, " (at the open, as the call said)")
            p["ap_pending"] = keep
            self.desk.save(dev)
        return opened

    async def run_autopilots(self, now: float | None = None) -> int:
        """Close each autopilot call at its exit time, or at its stop or target, with a push."""
        from .autopilot import EXITS, due_exit

        now = now or time.time()
        closed = 0
        for dev, p in list(self.desk.profiles.items()):
            for t in list(p.get("tracked") or []):
                if not t.get("auto"):
                    continue
                q = self.price_of(t["symbol"], 1800, now=now)
                why = due_exit(t, q[1] if q else None, now)
                if not why or (why == "time" and not q):
                    continue
                rule = next((r for r in p.get("autopilots") or [] if r["id"] == t["auto"]), None)
                lesson = {"stop": "autopilot: stopped out", "target": "autopilot: target reached",
                          "time": f"autopilot: out {EXITS.get((rule or {}).get('exit', 'close'), 'on time')}"}[why]  # fmt: skip
                self.desk.apply(dev, "track_close", {"id": t["id"], "lesson": lesson}, lambda s, _q=q: _q)
                done = p["closed"][0]
                closed += 1
                pnl = done.get("pnl_pct")
                payload = {"id": f"apx-{t['id']}", "title": f"🤖 Closed {t['direction']} {t['symbol']} {pnl:+.2f}%" if pnl is not None else f"🤖 Closed {t['symbol']}",
                           "body": lesson.replace("autopilot: ", "").capitalize() + (f" · {rule['label']}" if rule else ""),
                           "severity": "HIGH", "kind": "system", "tickers": [t["symbol"]], "tag": f"ap-{t['id']}",
                           "ts": now, "open": "/app/#a=desk"}  # fmt: skip
                await self._to_device(dev, payload, urgency="normal")
        return closed

    async def check_retests(self, now: float | None = None) -> int:
        """Second chance: a stock that ran its alert's way (1%+, or 40% of its kind's typical move)
        and has come back to within 0.3% of its alert price, when that kind of news went its way 60%+
        of the time by the close. One push to each phone that watches, holds or opened it; every
        flag logged and graded at the close."""
        from .catalysts import RETEST_BAND, RETEST_RAN
        from .dossier import _direction, _since

        now = now or time.time()
        if self.webpush is None or self.calendar.market_status(now).get("phase") != "open":
            return 0
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if len(self._retest_best) > 3000:  # yesterday's alerts: forget them
            self._retest_best.clear()
        sent_key = f"retest:{day}"
        sent_keys = set(json.loads(self.storage.get_setting(sent_key) or "[]"))
        log: list[dict[str, Any]] | None = None
        sent = 0
        for a in self.storage.recent_alerts(300, since=day0):
            cb = (a.get("edge") or {}).get("cat_base") or {}
            close = cb.get("close") or {}
            d = _direction(a)
            if not close or close.get("hit", 0) < 0.6 or close.get("median", 0) <= 0 or not d:
                continue
            sym = (((a.get("edge") or {}).get("play") or {}).get("direct") or a.get("tickers") or [""])[0]
            since = _since(a, sym, self.price_of) if sym else None
            if since is None:
                continue
            its_way = (1 if d == "up" else -1) * since
            best = max(self._retest_best.get(a["id"], 0.0), its_way)
            self._retest_best[a["id"]] = best
            ran = max(RETEST_RAN, 0.4 * (cb.get("typical") or 0))
            if best < ran or abs(its_way) > RETEST_BAND:
                continue
            ref = (a.get("edge") or {}).get("refs", {}).get(sym)
            if log is None:
                log = json.loads(self.storage.get_setting("retest_log") or "[]")
            if not any(e["alert_id"] == a["id"] for e in log):
                log.append({"ts": now, "day": day, "alert_id": a["id"], "symbol": sym, "sign": 1 if d == "up" else -1,
                            "ref": ref, "ran": round(best, 2), "close": None})  # fmt: skip
            devices = set(self.desk.watchers([sym])) | {
                dv
                for dv, p in self.desk.profiles.items()
                if a["id"] in (p.get("opened") or []) or a["id"] in (p.get("saved") or [])
            }
            for dev in devices:
                key = f"{dev}:{a['id']}"
                if key in sent_keys or not (self.desk.get(dev).get("prefs") or {}).get("retest", True):
                    continue
                probe = {
                    "kind": a.get("kind"),
                    "severity": "HIGH",
                    "tickers": [sym],
                    "edge": {"catalyst": (a.get("edge") or {}).get("catalyst")},
                }
                if not self.desk.allows(dev, probe, now):
                    continue
                sent_keys.add(key)
                q = self.price_of(sym)
                line = f"{cb['label']}, {cb['band_label']}: {round(close['hit'] * 100)}% went their way by the close (median {close['median']:+.1f}%, {close['n']})"
                payload = {"id": f"rt-{a['id']}", "title": f"↩ {sym} is back at its alert price (${q[1]:,.2f})" if q else f"↩ {sym} is back at its alert price",
                           "body": f"It ran {best:+.1f}% its way after “{a.get('title', '')[:100]}” and came back.\n{line}\nA second look, not a signal.",
                           "severity": "HIGH", "kind": "system", "tickers": [sym], "tag": f"rt-{sym}", "ts": now,
                           "open": f"/app/#a={a['id']}"}  # fmt: skip
                if await self._to_device(dev, payload):
                    sent += 1
        if log is not None:
            self.storage.set_setting("retest_log", json.dumps(log[-500:]))
        self.storage.set_setting(sent_key, json.dumps(sorted(sent_keys)))
        return sent

    def regime_label(self, now: float | None = None) -> str:
        """The live market regime (dossier.regime), re-read at most every 5 minutes."""
        now = now or time.time()
        ts, label = self._regime_now
        if now - ts > 300:
            from .dossier import regime

            try:
                label = regime(self, now)["label"]
            except Exception:  # noqa: BLE001 - no quotes yet: unknown
                label = "unknown"
            self._regime_now = (now, label)
        return label

    async def check_divergence(self, now: float | None = None) -> int:
        """The market isn't buying it: an alert 15-90 minutes old whose kind of news went its way
        60%+ of the time by the close, and the stock is going the other way by 1.5%+ (or half that
        kind's typical move). One push to phones that watch, hold, opened or saved it; each flag
        logged and graded at the close (did it keep going against the news?)."""
        from .dossier import _direction, _since

        now = now or time.time()
        if self.webpush is None or self.calendar.market_status(now).get("phase") != "open":
            return 0
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        sent_key = f"diverge:{day}"
        sent_keys = set(json.loads(self.storage.get_setting(sent_key) or "[]"))
        log: list[dict[str, Any]] | None = None
        sent = 0
        for a in self.storage.recent_alerts(300, since=now - 90 * 60):
            if now - a.get("created", now) < 15 * 60:
                continue
            cb = (a.get("edge") or {}).get("cat_base") or {}
            close = cb.get("close") or {}
            d = _direction(a)
            if not close or close.get("hit", 0) < 0.6 or close.get("median", 0) <= 0 or not d:
                continue
            sym = (((a.get("edge") or {}).get("play") or {}).get("direct") or a.get("tickers") or [""])[0]
            since = _since(a, sym, self.price_of) if sym else None
            if since is None:
                continue
            sign = 1 if d == "up" else -1
            its_way = sign * since
            if its_way > -max(1.5, 0.5 * (cb.get("typical") or 0)):
                continue
            ref = (a.get("edge") or {}).get("refs", {}).get(sym)
            if log is None:
                log = json.loads(self.storage.get_setting("diverge_log") or "[]")
            if not any(e["alert_id"] == a["id"] for e in log):
                log.append({"ts": now, "day": day, "alert_id": a["id"], "symbol": sym, "sign": sign, "ref": ref,
                            "against": round(-its_way, 2), "close": None})  # fmt: skip
            devices = set(self.desk.watchers([sym])) | {
                dv
                for dv, p in self.desk.profiles.items()
                if a["id"] in (p.get("opened") or []) or a["id"] in (p.get("saved") or [])
            }
            good = "good" if sign > 0 else "bad"
            for dev in devices:
                key = f"{dev}:{a['id']}"
                if key in sent_keys or not (self.desk.get(dev).get("prefs") or {}).get("diverge", True):
                    continue
                probe = {
                    "kind": a.get("kind"),
                    "severity": "HIGH",
                    "tickers": [sym],
                    "edge": {"catalyst": (a.get("edge") or {}).get("catalyst")},
                }
                if not self.desk.allows(dev, probe, now):
                    continue
                sent_keys.add(key)
                line = f"{cb['label']}, {cb['band_label']}: {round(close['hit'] * 100)}% went their way by the close (median {close['median']:+.1f}%, {close['n']})"
                payload = {"id": f"dv-{a['id']}", "title": f"⚠ {sym} {since:+.1f}% on {good} news",
                           "body": f"“{a.get('title', '')[:110]}”\n{line}\nThe market isn't buying it so far.",
                           "severity": "HIGH", "kind": "system", "tickers": [sym], "tag": f"dv-{sym}", "ts": now,
                           "open": f"/app/#a={a['id']}"}  # fmt: skip
                if await self._to_device(dev, payload):
                    sent += 1
        if log is not None:
            self.storage.set_setting("diverge_log", json.dumps(log[-500:]))
        self.storage.set_setting(sent_key, json.dumps(sorted(sent_keys)))
        return sent

    async def check_confirm(self, now: float | None = None) -> int:
        """The news is holding: an alert 1-3 hours old, moving its way (0.25%+), whose live odds say
        alerts like it finished their way 80%+ of the time (10+ graded). One push to phones that
        watch, hold, opened or saved it; each flag logged and graded at the close from its price."""
        from .liveodds import live_line, live_odds

        now = now or time.time()
        if self.webpush is None or self.calendar.market_status(now).get("phase") != "open":
            return 0
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        sent_key = f"confirm:{day}"
        sent_keys = set(json.loads(self.storage.get_setting(sent_key) or "[]"))
        log: list[dict[str, Any]] | None = None
        sent = 0
        for a in self.storage.recent_alerts(300, since=now - 3 * 3600):
            if a.get("kind") == "system" or now - a.get("created", now) < 3600:
                continue
            lo = live_odds(self, a, now)
            if not lo or lo["p"] < CONFIRM_ODDS or lo["n"] < 10 or lo["since"] < 0.25:
                continue
            sym = lo["symbol"]
            q = self.price_of(sym, 900, now=now)
            if log is None:
                log = json.loads(self.storage.get_setting("confirm_log") or "[]")
            if not any(e["alert_id"] == a["id"] for e in log):
                from .dossier import _direction

                sign = 1 if _direction(a) == "up" else -1
                log.append({"ts": now, "day": day, "alert_id": a["id"], "symbol": sym, "sign": sign,
                            "ref": q[1] if q else None, "p": lo["p"], "since": lo["since"], "close": None})  # fmt: skip
            devices = set(self.desk.watchers([sym])) | {
                dv
                for dv, p in self.desk.profiles.items()
                if a["id"] in (p.get("opened") or []) or a["id"] in (p.get("saved") or [])
            }
            for dev in devices:
                key = f"{dev}:{a['id']}"
                if key in sent_keys or not (self.desk.get(dev).get("prefs") or {}).get("confirm", True):
                    continue
                probe = {"kind": a.get("kind"), "severity": "HIGH", "tickers": [sym],
                         "edge": {"catalyst": (a.get("edge") or {}).get("catalyst")}}  # fmt: skip
                if not self.desk.allows(dev, probe, now):
                    continue
                sent_keys.add(key)
                payload = {"id": f"cf-{a['id']}", "title": f"✅ {sym}'s news is holding: {lo['since']:+.1f}% {lo['hours']:g} hours in",
                           "body": f"“{a.get('title', '')[:110]}”\n{live_line(lo)}\nA second look, not a signal.",
                           "severity": "HIGH", "kind": "system", "tickers": [sym], "tag": f"cf-{sym}", "ts": now,
                           "open": f"/app/#a={a['id']}"}  # fmt: skip
                if await self._to_device(dev, payload):
                    sent += 1
        if log is not None:
            self.storage.set_setting("confirm_log", json.dumps(log[-500:]))
        self.storage.set_setting(sent_key, json.dumps(sorted(sent_keys)))
        return sent

    def grade_confirm(self, now: float | None = None) -> int:
        """Confirmation flags graded at the close: the move its way from the price when it was flagged."""
        now = now or time.time()
        raw = self.storage.get_setting("confirm_log")
        if not raw:
            return 0
        log = json.loads(raw)
        graded = 0
        for e in log:
            if e.get("close") is not None or not e.get("ref"):
                continue
            close = (
                datetime.fromtimestamp(e["ts"], ET)
                .replace(hour=16, minute=0, second=0, microsecond=0)
                .timestamp()
            )
            q = self.price_of(e["symbol"], 900, now=now)
            if q and close <= now <= close + 1800:
                e["close"] = round(e["sign"] * (q[1] / e["ref"] - 1.0) * 100.0, 2)
                graded += 1
        if graded:
            self.storage.set_setting("confirm_log", json.dumps(log))
        return graded

    def grade_divergence(self, now: float | None = None) -> int:
        """Divergence flags graded at the close: how far it went against the news, from the alert price."""
        now = now or time.time()
        raw = self.storage.get_setting("diverge_log")
        if not raw:
            return 0
        log = json.loads(raw)
        graded = 0
        for e in log:
            if e.get("close") is not None or not e.get("ref"):
                continue
            close = (
                datetime.fromtimestamp(e["ts"], ET)
                .replace(hour=16, minute=0, second=0, microsecond=0)
                .timestamp()
            )
            q = self.price_of(e["symbol"], 900, now=now)
            if q and close <= now <= close + 1800:
                e["close"] = round(
                    -e["sign"] * (q[1] / e["ref"] - 1.0) * 100.0, 2
                )  # against the news: positive
                graded += 1
        if graded:
            self.storage.set_setting("diverge_log", json.dumps(log))
        return graded

    def grade_retests(self, now: float | None = None) -> int:
        """Second-chance flags graded at the close (16:00-16:30 ET that day): the move its way from
        the alert price."""
        now = now or time.time()
        raw = self.storage.get_setting("retest_log")
        if not raw:
            return 0
        log = json.loads(raw)
        graded = 0
        for e in log:
            if e.get("close") is not None or not e.get("ref"):
                continue
            close = (
                datetime.fromtimestamp(e["ts"], ET)
                .replace(hour=16, minute=0, second=0, microsecond=0)
                .timestamp()
            )
            q = self.price_of(e["symbol"], 900, now=now)
            if q and close <= now <= close + 1800:
                e["close"] = round(e["sign"] * (q[1] / e["ref"] - 1.0) * 100.0, 2)
                graded += 1
        if graded:
            self.storage.set_setting("retest_log", json.dumps(log))
        return graded

    async def check_sympathy(self, now: float | None = None) -> int:
        """A stock with a HIGH alert today is up (or down) 4%+ the way of its news, and a peer of it
        that a phone watches or holds hasn't moved yet (a quarter of the move or less): tell that
        phone once, with how past flags like it played out. At most three a phone a day; the
        phone's categories, mutes, dials and focus apply. Every flag is logged and graded later."""
        from .dossier import SYMPATHY_MIN_MOVE, SYMPATHY_PER_DAY, _direction, peers, sympathy_record

        now = now or time.time()
        if self.webpush is None or self.calendar.market_status(now).get("phase") != "open":
            return 0
        watched: dict[str, list[str]] = {}
        for dev, p in self.desk.profiles.items():
            if (p.get("prefs") or {}).get("sympathy", True):
                for sym in set(p.get("watchlist") or []) | set(p.get("holdings") or {}):
                    watched.setdefault(sym, []).append(dev)
        if not watched:
            return 0
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        st = self._sympathy_day
        if st["day"] != day:
            saved = self.storage.get_setting(f"sympathy:{day}")
            st.update(json.loads(saved) if saved else {"day": day, "keys": [], "count": {}})
        day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        leaders: dict[str, dict[str, Any]] = {}
        for a in self.storage.recent_alerts(300, since=max(day0, now - 4 * 3600)):
            if a.get("severity") not in ("HIGH", "CRITICAL") or a.get("kind") == "system":
                continue
            lead = ((a.get("edge") or {}).get("play") or {}).get("direct") or a.get("tickers") or []
            if lead and lead[0] not in leaders:
                leaders[lead[0]] = a
        record = None
        log: list[dict[str, Any]] | None = None
        sent = 0
        for lead, a in leaders.items():
            q = self.price_of(lead, 1800, now=now)
            if not q or q[2] is None or abs(q[2]) < SYMPATHY_MIN_MOVE:
                continue
            sign = 1 if q[2] > 0 else -1
            d = _direction(a)
            if d and {"up": 1, "down": -1}[d] != sign:
                continue  # moving against its own news: not the news moving it
            pr = peers(self, lead, now, limit=12)
            for lag in pr["laggards"]:
                devs = [dv for dv in watched.get(lag["symbol"], []) if dv]
                if not devs:
                    continue
                lq = self.price_of(lag["symbol"], 1800, now=now)
                if log is None:
                    log = json.loads(self.storage.get_setting("sympathy_log") or "[]")
                if lq and not any(
                    e["day"] == day and e["lead"] == lead and e["lag"] == lag["symbol"] for e in log
                ):
                    log.append({"day": day, "ts": now, "lead": lead, "lag": lag["symbol"], "sign": sign,
                                "lead_chg": round(q[2], 2), "ref": lq[1], "h1": None, "close": None})  # fmt: skip
                for dev in devs:
                    key = f"{dev}:{lead}:{lag['symbol']}"
                    if key in st["keys"] or st["count"].get(dev, 0) >= SYMPATHY_PER_DAY:
                        continue
                    probe = {
                        "kind": a.get("kind"),
                        "severity": "HIGH",
                        "tickers": [lag["symbol"]],
                        "edge": {},
                    }
                    if not self.desk.allows(dev, probe, now):
                        continue
                    st["keys"].append(key)
                    st["count"][dev] = st["count"].get(dev, 0) + 1
                    if record is None:
                        record = sympathy_record(self, now)
                    past = record.get("close") or record.get("h1")
                    from .peerlearn import pair, pair_line

                    learned = pair_line(pair(self.pair_rates, lead, lag["symbol"]))  # this pair's own record
                    line = learned or (
                        f"Past flags: {round(past['hit'] * past['n'])} of {past['n']} caught up "
                        f"{'by the close' if record.get('close') else 'within the hour'} (median {past['median']:+.1f}%)."
                        if past and past["n"] >= 8
                        else "A lead to check, not a signal: sympathy moves come late, small, or not at all."
                    )
                    payload = {"id": f"sym-{lead}-{lag['symbol']}-{day}",
                               "title": f"🔗 {lead} {q[2]:+.1f}%: your {lag['symbol']} hasn't moved ({lag['chg']:+.1f}%)",
                               "body": f"Same {pr['basis']}: {pr['group']}. {lead}'s news: “{a.get('title', '')[:120]}”\n{line}",
                               "severity": "HIGH", "kind": "system", "tickers": [lag["symbol"], lead],
                               "tag": f"sym-{lag['symbol']}", "ts": now, "open": f"/app/#t={lag['symbol']}"}  # fmt: skip
                    if await self._to_device(dev, payload):
                        sent += 1
        if log is not None:
            self.storage.set_setting("sympathy_log", json.dumps(log[-500:]))
        self.storage.set_setting(f"sympathy:{day}", json.dumps(st))
        return sent

    async def check_reminders(self, now: float | None = None) -> int:
        """ "Remind me" on an alert: when it comes due, the alert comes back to that phone, with
        how the stock has moved since."""
        now = now or time.time()
        sent = 0
        for device, r in self.desk.due_reminders(now):
            alert = self.storage.get_alert(r["alert_id"]) or {}
            refs = (alert.get("edge") or {}).get("refs") or {}
            bits = []
            for sym in r.get("tickers") or []:
                q, ref = self.price_of(sym), refs.get(sym)
                if q and ref:
                    bits.append(f"{sym} {(q[1] / ref - 1.0) * 100.0:+.1f}% since the alert")
            payload = {"id": f"rm-{r['id']}", "title": f"⏰ {r['title'][:150]}",
                       "body": " · ".join(bits) or "You asked to be reminded about this alert.",
                       "severity": "HIGH", "kind": "system", "tickers": (r.get("tickers") or [])[:2],
                       "tag": f"rm-{r['alert_id'][:20]}", "ts": now, "open": f"/app/#a={r['alert_id']}"}  # fmt: skip
            if await self._to_device(device, payload):
                sent += 1
        return sent

    async def _target_push(self, item: NewsItem) -> int:
        """An analyst moves a target on a stock a phone watches or holds by 15%+, or puts it 25%+
        from the price: that phone gets it (targets.py). Once per headline per phone."""
        from .targets import is_big, parse_target

        pt = parse_target(item.title)
        if pt is None:
            return 0
        sym = item.tickers[0]
        q = self.price_of(sym, 86400)
        px = q[1] if q else None
        if not is_big(pt, px):
            return 0
        verb = {"raise": "raises", "cut": "cuts", "initiate": "starts", "maintain": "keeps", "set": "sets"}[
            pt["action"]
        ]
        who = pt["firm"] or "An analyst"
        title = f"🎯 {who} {verb} {sym} target to ${pt['to']:,.2f}".replace(".00", "")
        bits = []
        if pt["change_pct"] is not None:
            bits.append(f"{pt['change_pct']:+.0f}% from ${pt['from']:,.2f}".replace(".00", ""))
        if px:
            bits.append(f"{(pt['to'] / px - 1) * 100:+.0f}% from the price (${px:,.2f})")
        if pt["rating"]:
            bits.append(pt["rating"])
        from .firms import ACTIONS, firm_base, firm_line

        record = ""
        if pt["firm"] and pt["action"] in ACTIONS:
            record = firm_line(firm_base(self.firm_rates, {"firm": pt["firm"], "action": pt["action"],
                                                          "label": f"{pt['firm']} {ACTIONS[pt['action']]}"}))  # fmt: skip
        probe = {"kind": "news", "severity": "HIGH", "tickers": [sym], "edge": {}}
        sent = 0
        for device in self.desk.watchers([sym]):
            if not self.desk.allows(device, probe):
                continue
            payload = {"id": f"pt-{item.uid[-20:]}", "title": title[:160],
                       "body": " · ".join(bits) + f"\n{item.title[:200]}" + (f"\n{record}" if record else ""),
                       "severity": "HIGH", "kind": "system", "tickers": [sym], "url": item.url,
                       "tag": f"pt-{sym}", "ts": time.time(), "open": f"/app/#t={sym}"}  # fmt: skip
            if await self._to_device(device, payload):
                sent += 1
        return sent

    async def _keyword_push(self, item: NewsItem, analysis: Analysis, story_id: int) -> int:
        """A scored headline naming a phone's keyword goes to that phone, even below the alert
        line: once per story per phone, not in its quiet hours, not about only muted stocks, and
        not when the story is HIGH+ (the alert itself is on its way)."""
        from .desk import in_quiet

        if analysis.score < KW_MIN_SCORE or any(r.startswith("muted") for r in analysis.reasons):
            return 0
        text = f"{item.title}\n{item.summary or ''}"
        now = time.time()
        loud = self.cfg.scoring.severity_for(analysis.score) >= Severity.HIGH
        sent = 0
        for device, pats in self.desk.keyword_devices().items():
            hit = next((k for k, rx in pats if rx.search(text)), None)
            if hit is None or (device, story_id) in self._kw_sent or self.desk.neg_match(device, item.title):
                continue
            self._kw_sent[(device, story_id)] = now
            self.desk.record_kw_hit(device, {"k": hit, "title": item.title[:200], "url": item.url, "source": item.source,
                                             "ts": now, "score": round(analysis.score), "tickers": analysis.tickers[:4]})  # fmt: skip
            self._broadcast("kw_hit", {"device": device, "k": hit, "title": item.title[:200]})
            prefs = self.desk.get(device).get("prefs") or {}
            if (
                loud
                or in_quiet(prefs.get("quiet"), now)
                or self.desk.all_muted(device, {"tickers": analysis.tickers}, now)
            ):
                continue
            tick = f" · {' '.join(analysis.tickers[:4])}" if analysis.tickers else ""
            payload = {"id": f"kw-{item.uid[:20]}", "title": f"🔑 {hit}: {item.title[:150]}",
                       "body": f"{item.source} · score {analysis.score:.0f}{tick} · your keyword", "severity": "MEDIUM",
                       "kind": "system", "tickers": analysis.tickers[:4], "url": item.url, "tag": f"kw-{item.uid[:24]}",
                       "ts": now, "open": "/app/#q=" + quote(hit)}  # fmt: skip
            if self.desk.hold(device, payload, now):
                continue
            if await self._to_device(device, payload, urgency="normal"):
                sent += 1
        if len(self._kw_sent) > 5000:
            for k, _ in sorted(self._kw_sent.items(), key=lambda kv: kv[1])[:2500]:
                del self._kw_sent[k]
        return sent

    async def _halt_watch(self, item: NewsItem, analysis: Analysis) -> int:
        """A trading halt on a stock someone watches reaches their phone, even when the halt itself
        scored under the alert line (a MEDIUM or higher one already goes to watchers), and so does
        its resumption, which replaces the halt notification."""
        resume = bool(item.extra.get("halt_resume"))
        if not resume and self.cfg.scoring.severity_for(analysis.score) >= Severity.MEDIUM:
            return 0
        probe = {"kind": "news", "severity": "HIGH", "tickers": item.tickers}
        sent = 0
        for device in self.desk.watchers(item.tickers):
            if not self.desk.allows(device, probe):
                continue
            title = item.title[:160] if resume else f"⏸ {item.title[:160]}"
            body = item.summary or ""
            if resume:
                with contextlib.suppress(Exception):
                    from .haltrec import resume_line

                    rl = resume_line(self, item.tickers[0])
                    body = f"{body}\n{rl}." if rl else body
            payload = {"id": f"halt-{item.uid[-20:]}", "title": title, "body": body[:400],
                       "severity": "HIGH", "kind": "system", "tickers": item.tickers[:2], "url": item.url,
                       "tag": f"halt-{item.tickers[0]}", "ts": time.time(), "open": f"/app/#t={item.tickers[0]}"}  # fmt: skip
            if await self._to_device(device, payload):
                sent += 1
        return sent

    async def check_board_watch(self, now: float | None = None) -> int:
        """A watched stock that lands on the radar or the options board, with no alert of that kind
        on it today, reaches its watchers' phones: once per phone, stock and board a day."""
        now = now or time.time()
        if self.webpush is None:
            return 0
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        st = self._board_seen
        if st["day"] != day:
            saved = self.storage.get_setting(f"boardwatch:{day}")
            st.update(json.loads(saved) if saved else {"day": day, "keys": []})
        watched = {s for p in self.desk.profiles.values() for s in p.get("watchlist") or []}
        if not watched:
            return 0
        day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        events: list[tuple[str, str, str, str]] = []
        if self.radar is not None:
            for sym, r in list(self.radar.board.items()):
                if sym in watched and (r.get("flagged") or r.get("ignition")):
                    ig = r.get("ignition") or {}
                    what = (
                        f"igniting {ig['move_pct']:+.1f}%"
                        if ig.get("move_pct") is not None
                        else f"{(r.get('change_pct') or 0):+.1f}%"
                    )
                    vol = (
                        f" on {round(ig.get('burst') or r.get('rvol') or 0)}× volume"
                        if (ig.get("burst") or r.get("rvol"))
                        else ""
                    )
                    events.append((sym, "radar", f"★ {sym} on the radar: {what}{vol}", "radar"))
        if self.options_radar is not None:
            for r in self.options_radar.snapshot(200):
                if r["symbol"] in watched and (r.get("detected") or 0) >= day0:
                    calls, puts = r.get("call_premium") or 0, r.get("put_premium") or 0
                    events.append(
                        (
                            r["symbol"],
                            "options",
                            f"★ {r['symbol']} on the options board: {r.get('kind', 'flow')} · calls ${calls / 1e6:.1f}M · puts ${puts / 1e6:.1f}M",
                            "options",
                        )
                    )
        sent = 0
        for sym, board, title, cat in events:
            had = {self._cat_of(a) for a in self.storage.alerts_for_ticker(sym, since=day0, limit=30)}
            if cat in had:
                continue  # an alert of that kind already went out on it
            probe = {
                "kind": "price" if cat == "radar" else "options",
                "severity": "MEDIUM",
                "tickers": [sym],
                "edge": {"radar": True} if cat == "radar" else {},
            }
            for device in self.desk.watchers([sym]):
                key = f"{device}:{sym}:{board}"
                if key in st["keys"]:
                    continue
                st["keys"].append(key)
                prefs = self.desk.get(device).get("prefs") or {}
                if not prefs.get("watch_push", True) or not self.desk.allows(device, probe, now):
                    continue
                payload = {"id": f"bw-{sym}-{board}-{day}", "title": title, "body": "On your watchlist · no alert on it yet today",
                           "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"bw-{sym}-{board}", "ts": now,
                           "open": f"/app/#t={sym}"}  # fmt: skip
                if self.desk.hold(device, payload, now):
                    continue
                if await self._to_device(device, payload, urgency="normal"):
                    sent += 1
        self.storage.set_setting(f"boardwatch:{day}", json.dumps(st))
        return sent

    def price_at(self, sym: str, ts: float) -> float | None:
        """The stock's price at ``ts`` from the live tape's recent path (the last 30 minutes)."""
        path = self.live_quotes.path(sym, ts)
        if path is None:
            return None
        before = [p for t, p in path.hist if t <= ts + 5]
        return before[-1] if before else None

    def _rule_hit(self, r: dict[str, Any], sym: str, now: float, day: str) -> tuple[str, str, str] | None:
        """(key, title, body) when this rule holds for this stock now, else None."""
        kind, v = r["kind"], r["value"]
        if kind == "headline":
            for it in self.storage.items_for_ticker(sym, since=now - 600, limit=5, min_score=v):
                return (f"h:{r['id']}:{it['uid']}", f"📰 {sym}: {it['title'][:150]}",
                        f"{it['source']} · score {round(it.get('score') or 0)} · your rule: headlines on {r['symbol']} scoring {v:g}+")  # fmt: skip
            return None
        if kind == "news_move":
            q = self.price_of(sym, 900, now=now)
            if not q:
                return None
            for it in self.storage.items_for_ticker(sym, since=now - 3600, limit=5, min_score=20):
                ref = self._rule_refs.get((sym, it["uid"]))
                if ref is None:
                    ref = self.price_at(sym, it["detected"]) or q[1]
                    self._rule_refs[(sym, it["uid"])] = ref
                move = (q[1] / ref - 1.0) * 100.0 if ref else 0.0
                if abs(move) >= v:
                    return (f"nm:{r['id']}:{it['uid']}", f"⚡ {sym} {move:+.1f}% since the headline",
                            f"{it['title'][:160]} · {it['source']} · your rule: a headline, then a {v:g}% move")  # fmt: skip
            return None
        lt = (
            self.ignition.latest(sym, now)
            if self.ignition is not None and hasattr(self.ignition, "latest")
            else None
        )
        if kind == "volume":
            if lt and lt.get("rvol") and lt["rvol"] >= v:
                return (f"v:{r['id']}:{sym}:{day}", f"📊 {sym} at {lt['rvol']:.1f}× its normal volume",
                        f"{lt['chg']:+.1f}% on the day · your rule: {v:g}× volume")  # fmt: skip
            return None
        if kind == "vwap":
            if not lt or not lt.get("vwap"):
                return None
            side = "above" if lt["price"] > lt["vwap"] else "below"
            was = self._vwap_side.get(sym)
            self._vwap_side[sym] = side
            if was is not None and was != side and side == r["dir"]:
                return (f"w:{r['id']}:{sym}:{day}", f"〽️ {sym} crossed {side} VWAP",
                        f"${lt['price']:,.2f} vs VWAP ${lt['vwap']:,.2f} · {lt['chg']:+.1f}% on the day · your rule")  # fmt: skip
            return None
        if kind == "gap":
            if self.calendar.market_status(now).get("phase") != "pre-market":
                return None
            q = self.price_of(sym, 900, now=now)
            if q and q[2] is not None and abs(q[2]) >= v:
                return (
                    f"g:{r['id']}:{sym}:{day}",
                    f"🌅 {sym} gapping {q[2]:+.1f}% pre-market",
                    f"${q[1]:,.2f} · your rule: a {v:g}% gap",
                )
        return None

    async def check_rules(self, now: float | None = None) -> int:
        """Every phone's smart alerts (desk.py rules), each pushed to its phone once per headline or
        once a day."""
        now = now or time.time()
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        sent = 0
        for device, p in list(self.desk.profiles.items()):
            rules = p.get("rules") or []
            if not rules:
                continue
            fired = p.setdefault("rules_fired", [])
            changed = False
            for r in rules:
                syms = (p.get("watchlist") or [])[:60] if r["symbol"] == "*" else [r["symbol"]]
                for sym in syms:
                    hit = self._rule_hit(r, sym, now, day)
                    if hit is None or hit[0] in fired:
                        continue
                    fired.append(hit[0])
                    changed = True
                    payload = {"id": f"rule-{hit[0][:40]}", "title": hit[1], "body": hit[2], "severity": "HIGH", "kind": "system",
                               "tickers": [sym], "tag": f"rule-{r['id']}-{sym}", "ts": now, "open": f"/app/#t={sym}"}  # fmt: skip
                    self._broadcast(
                        "price_alert", {"device": device, "symbol": sym, "title": hit[1], "body": hit[2]}
                    )
                    if await self._to_device(device, payload):
                        sent += 1
            if changed:
                del fired[:-400]
                self.desk.save(device)
        if len(self._rule_refs) > 5000:
            self._rule_refs.clear()
        return sent

    async def check_portfolio_risk(self, now: float | None = None) -> int:
        """A phone's portfolio up or down its chosen % today: one push at that line and one at twice
        it, each way, per day; with the dollars and the holdings doing it."""
        from .dossier import portfolio

        now = now or time.time()
        if self.calendar.market_status(now).get("phase") == "closed":
            return 0
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        sent = 0
        for device, prof in list(self.desk.profiles.items()):
            line = (prof.get("prefs") or {}).get("port_alert") or 0
            if not line or not prof.get("holdings"):
                continue
            pf = portfolio(self, device, now)
            pct = pf.get("day_pct")
            if pct is None:
                continue
            for k in (2, 1):
                if abs(pct) < line * k:
                    continue
                way = "up" if pct > 0 else "down"
                if not self._once(f"portrisk:{day}:{device}:{way}:{k}"):
                    break
                who = ", ".join(
                    f"{c['symbol']} {'+' if c['day_pnl'] >= 0 else '−'}${abs(c['day_pnl']):,.0f}"
                    for c in pf["contributors"]
                )
                payload = {"id": f"pr-{day}-{way}-{k}", "title": f"{'📈' if way == 'up' else '📉'} Your portfolio {pct:+.1f}% today ({'+' if pf['day_pnl'] >= 0 else '−'}${abs(pf['day_pnl']):,.0f})",
                           "body": (f"Moving it: {who}" if who else "") + f" · your line: ±{line * k:g}%", "severity": "HIGH", "kind": "system",
                           "tickers": [c["symbol"] for c in pf["contributors"]], "tag": f"pr-{day}", "ts": now, "open": "/app/#a=desk"}  # fmt: skip
                if await self._to_device(device, payload):
                    sent += 1
                break
        return sent

    async def check_followups(self, now: float | None = None) -> int:
        """An hour after an alert on a stock a phone watches or holds: if it moved 2%+ since (from the
        alert's reference price), that phone gets how it played out. Once per alert per phone; for
        phones with follow-ups on."""
        now = now or time.time()
        sent = 0
        for a in self.storage.recent_alerts(200, since=now - 75 * 60):
            age = now - float(a.get("created", 0))
            refs = (a.get("edge") or {}).get("refs") or {}
            if a.get("kind") == "system" or not 55 * 60 <= age <= 75 * 60 or not refs:
                continue
            for sym, ref in list(refs.items())[:2]:
                q = self.price_of(sym, 900, now=now)
                if not q or not ref:
                    continue
                move = (q[1] / float(ref) - 1.0) * 100.0
                if abs(move) < 2:
                    continue
                for device in self.desk.watchers([sym]):
                    prefs = self.desk.get(device).get("prefs") or {}
                    if not prefs.get("followups") or self.desk.all_muted(device, {"tickers": [sym]}, now):
                        continue
                    if not self._once(f"followup:{a['id']}:{device}"):
                        continue
                    payload = {"id": f"fu-{a['id']}", "title": f"↪ {sym} {move:+.1f}% since the alert an hour ago", "body": a["title"][:200],
                               "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"fu-{a['id']}", "ts": now, "open": f"/app/#a={a['id']}"}  # fmt: skip
                    if await self._to_device(device, payload, urgency="normal"):
                        sent += 1
        return sent

    def save_memory(self, now: float | None = None) -> dict[str, Any] | None:
        """After 16:15 ET on a market day: keep the session's snapshot (market memory)."""
        from .dossier import memory_snapshot

        now = now or time.time()
        dt = datetime.fromtimestamp(now, ET)
        if (
            dt.weekday() >= 5
            or dt.date().isoformat() in self.calendar.holidays
            or (dt.hour, dt.minute) < (16, 15)
        ):
            return None
        key = f"memory:{dt.date().isoformat()}"
        if self.storage.get_setting(key):
            return None
        snap = memory_snapshot(self, now)
        if snap:
            self.storage.set_setting(key, json.dumps(snap))
        return snap

    def warm_earnings_week(self, now: float | None = None, top: int = 8) -> list[str]:
        """Once an hour: read the implied move and past reactions of the week's biggest reporters
        in the background, so the Calendar can show them without a wait."""
        from .dossier import earnings_week
        from .personal import earnings_reactions, implied_move

        now = now or time.time()
        if now - getattr(self, "_warmed_at", 0.0) < 3600:
            return []
        self._warmed_at = now
        syms = [r["symbol"] for d in earnings_week(self, now, min_cap=10e9)["days"] for r in d["rows"]][:top]

        async def warm() -> None:
            for s in syms:
                with contextlib.suppress(Exception):
                    await implied_move(self, s, now)
                    await earnings_reactions(self, s, now)

        if syms:
            self._spawn(warm())
        return syms

    def _tomorrow_due(self, now: float) -> bool:
        dt = datetime.fromtimestamp(now, ET)
        if dt.weekday() in (4, 5) or not ((20, 0) <= (dt.hour, dt.minute) < (20, 45)):  # Sun-Thu evenings
            return False
        return self.storage.get_setting("tomorrow_sent") != dt.date().isoformat()

    async def send_tomorrow(self, now: float | None = None) -> int:
        """20:00 ET, Sunday to Thursday: the next session, to every phone that turned it on."""
        from .dossier import tomorrow, tomorrow_push

        now = now or time.time()
        self.storage.set_setting("tomorrow_sent", datetime.fromtimestamp(now, ET).date().isoformat())
        if self.webpush is None or self.dispatcher.paused:
            return 0
        sent = 0
        for d in {s.device for s in self.webpush.subs.values() if s.device}:  # type: ignore[attr-defined]
            if not ((self.desk.profiles.get(d) or {}).get("prefs") or {}).get("tomorrow"):
                continue
            payload = tomorrow_push(tomorrow(self, d, now))
            if payload and await self._to_device(d, payload, urgency="normal"):
                sent += 1
        return sent

    def _close_due(self, now: float) -> bool:
        dt = datetime.fromtimestamp(now, ET)
        if (
            dt.weekday() >= 5
            or dt.date().isoformat() in self.calendar.holidays
            or not ((16, 15) <= (dt.hour, dt.minute) < (16, 50))
        ):
            return False
        return self.storage.get_setting("close_sent") != dt.date().isoformat()

    async def send_close(self, now: float | None = None) -> int:
        """16:15 ET: the close, to every phone that turned it on."""
        from .dossier import close_push, close_recap

        now = now or time.time()
        self.storage.set_setting("close_sent", datetime.fromtimestamp(now, ET).date().isoformat())
        if self.webpush is None or self.dispatcher.paused:
            return 0
        devices = [d for d in {s.device for s in self.webpush.subs.values() if s.device}  # type: ignore[attr-defined]
                   if ((self.desk.profiles.get(d) or {}).get("prefs") or {}).get("close_recap")]  # fmt: skip
        if not devices:
            return 0
        payload = close_push(close_recap(self, now))
        if payload is None:
            return 0
        sent = 0
        for d in devices:
            if await self._to_device(d, payload, urgency="normal"):
                sent += 1
        return sent

    def pre_move(self, symbols: list[str], ts: float, window_s: float = 900.0) -> dict[str, float]:
        """Each stock's move over the ``window_s`` before ``ts``, from the live tape's recent path
        (only when a price from at least 10 minutes earlier is in it)."""
        out = {}
        for sym in symbols:
            path = self.live_quotes.path(sym, ts)
            if path is None or len(path.hist) < 2:
                continue
            upto = [(t, p) for t, p in path.hist if t <= ts + 5]
            before = [(t, p) for t, p in upto if t <= ts - window_s]
            ref = before[-1] if before else (upto[0] if upto and ts - upto[0][0] >= 600 else None)
            if ref and upto and ref[1]:
                out[sym] = round((upto[-1][1] / ref[1] - 1.0) * 100.0, 2)
        return out

    async def _forward(self, device: str, payload: dict[str, Any]) -> bool:
        """This phone's own forward (Discord webhook, Telegram bot or ntfy topic): the same alert,
        as text. Only to those three services' own hosts."""
        from .desk import forward_request

        fw = ((self.desk.profiles.get(device) or {}).get("prefs") or {}).get("forward")
        req = forward_request(fw, payload) if fw else None
        if req is None:
            return False
        url, kw = req
        try:
            await self.http.post(url, timeout_s=15, **kw)
            return True
        except Exception as exc:  # noqa: BLE001 - a dead webhook must not stop the phone's push
            log.info("forward for %s failed: %s", device[:8], exc)
            return False

    def mark_day(self, now: float | None = None, every_s: float = 300.0) -> None:
        """Keep SPY's and QQQ's price every 5 minutes today, for the day-in-one-screen timeline."""
        now = now or time.time()
        day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for sym in ("SPY", "QQQ"):
            marks = [m for m in self.day_marks.get(sym, []) if m[0] >= day0]
            q = self.price_of(sym, 900, now=now)
            if q and q[1] and (not marks or q[0] - marks[-1][0] >= every_s):
                marks.append((q[0], q[1]))
            self.day_marks[sym] = marks

    def _week_due(self, now: float) -> bool:
        dt = datetime.fromtimestamp(now, ET)
        if dt.weekday() != 4 or not ((16, 30) <= (dt.hour, dt.minute) < (18, 0)):  # Friday after the close
            return False
        return self.storage.get_setting("week_sent") != dt.date().isoformat()

    async def send_week_cards(self, now: float | None = None) -> int:
        from .personal import week_card, week_push

        now = now or time.time()
        self.storage.set_setting("week_sent", datetime.fromtimestamp(now, ET).date().isoformat())
        if self.webpush is None or self.dispatcher.paused:
            return 0
        sent = 0
        for device in {s.device for s in self.webpush.subs.values() if s.device}:  # type: ignore[attr-defined]
            prof = self.desk.profiles.get(device)
            if not prof or not (prof.get("prefs") or {}).get("week", True):
                continue
            card = week_card(self, device, now)
            payload = week_push(card) if card else None
            if payload and await self._to_device(device, payload, urgency="normal"):
                sent += 1
        return sent

    async def send_digests(self, now: float | None = None) -> int:
        """Phones in digest mode: what was held since the last one, in one push."""
        now = now or time.time()
        sent = 0
        rank = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}
        for device, held in self.desk.due_digests(now):
            top = sorted(held, key=lambda a: (-rank.get(a["severity"], 0), -a["ts"]))
            lines = [f"• {a['title']}" for a in top[:4]] + (
                [f"+ {len(top) - 4} more in the app"] if len(top) > 4 else []
            )
            n_high = sum(1 for a in held if a["severity"] == "HIGH")
            mins = (self.desk.get(device).get("prefs") or {}).get("digest") or 0
            payload = {"id": f"dg-{device[:8]}-{int(now)}", "title": f"📬 {len(held)} alert{'s' if len(held) > 1 else ''}"
                       + (f" ({n_high} HIGH)" if n_high else "") + (f" · last {mins} min" if mins else " held back in a rush"),
                       "body": "\n".join(lines)[:600], "severity": "MEDIUM", "kind": "system", "tickers": [],
                       "tag": f"dg-{device[:8]}", "ts": now, "open": "/app/"}  # fmt: skip
            if await self._to_device(device, payload, urgency="normal"):
                sent += 1
        return sent

    def _once(self, key: str) -> bool:
        """True the first time ``key`` is seen (kept in the settings table: survives restarts)."""
        if self.storage.get_setting(key):
            return False
        self.storage.set_setting(key, "1")
        return True

    async def send_earnings_reminders(self, now: float | None = None) -> int:
        """A watched stock reporting today: a push before it does (08:30-09:25 ET for a report
        before the open, 15:30-15:55 for one after the close, 09:00-09:25 when the time isn't
        known), with what options price and its usual move when those are known."""
        from .personal import earnings_reactions, implied_move

        now = now or time.time()
        book = getattr(self.trends, "earnings", None) if self.trends is not None else None
        if book is None or self.webpush is None:
            return 0
        dt = datetime.fromtimestamp(now, ET)
        if dt.weekday() >= 5 or dt.date().isoformat() in self.calendar.holidays:
            return 0
        hm = (dt.hour, dt.minute)
        sent = 0
        devices = {s.device for s in self.webpush.subs.values() if s.device}  # type: ignore[attr-defined]
        for device in devices:
            p = self.desk.profiles.get(device)
            if not p or not (p.get("prefs") or {}).get("earn_remind", True):
                continue
            held = p.get("holdings") or {}
            for sym in dict.fromkeys([*(p.get("watchlist") or []), *held]):
                e = book.lookup(sym, dt.date())
                if not e or e["in_days"] != 0:
                    continue
                when = str(e.get("when") or "").lower()
                window = (
                    ((15, 30), (15, 56))
                    if "after" in when
                    else ((8, 30), (9, 26))
                    if "before" in when
                    else ((9, 0), (9, 26))
                )
                if not (window[0] <= hm < window[1]) or not self._once(f"remind:{dt.date()}:{device}:{sym}"):
                    continue
                bits = []
                nxt = None
                with contextlib.suppress(Exception):
                    im = await implied_move(self, sym, now)
                    nxt = next((r for r in im.get("rows") or [] if r.get("earnings")), None)
                    if nxt:
                        bits.append(f"options price ±{nxt['move_pct']:.1f}% by {nxt['expiry'][5:]}")
                with contextlib.suppress(Exception):
                    er = await earnings_reactions(self, sym, now)
                    if er.get("avg_abs_move"):
                        bits.append(
                            f"it moved ±{er['avg_abs_move']:.1f}% on its last {len(er['rows'])} reports"
                        )
                shares = (held.get(sym) or {}).get("shares") or 0
                q = self.price_of(sym, 86400, now=now) if shares else None
                if q and nxt:
                    bits.append(
                        f"about ±${shares * q[1] * nxt['move_pct'] / 100:,.0f} on your {shares:g} shares"
                    )
                title = f"📅 {sym} reports {'after the close' if 'after' in when else 'before the open' if 'before' in when else 'today'}"
                payload = {"id": f"er-{sym}-{dt.date()}", "title": title, "body": (" · ".join(bits) or "On your watchlist")[:300],
                           "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"er-{sym}", "ts": now,
                           "open": f"/app/#t={sym}"}  # fmt: skip
                if await self._to_device(device, payload, urgency="normal"):
                    sent += 1
        return sent

    async def send_setups(self, now: float | None = None) -> int:
        """09:25-09:29 ET on market days: each phone that asked for them gets today's setups in one
        push (plans.todays_setups), once."""
        from .plans import setups_push, todays_setups

        now = now or time.time()
        if self.webpush is None:
            return 0
        dt = datetime.fromtimestamp(now, ET)
        if (
            dt.weekday() >= 5
            or dt.date().isoformat() in self.calendar.holidays
            or not ((9, 25) <= (dt.hour, dt.minute) < (9, 30))
        ):
            return 0
        sent = 0
        for dev, p in list(self.desk.profiles.items()):
            if not (p.get("prefs") or {}).get("setups") or not self._once(f"setups:{dev}:{dt.date()}"):
                continue
            msg = setups_push(todays_setups(self, dev, now))
            if msg is None:
                continue
            payload = {"id": f"su-{dt.date()}-{dev[:6]}", **msg, "severity": "HIGH", "kind": "system", "tickers": [],
                       "tag": f"su-{dt.date()}", "ts": now, "open": "/app/#a=brief"}  # fmt: skip
            if await self._to_device(dev, payload):
                sent += 1
        return sent

    async def send_open_plans(self, now: float | None = None) -> int:
        """09:20-09:29 ET on market days: each phone with a watchlist gets the open in one push:
        its stocks moving pre-market, the top gappers with a reason, today's timed events."""
        from .dossier import gaps
        from .opening import premarket

        now = now or time.time()
        if self.webpush is None:
            return 0
        dt = datetime.fromtimestamp(now, ET)
        if (
            dt.weekday() >= 5
            or dt.date().isoformat() in self.calendar.holidays
            or not ((9, 20) <= (dt.hour, dt.minute) < (9, 30))
        ):
            return 0
        board = gaps(self, now)
        movers = [r for r in board["up"] + board["down"] if r["why"]]
        movers.sort(key=lambda r: -abs(r["chg"]))
        events = [
            e
            for e in self.calendar.upcoming(days=1, now=now)
            if e.get("in_days") == 0 and e.get("time") and (e.get("impact") or 1) >= 2
        ]
        sent = 0
        for device in {s.device for s in self.webpush.subs.values() if s.device}:  # type: ignore[attr-defined]
            p = self.desk.profiles.get(device)
            if not p or not p.get("watchlist") or not (p.get("prefs") or {}).get("open_plan", True):
                continue
            if not self._once(f"openplan:{dt.date()}:{device}"):
                continue
            mine = []
            for sym in p["watchlist"]:
                q = self.price_of(sym, 3600, now=now)
                if q and q[2] is not None and abs(q[2]) >= 1:
                    mine.append((sym, q[2]))
            mine.sort(key=lambda x: -abs(x[1]))
            lines = []
            if mine:
                lines.append("★ " + ", ".join(f"{s} {c:+.1f}%" for s, c in mine[:4]))
            for r in movers[:2]:
                lines.append(f"{r['symbol']} {r['chg']:+.1f}%: {r['why']['title'][:90]}")
            for e in events[:2]:
                lines.append(f"{e['time']} ET: {e['title'][:80]}")
            # into the open: the phone's stocks with news since the close, and how gaps like theirs went
            gappers = [x for x in premarket(self, now, device)["rows"] if x["mine"] and x["record"]][:2]
            for k, r in enumerate(gappers):
                lines.insert(
                    k + (1 if mine else 0), f"{r['symbol']} {r['gap']:+.1f}% on its news · {r['line']}"
                )
            if not lines:
                continue
            payload = {"id": f"op-{dt.date()}", "title": "🔔 The open in 10 minutes", "body": "\n".join(lines)[:600],
                       "severity": "MEDIUM", "kind": "system", "tickers": [s for s, _ in mine[:4]], "tag": "openplan",
                       "ts": now, "open": "/app/"}  # fmt: skip
            if await self._to_device(device, payload, urgency="normal"):
                sent += 1
        return sent

    async def auto_why(self, now: float | None = None) -> list[str]:
        """At 11:00 and 14:30 ET, a watched stock moving 4%+ with no headline behind it gets the
        EDGAR + news hunt (once a day per stock), so its page, the report card and the tape can
        say why."""
        now = now or time.time()
        if self.why is None:
            return []
        dt = datetime.fromtimestamp(now, ET)
        if dt.weekday() >= 5 or not any(
            (h, m) <= (dt.hour, dt.minute) < (h, m + 5) for h, m in ((11, 0), (14, 30))
        ):
            return []
        watched = {s for p in self.desk.profiles.values() for s in p.get("watchlist") or []}
        done = []
        for sym in sorted(watched):
            q = self.price_of(sym, 900, now=now)
            if not q or q[2] is None or abs(q[2]) < 4 or self.find_catalysts([sym], now, overnight=True):
                continue
            if not self._once(f"autowhy:{dt.date()}:{sym}"):
                continue
            with contextlib.suppress(Exception):
                await self.find_why(sym, now)
                done.append(sym)
        return done

    async def find_why(self, sym: str, now: float | None = None) -> dict[str, Any]:
        """Look for the reason a stock is moving, now: today's filings and news for it (EDGAR and a
        news search), each scored like any other item. Rate-limited per stock."""
        now = now or time.time()
        if self.why is None:
            return {"symbol": sym, "available": False, "why": "The why-lookup is off on this server."}
        if not self.why.allow(sym, now):
            return {"symbol": sym, "available": True, "busy": True, "found": [],
                    "why": "Looked for this stock a moment ago (or the hourly budget is used up); try again shortly."}  # fmt: skip
        li = self.universe.get(sym)
        since = Calendar.last_close(now, self.calendar.holidays) - 6 * 3600
        items = await self.why.hunt(sym, (li.name if li is not None else "") or sym, since)
        found = []
        for it in items:
            an = await self.on_item(it)
            if an is None:
                an = self.scorer.score(it)
            found.append({"title": it.title, "source": it.source, "url": it.url, "score": round(an.score),
                          "published": it.published, "detected": it.detected})  # fmt: skip
        found.sort(key=lambda x: -x["score"])
        return {
            "symbol": sym,
            "available": True,
            "found": found[:8],
            "searched": ["EDGAR filings", "news search"],
        }

    @staticmethod
    def _cat_of(a: dict[str, Any]) -> str:
        from .desk import alert_category

        return alert_category(a)

    async def check_confluence(self, now: float | None = None) -> list[Alert]:
        """When independent kinds of evidence line up on one side of a stock (dossier.py), say so
        once per stock, side and day: HIGH (pushed) at a combined weight of 7+, else MEDIUM (in the
        app, and pushed to phones watching the stock). At most 8 pushed a day."""
        from .dossier import CONF_HIGH_WEIGHT, CONF_KIND_LABEL, confluence_candidates

        now = now or time.time()
        if self.calendar.market_status(now).get("phase") == "closed":
            return []
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        st = self.confluence_day
        if st["day"] != day:
            saved = self.storage.get_setting(f"confluence:{day}")
            st.update(json.loads(saved) if saved else {"day": day, "fired": [], "pushed": 0})
        made = []
        for c in confluence_candidates(self, now):
            key = f"{c['symbol']}:{c['side']}"
            if key in st["fired"]:
                continue
            st["fired"].append(key)
            high = c["weight"] >= CONF_HIGH_WEIGHT and st["pushed"] < 8
            st["pushed"] += 1 if high else 0
            li = self.universe.get(c["symbol"])
            arrow = "▲" if c["side"] == "up" else "▼"
            kinds = [CONF_KIND_LABEL.get(k, k) for k in c["kinds"]]
            title = f"{arrow} {c['symbol']}: {', '.join(kinds[:-1])} and {kinds[-1]} line up" + (
                f" ({li.name[:40]})" if li else ""
            )
            body = "\n".join(f"{'▲' if x['side'] == 'up' else '▼'} {x['label']}" for x in c["signals"][:4])
            q = self.price_of(c["symbol"], 1800, now=now)
            alert = Alert(
                kind="confluence", severity=Severity.HIGH if high else Severity.MEDIUM, title=title, body=body,
                tickers=[c["symbol"]], created=now,
                edge={"confluence": {k: c[k] for k in ("side", "weight", "against", "kinds", "signals")},
                      **({"refs": {c["symbol"]: q[1]}} if q else {})},
            )  # fmt: skip
            await self.publish(alert)
            made.append(alert)
        self.storage.set_setting(f"confluence:{day}", json.dumps(st))
        return made

    def refresh_base_rates(self, now: float | None = None) -> None:
        from .dossier import track_record

        self.base_rates = {r["kind"]: r for r in track_record(self, 60, now)["rows"]}
        from .catalysts import catalyst_rates

        self.catalyst_rates = catalyst_rates(self, 180, now)
        from .catalysts import regime_rates

        self.regime_rates = regime_rates(self, 180, now)
        from .firms import firm_rates

        self.firm_rates = firm_rates(self, 365, now)
        from .liveodds import h1_table

        self.h1_rates = h1_table(self, 180, now)
        from .heat import heat_rates

        self.heat_rates = heat_rates(self, 180, now)
        from .flowrec import flow_rates

        self.flow_rates = flow_rates(self, 180, now)
        from .persona import persona_rates

        self.persona_rates = persona_rates(self, 365, now)
        from .newsclock import clock_rates

        self.clock_rates = clock_rates(self, 180, now)
        from .opening import open_rates

        self.open_rates = open_rates(self, 365, now)
        from .insiders import insider_rates

        self.insider_rates = insider_rates(self, 730, now)
        from .drift import drift_rates

        self.drift_rates = drift_rates(self, 365, now)
        from .coverage import coverage_rates

        self.coverage_rates = coverage_rates(self, 180, now)
        from .thecall import call_rates

        self.call_rates = call_rates(self, 180, now)
        from .climate import climate_rates

        self.climate_rates = climate_rates(self, 180, now)
        from .overnight import overnight_rates

        self.overnight_rates = overnight_rates(self, 180, now)
        from .thecall import replay as call_replay

        self.call_replay = call_replay(self, 180, now)
        from .macrodays import macro_rates

        self.macro_rates = macro_rates(self, 365, now)
        from .volnews import vol_rates

        self.vol_rates = vol_rates(self, 180, now)
        from .thecall import muted_facts

        self.call_muted = muted_facts(self.call_replay)
        from .peerlearn import pair_rates

        self.pair_rates = pair_rates(self, 180, now)
        from .waits import wait_rates

        self.wait_rates = wait_rates(self, 180, now)
        from .floatshort import float_rates

        self.float_rates = float_rates(self, 180, now)
        from .dilution import dilution_rates

        self.dilution_rates = dilution_rates(self, 365, now)
        from .catchup import catchup_rates

        self.catchup_rates = catchup_rates(self, 180, now)
        from .haltrec import halt_rates

        self.halt_rates = halt_rates(self, 180, now)
        from .secondleg import leg_rates

        self.leg_rates = leg_rates(self, 180, now)
        from .macrotape import theme_rates

        self.theme_rates = theme_rates(self, 365, now)
        from .volconfirm import rvol_rates

        self.rvol_rates = rvol_rates(self, 180, now)
        from .callnow import turn_rates

        self.turn_rates = turn_rates(self, 180, now)
        from .runup import runup_rates

        self.runup_rates = runup_rates(self)
        from .entries import entry_rates

        self.entry_rates = entry_rates(self, 180, now)
        from .tapeday import tape_rates

        self.tape_rates = tape_rates(self, 365, now)
        evs, dates = [], set()
        for a in self.storage.recent_alerts(3000, since=(now or time.time()) - 10 * 86400):
            ev = ((a.get("edge") or {}).get("odds") or {}).get("ev")
            if ev is not None:
                evs.append(float(ev))
                dates.add(datetime.fromtimestamp(a["created"], ET).date())
        self.ev_hist = (evs, max(1, len(dates)))
        if (now or time.time()) - self._odds_at >= 3600:
            from .odds import fit_odds

            self._odds_at = now or time.time()
            self.odds = fit_odds(self, 180, now, prev=self.odds)

    def _eod_due(self, now: float) -> bool:
        dt = datetime.fromtimestamp(now, ET)
        if dt.weekday() >= 5 or dt.date().isoformat() in self.calendar.holidays:
            return False
        if not ((16, 5) <= (dt.hour, dt.minute) < (17, 30)):  # a missed card isn't sent at night
            return False
        return self.storage.get_setting("eod_sent") != dt.date().isoformat()

    async def send_report_cards(self, now: float | None = None) -> int:
        """16:05 ET: each phone with a watchlist or tracked calls gets its day in one push."""
        from .personal import report_card, report_push

        now = now or time.time()
        self.storage.set_setting("eod_sent", datetime.fromtimestamp(now, ET).date().isoformat())
        if self.webpush is None or self.dispatcher.paused:
            return 0
        sent = 0
        for device in {s.device for s in self.webpush.subs.values() if s.device}:  # type: ignore[attr-defined]
            prof = self.desk.profiles.get(device)
            if not prof or not (prof.get("prefs") or {}).get("eod", True):
                continue
            card = report_card(self, device, now)
            payload = report_push(card) if card else None
            if payload and await self._to_device(device, payload, urgency="normal"):
                sent += 1
        return sent

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
            with contextlib.suppress(Exception):
                self.save_scorecard(now)

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
