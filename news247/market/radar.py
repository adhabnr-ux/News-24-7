"""The radar: small caps ripping before there is a headline.

Small-cap news often reaches the tape before it reaches any feed: an 8-K accepted at 16:01, a
deal whispered to a few desks, a Reuters scoop on a government stake. The price is the first
public trace. The radar scans the whole small/mid-cap market every minute and flags a name the
moment it breaks out, with how big the company is, how much money is actually trading (a move
nobody trades is not a move) and how unusual that volume is. When a headline then lands, the
alert says the radar had it first; when one already exists, the radar alert links it.

Two feeds, both free and keyless:

* ``yahoo`` — Yahoo Finance's ``small_cap_gainers`` screener (every minute, includes pre- and
  after-hours prices, 3-month average volume for relative volume). Yahoo sometimes asks for a
  cookie + "crumb"; the feed fetches one and retries.
* ``nasdaq`` — the Nasdaq screener snapshot that also builds the universe (every ~10 minutes
  in session; covers losers too). Used on its own when ``radar_provider: nasdaq``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..config import SmallCapConfig
from ..http import HttpClient, HTTPError
from ..sources.base import SourceHealth, _sleep_or_stop
from .prices import SPARK_URL, parse_spark
from .universe import CACHE_NAME, SCREENER_HEADERS, Listing, Universe, band, fmt_cap

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

YAHOO_SCREENER = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_HEADERS = {
    "User-Agent": SCREENER_HEADERS["User-Agent"],
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
# rungs as multiples of the band's threshold: 20% -> 20, 35, 50, 75, 100, 150, 200, 300, 500, 1000
LADDER = (1.0, 1.75, 2.5, 3.75, 5.0, 7.5, 10.0, 15.0, 25.0, 50.0)
BIG_CAP = 2e9  # from here up a company is liquid by definition: no dollar-volume test
JUMP_COOLDOWN_S = 20 * 60
JUMP_LOOKBACK_S = (45, 6 * 60)  # compare with a price 45 s – 6 min old


@dataclass
class Quote:
    symbol: str
    name: str = ""
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    avg_volume: float | None = None  # 3-month average daily volume
    market_cap: float | None = None
    session: str = "regular"  # regular | pre | post
    source: str = ""

    @property
    def dollar_volume(self) -> float | None:
        return self.price * self.volume if self.price and self.volume else None

    @property
    def rvol(self) -> float | None:
        return self.volume / self.avg_volume if self.volume and self.avg_volume else None


@dataclass
class RadarHit:
    quote: Quote
    kind: str  # "day" (crossed a ladder level) | "jump" (fast move between scans)
    level: int  # ladder level crossed (day) or 0
    jump_pct: float | None = None
    jump_window_s: int = 0
    ref_price: float | None = None
    severity: str = "MEDIUM"
    flags: list[str] = field(default_factory=list)
    listing: Listing | None = None
    detected: float = field(default_factory=time.time)

    @property
    def symbol(self) -> str:
        return self.quote.symbol

    @property
    def direction(self) -> str:
        pct = self.jump_pct if self.kind == "jump" and self.jump_pct is not None else self.quote.change_pct
        return "up" if (pct or 0) >= 0 else "down"

    def to_dict(self) -> dict[str, Any]:
        q = self.quote
        cap = q.market_cap or (self.listing.market_cap if self.listing else None)
        return {
            "symbol": q.symbol,
            "name": q.name or (self.listing.name if self.listing else ""),
            "price": q.price,
            "prev_close": q.prev_close,
            "change_pct": q.change_pct,
            "session": q.session,
            "kind": self.kind,
            "level": self.level,
            "jump_pct": self.jump_pct,
            "jump_window_s": self.jump_window_s,
            "market_cap": cap,
            "cap": fmt_cap(cap),
            "band": band(cap),
            "dollar_volume": q.dollar_volume,
            "rvol": round(q.rvol, 1) if q.rvol else None,
            "severity": self.severity,
            "flags": list(self.flags),
            "sector": self.listing.sector if self.listing else "",
            "industry": self.listing.industry if self.listing else "",
            "detected": self.detected,
            "source": q.source,
        }


# --------------------------------------------------------------------------- parsing


def _f(v: Any) -> float | None:
    if isinstance(v, dict):  # formatted=true shape: {"raw": 1.2, "fmt": "1.20"}
        v = v.get("raw")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_yahoo_screener(data: dict[str, Any]) -> list[Quote]:
    """finance.result[0].quotes[] -> quotes. Pre/after-hours prices win when the market is in
    that session, so a 7 a.m. FDA approval shows up at 7 a.m., not at the 9:30 open."""
    results = ((data.get("finance") or {}).get("result")) or []
    out: list[Quote] = []
    for res in results:
        for q in res.get("quotes") or []:
            sym = str(q.get("symbol") or "").upper()
            if not sym or q.get("quoteType", "EQUITY") != "EQUITY":
                continue
            state = str(q.get("marketState") or "").upper()
            price, pct, sess = (
                _f(q.get("regularMarketPrice")),
                _f(q.get("regularMarketChangePercent")),
                "regular",
            )
            prev = _f(q.get("regularMarketPreviousClose"))
            if state.startswith("PRE") and _f(q.get("preMarketPrice")):
                # pre-market moves are measured from yesterday's close (= the regular price now)
                price, pct, sess = _f(q.get("preMarketPrice")), _f(q.get("preMarketChangePercent")), "pre"
                prev = _f(q.get("regularMarketPrice")) or prev
            elif state.startswith("POST") and _f(q.get("postMarketPrice")):
                post_pct = _f(q.get("postMarketChangePercent"))
                reg_pct = _f(q.get("regularMarketChangePercent")) or 0.0
                price, sess = _f(q.get("postMarketPrice")), "post"
                # after hours: total move since yesterday's close = regular day + post session
                pct = (
                    ((1 + reg_pct / 100) * (1 + (post_pct or 0) / 100) - 1) * 100
                    if post_pct is not None
                    else pct
                )
            out.append(
                Quote(
                    symbol=sym.replace(".", "-"),
                    name=str(q.get("longName") or q.get("shortName") or ""),
                    price=price,
                    prev_close=prev,
                    change_pct=pct,
                    volume=_f(q.get("regularMarketVolume")),
                    avg_volume=_f(q.get("averageDailyVolume3Month")) or _f(q.get("averageDailyVolume10Day")),
                    market_cap=_f(q.get("marketCap")),
                    session=sess,
                    source="yahoo",
                )
            )
    return out


def quotes_from_listings(listings: list[Listing]) -> list[Quote]:
    out = []
    for li in listings:
        if li.price is None or li.change_pct is None or not li.common:
            continue
        prev = li.price / (1 + li.change_pct / 100) if li.change_pct > -100 else None
        out.append(
            Quote(
                symbol=li.symbol,
                name=li.name,
                price=li.price,
                prev_close=prev,
                change_pct=li.change_pct,
                volume=li.volume,
                market_cap=li.market_cap,
                source="nasdaq",
            )
        )
    return out


# --------------------------------------------------------------------------- the scanner


@dataclass
class _State:
    day: str = ""
    fired_up: int = 0
    fired_down: int = 0
    last_jump: float = 0.0
    prices: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=24))


class MoversRadar:
    """Turns market snapshots into breakout hits. Pure logic: no I/O, fully testable."""

    def __init__(
        self, cfg: SmallCapConfig, universe: Universe | None = None, state_path: Path | None = None
    ) -> None:
        self.cfg = cfg
        self.universe = universe
        self.state: dict[str, _State] = {}
        self.board: dict[str, dict[str, Any]] = {}  # what the app's radar list shows
        self.pushes: dict[str, int] = {}  # "<ET date>:<small|big>" -> radar pushes sent
        self.scans = 0
        self.hits_total = 0
        # what already fired today survives a restart, so a restart neither repeats an alert nor
        # (as a silent start-up baseline would) misses a stock that was already moving
        self.state_path = state_path
        self._load()

    # ------------------------------------------------------------------ per-size rules

    def rules(self, cap: float) -> tuple[float, float, float, str]:
        """(day-move threshold %, fast-jump threshold %, push threshold %, push class) by size.
        A 20% day is routine for a micro cap and a once-a-year event for a $75B company."""
        c = self.cfg
        if cap < c.radar_max_cap:
            return c.radar_min_pct, c.radar_jump_pct, c.radar_push_pct, "small"
        if cap < 10e9:
            m = c.radar_mid_min_pct
        elif cap < 200e9:
            m = c.radar_large_min_pct
        else:
            m = c.radar_mega_min_pct
        return m, m / 2, m * 1.5, "big"

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            doc = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        day = doc.get("day", "")
        for sym, (up, down) in (doc.get("fired") or {}).items():
            self.state[sym] = _State(day=day, fired_up=float(up), fired_down=float(down))
        self.pushes = {k: int(v) for k, v in (doc.get("pushes") or {}).items()}

    def save(self, now: float | None = None) -> None:
        if not self.state_path:
            return
        day = self._day(now or time.time())
        fired = {
            s: [st.fired_up, st.fired_down]
            for s, st in self.state.items()
            if st.day == day and (st.fired_up or st.fired_down)
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {
                        "day": day,
                        "fired": fired,
                        "pushes": {k: v for k, v in self.pushes.items() if k.startswith(day)},
                    }
                )
            )
        except OSError as exc:
            log.warning("radar state not saved: %s", exc)

    @staticmethod
    def _day(now: float) -> str:
        return datetime.fromtimestamp(now, ET).date().isoformat()

    def eligible(self, q: Quote, li: Listing | None) -> tuple[bool, list[str]]:
        flags: list[str] = []
        cap = q.market_cap or (li.market_cap if li else None)
        if cap is None or cap < self.cfg.min_market_cap:
            return False, flags
        if q.price is None or q.price < self.cfg.min_price or q.change_pct is None:
            return False, flags
        dv = q.dollar_volume
        if cap < BIG_CAP and (dv is None or dv < self.cfg.radar_min_dollar_volume):
            return False, flags
        if li is not None and li.pump_profile:
            flags.append(li.pump_profile)
        if cap < 50e6:
            flags.append("nano cap")
        return True, flags

    def scan(self, quotes: list[Quote], now: float | None = None, provider: str = "") -> list[RadarHit]:
        now = now or time.time()
        day = self._day(now)
        self.scans += 1
        hits: list[RadarHit] = []
        for q in quotes:
            li = self.universe.get(q.symbol) if self.universe is not None else None
            if li is not None and not li.common:
                continue
            if not q.name and li is not None:
                q.name = li.name
            if q.market_cap is None and li is not None:
                q.market_cap = li.market_cap
            ok, flags = self.eligible(q, li)
            st = self.state.setdefault(q.symbol, _State())
            if st.day != day:
                st.day, st.fired_up, st.fired_down = day, 0, 0
                st.prices.clear()
            jump, ref, window = self._jump(st, q, now)
            st.prices.append((now, q.price or 0.0))
            if not ok:
                continue
            pct = q.change_pct or 0.0
            cap = q.market_cap or (li.market_cap if li else 0) or 0
            min_pct, jump_pct, push_pct, klass = self.rules(cap)
            # the push threshold is a rung too: bigger companies push at 1.5x, between the 1x and
            # 1.75x rungs, so a stock that climbs to +9.3% (AMT, Oct 9 2026) would otherwise never push
            rungs = sorted({min_pct * m for m in LADDER} | {push_pct})
            level = max((r for r in rungs if abs(pct) >= r), default=0)
            entry = self.board.get(q.symbol, {})
            self.board[q.symbol] = {
                **entry,
                **RadarHit(
                    q, "day", level, listing=li, flags=flags, detected=entry.get("detected", now)
                ).to_dict(),
                "updated": now,
            }
            if abs(pct) < min_pct and not (jump is not None and abs(jump) >= jump_pct):
                if abs(pct) < min_pct * 0.5:
                    self.board.pop(q.symbol, None)  # faded: off the board
                continue
            hit: RadarHit | None = None
            fired = st.fired_up if pct >= 0 else st.fired_down
            if level >= min_pct and level > fired + 1e-9:
                if pct >= 0:
                    st.fired_up = level
                else:
                    st.fired_down = level
                hit = RadarHit(q, "day", level, ref_price=q.prev_close, listing=li, flags=flags, detected=now)
            if jump is not None and abs(jump) >= jump_pct and now - st.last_jump >= JUMP_COOLDOWN_S:
                st.last_jump = now
                if hit is None:
                    hit = RadarHit(q, "jump", 0, listing=li, flags=flags, detected=now)
                hit.jump_pct, hit.jump_window_s, hit.ref_price = round(jump, 2), window, ref
            if hit is None:
                continue
            hit.severity = self._severity(hit, day, push_pct, jump_pct, klass, cap)
            self.board[q.symbol]["flagged"] = now
            self.board[q.symbol]["severity"] = hit.severity
            hits.append(hit)
        self.hits_total += len(hits)
        self._trim(now)
        if hits:
            self.save(now)
        return hits

    def _jump(self, st: _State, q: Quote, now: float) -> tuple[float | None, float | None, int]:
        if not q.price:
            return None, None, 0
        lo, hi = JUMP_LOOKBACK_S
        for ts, p in st.prices:  # oldest first: the biggest window inside the lookback
            age = now - ts
            if lo <= age <= hi and p > 0:
                return (q.price / p - 1.0) * 100.0, p, int(age)
        return None, None, 0

    def _severity(
        self, hit: RadarHit, day: str, push_pct: float, jump_pct: float, klass: str, cap: float
    ) -> str:
        q = hit.quote
        big = abs(q.change_pct or 0) >= push_pct or (
            hit.jump_pct is not None and abs(hit.jump_pct) >= 2 * jump_pct
        )
        liquid = cap >= BIG_CAP or (q.dollar_volume or 0) >= 5e6 or (q.rvol or 0) >= 5
        key = f"{day}:{klass}"
        if big and liquid and not hit.flags and self.pushes.get(key, 0) < self.cfg.radar_daily_pushes:
            self.pushes[key] = self.pushes.get(key, 0) + 1
            return "HIGH"
        return "MEDIUM"

    def _trim(self, now: float) -> None:
        for sym, e in list(self.board.items()):
            if now - e.get("updated", now) > 3 * 3600:
                del self.board[sym]
        if len(self.state) > 20000:
            for sym in list(self.state)[:5000]:
                del self.state[sym]

    def snapshot(self, limit: int = 40) -> list[dict[str, Any]]:
        rows = sorted(self.board.values(), key=lambda e: abs(e.get("change_pct") or 0), reverse=True)
        return rows[:limit]


# --------------------------------------------------------------------------- the feed


class SmallCapFeed:
    """Keeps the universe fresh and runs the radar on a schedule (see module docstring)."""

    def __init__(
        self,
        cfg: SmallCapConfig,
        http: HttpClient,
        universe: Universe,
        data_dir: Path | None,
        radar: MoversRadar | None = None,
        market_phase: Any = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.universe = universe
        self.cache = (data_dir / CACHE_NAME) if data_dir else None
        self.radar = radar
        self.market_phase = market_phase  # callable(now) -> "open" | "pre-market" | "after-hours" | "closed"
        self.health = SourceHealth()
        self.radar_health = SourceHealth()
        self._crumb: str | None = None
        self._swept = 0.0
        self._saved_at = 0.0
        self._universe_tried = 0.0

    @staticmethod
    def load_cached(data_dir: Path | None) -> Universe | None:
        if data_dir is None:
            return None
        return Universe.load(data_dir / CACHE_NAME)

    # ------------------------------------------------------------------ universe

    async def refresh_universe(self) -> list[Listing] | None:
        self._universe_tried = time.time()
        self.health.polls += 1
        t0 = time.monotonic()
        try:
            resp = await self.http.get(self.cfg.universe_url, headers=SCREENER_HEADERS, timeout_s=45)
            fresh = Universe.from_screener(resp.json())
        except (
            HTTPError,
            aiohttp.ClientError,
            ValueError,
            KeyError,
            TypeError,
            asyncio.TimeoutError,
            OSError,
        ) as exc:
            self._error(self.health, exc)
            log.warning("universe refresh failed (%s); keeping %d cached listings", exc, len(self.universe))
            return None
        listings = list(fresh.by_symbol.values())
        if len(listings) < 1000:  # a truncated or blocked answer must not wipe a good universe
            self._error(self.health, ValueError(f"only {len(listings)} listings"))
            return None
        self.universe.replace(listings, loaded_at=time.time(), source="nasdaq")
        self.health.last_ok = time.time()
        self.health.consecutive_errors = 0
        self.health.items = len(listings)
        self.health.last_fetch_ms = (time.monotonic() - t0) * 1000
        if self.cache and time.time() - self._saved_at > 3600:
            try:
                await asyncio.to_thread(self.universe.save, self.cache)
                self._saved_at = time.time()
            except OSError as exc:
                log.warning("universe cache not saved: %s", exc)
        log.info("universe: %d listings (Nasdaq screener)", len(listings))
        return listings

    # ------------------------------------------------------------------ yahoo radar feed

    async def fetch_yahoo(self, scr_id: str = "small_cap_gainers", count: int = 100) -> list[Quote]:
        params = {
            "scrIds": scr_id,
            "count": str(count),
            "formatted": "false",
            "lang": "en-US",
            "region": "US",
        }
        for attempt in range(2):
            if self._crumb:
                params["crumb"] = self._crumb
            try:
                resp = await self.http.get(YAHOO_SCREENER, headers=YAHOO_HEADERS, params=params)
                return parse_yahoo_screener(resp.json())
            except HTTPError as exc:
                if exc.status in (401, 403) and attempt == 0:
                    await self._get_crumb()
                    continue
                raise
        return []

    async def _get_crumb(self) -> None:
        """Yahoo's cookie dance: fc.yahoo.com sets the A3 cookie (answering 404 is normal),
        getcrumb then returns the token that must ride along with the cookie."""
        try:
            await self.http.get(YAHOO_COOKIE_URL, headers=YAHOO_HEADERS)
        except HTTPError:
            pass
        try:
            resp = await self.http.get(YAHOO_CRUMB_URL, headers=YAHOO_HEADERS)
            crumb = resp.text().strip()
            self._crumb = crumb if crumb and len(crumb) < 64 and "<" not in crumb else None
        except HTTPError as exc:
            log.debug("yahoo crumb unavailable: %s", exc)
            self._crumb = None

    # ------------------------------------------------------------------ loop

    def _phase(self, now: float) -> str:
        if self.market_phase is None:
            return "open"
        try:
            return str(self.market_phase(now))
        except Exception:  # noqa: BLE001 - a calendar bug must not stop the radar
            return "open"

    async def run(self, on_hits: Any, stop: asyncio.Event) -> None:
        """Refresh the universe when stale (every ``refresh_hours``, every 10 min in session) and
        scan for breakouts every ``radar_seconds`` from 04:00 to 20:00 ET on trading days."""
        while not stop.is_set():
            now = time.time()
            phase = self._phase(now)
            active = phase in ("open", "pre-market", "after-hours")
            universe_due = self.universe.age > self.cfg.refresh_hours * 3600 or (
                phase == "open" and now - self._universe_tried > 600
            )
            if universe_due and now - self._universe_tried > 300:
                listings = await self.refresh_universe()
                if listings and self.radar is not None and phase == "open" and self.cfg.radar:
                    await self._emit(
                        on_hits, self.radar.scan(quotes_from_listings(listings), provider="nasdaq")
                    )
            if active and self.radar is not None and self.cfg.radar and self.cfg.radar_provider == "yahoo":
                await self._yahoo_scan(on_hits, phase)
            sweep_due = self.cfg.sweep_size > 0 and now - self._swept >= self.cfg.sweep_seconds
            if active and self.radar is not None and self.cfg.radar and sweep_due:
                self._swept = now
                await self._sweep(on_hits, phase)
            await _sleep_or_stop(
                stop, self.cfg.radar_seconds if active else min(900.0, self.cfg.radar_seconds * 10)
            )

    async def _yahoo_scan(self, on_hits: Any, phase: str = "open") -> None:
        """Small-cap gainers every scan (pre/after hours included); in the regular session also
        the all-size day gainers and losers (Yahoo lists those by the regular session only)."""
        assert self.radar is not None
        self.radar_health.polls += 1
        t0 = time.monotonic()
        screens = ["small_cap_gainers"] + (["day_gainers", "day_losers"] if phase == "open" else [])
        quotes: list[Quote] = []
        try:
            for scr in screens:
                quotes.extend(await self.fetch_yahoo(scr))
        except (
            HTTPError,
            aiohttp.ClientError,
            ValueError,
            KeyError,
            TypeError,
            asyncio.TimeoutError,
            OSError,
        ) as exc:
            self._error(self.radar_health, exc)
            return
        self.radar_health.last_ok = time.time()
        self.radar_health.consecutive_errors = 0
        self.radar_health.items += len(quotes)
        self.radar_health.last_fetch_ms = (time.monotonic() - t0) * 1000
        await self._emit(on_hits, self.radar.scan(quotes, provider="yahoo"))

    async def sweep_quotes(self, phase: str = "open") -> list[Quote]:
        """Pre-market, regular and after-hours prices for the largest ``sweep_size`` companies
        (Yahoo spark, 20 symbols a request). The screeners above only rank the regular session,
        so this is what sees a $75B company gap up 9% before the open."""
        big = sorted(
            (li for li in self.universe.by_symbol.values() if li.common and (li.market_cap or 0) >= BIG_CAP),
            key=lambda li: -(li.market_cap or 0),
        )[: self.cfg.sweep_size]
        session = {"pre-market": "pre", "after-hours": "post"}.get(phase, "regular")
        out: list[Quote] = []
        for i in range(0, len(big), 20):
            chunk = big[i : i + 20]
            resp = await self.http.get(
                SPARK_URL,
                params={
                    "symbols": ",".join(li.symbol for li in chunk),
                    "range": "1d",
                    "interval": "5m",
                    "includePrePost": "true",
                },
                headers=YAHOO_HEADERS,
            )
            snaps = parse_spark(resp.json())
            for li in chunk:
                snap = snaps.get(li.symbol)
                if not snap:
                    continue
                pts = snap.get("points") or []
                price = pts[-1][1] if pts else snap.get("price")
                prev = snap.get("prev_close")
                if not price or not prev:
                    continue
                out.append(
                    Quote(
                        li.symbol,
                        name=li.name,
                        price=float(price),
                        prev_close=float(prev),
                        change_pct=(float(price) / float(prev) - 1.0) * 100.0,
                        market_cap=li.market_cap,
                        session=session,
                        source="sweep",
                    )
                )
        return out

    async def _sweep(self, on_hits: Any, phase: str) -> None:
        assert self.radar is not None
        try:
            quotes = await self.sweep_quotes(phase)
        except (
            HTTPError,
            aiohttp.ClientError,
            ValueError,
            KeyError,
            TypeError,
            asyncio.TimeoutError,
            OSError,
        ) as exc:
            self._error(self.radar_health, exc)
            return
        if not quotes:
            return  # no large caps known yet (universe still loading)
        self.radar_health.items += len(quotes)
        await self._emit(on_hits, self.radar.scan(quotes, provider="sweep"))

    @staticmethod
    async def _emit(on_hits: Any, hits: list[RadarHit]) -> None:
        if hits:
            await on_hits(hits)

    @staticmethod
    def _error(h: SourceHealth, exc: BaseException) -> None:
        h.errors += 1
        h.consecutive_errors += 1
        h.last_error = f"{type(exc).__name__}: {exc}"[:300]
        h.last_error_at = time.time()

    def status(self) -> dict[str, Any]:
        return {
            "listings": len(self.universe),
            "universe_age_s": None if self.universe.age == float("inf") else round(self.universe.age),
            "universe_source": self.universe.source,
            "universe": self.health.to_dict(),
            "radar": {
                "enabled": bool(self.cfg.radar and self.radar is not None),
                "provider": self.cfg.radar_provider,
                "scans": self.radar.scans if self.radar else 0,
                "hits": self.radar.hits_total if self.radar else 0,
                "on_board": len(self.radar.board) if self.radar else 0,
                **self.radar_health.to_dict(),
            },
        }
