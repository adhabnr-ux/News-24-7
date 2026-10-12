"""Live data for the options tape: the stock's price right now, and a real-time tripwire on the
contracts most likely to explode.

Why this exists (Oct 12 2026): BIIB gapped up at the open and its $247.5 calls went from
$0.06 to $0.80 in minutes. The tape alerted after the run was over, for two reasons:

* the free Alpaca options feed ("indicative") delivers *trades* 15 minutes late, and the spike
  test was driven by the last trade;
* the stock price on the chain came from the Nasdaq screener universe, which still showed
  Friday's close ($225.71, +3.33%) after the open, so the scheduler did not know BIIB was moving
  and the model value of yesterday's close was computed from the wrong day.

The fixes live here:

* ``LiveQuotes``: one store of each stock's latest price, from the fastest source available
  (Alpaca's real-time stock snapshots when keys are set, else the radar's Yahoo sweep, which
  reads the ~600 largest companies every minute). The options feed reads its stock price and its
  movers from here instead of the 10-minute universe.
* ``OptionStream``: Alpaca's options websocket (``/v1beta1/{feed}``, msgpack). Indicative
  *quotes* are live even on the free plan (only trades are delayed), so the stream watches the
  quotes of up to ``stream_max_quotes`` contracts (the free plan allows 200): the cheap,
  near-dated contracts of the companies moving or in the news. When a contract's bid crosses a
  rung of its previous close (4x, 6x, 11x, 21x...), the company's chain is re-read at once and the
  usual tests decide the alert: seconds, not a rotation and a 15-minute trade delay.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- stock quotes


@dataclass
class LiveQuote:
    symbol: str
    price: float
    prev_close: float | None
    ts: float  # when the price was true (epoch), not when we read it
    source: str

    @property
    def change_pct(self) -> float | None:
        if self.prev_close and self.prev_close > 0:
            return (self.price / self.prev_close - 1.0) * 100.0
        return None


@dataclass
class DayPath:
    """One stock's session so far, from the live prices seen (and the day's bar when a source
    gives it): where it opened, its high and low and when, and the last ``JUMP_KEEP_S`` of prices
    for the jump detector."""

    day: str
    high: float
    high_ts: float
    low: float
    low_ts: float
    open: float | None = None
    hist: list[tuple[float, float]] = field(default_factory=list)  # (epoch, price), oldest first


JUMP_KEEP_S = 1800.0  # price history kept per symbol for the jump detector


class LiveQuotes:
    """The latest stock price per symbol, newest source wins; plus each symbol's path today
    (high/low and when) and a 30-minute history, so a sudden jump is seen between sweeps."""

    def __init__(self) -> None:
        self.quotes: dict[str, LiveQuote] = {}
        self.paths: dict[str, DayPath] = {}
        self.updates = 0

    def update(
        self, symbol: str, price: float | None, prev_close: float | None, ts: float, source: str
    ) -> None:
        if not symbol or not price or price <= 0:
            return
        sym = symbol.upper()
        cur = self.quotes.get(sym)
        if cur is not None and cur.ts > ts:
            return  # never let an older reading replace a newer one
        self.quotes[sym] = LiveQuote(
            sym, float(price), prev_close or (cur.prev_close if cur else None), ts, source
        )
        self.updates += 1
        self._track(sym, float(price), ts)

    def _track(self, sym: str, price: float, ts: float) -> None:
        day = datetime.fromtimestamp(ts, ET).date().isoformat()
        p = self.paths.get(sym)
        if p is None or p.day != day:
            p = self.paths[sym] = DayPath(day, price, ts, price, ts)
        if price > p.high:
            p.high, p.high_ts = price, ts
        if price < p.low:
            p.low, p.low_ts = price, ts
        if not p.hist or ts > p.hist[-1][0]:
            p.hist.append((ts, price))
        elif ts == p.hist[-1][0]:
            p.hist[-1] = (ts, price)
        cut = ts - JUMP_KEEP_S
        while p.hist and p.hist[0][0] < cut:
            p.hist.pop(0)

    def seed(self, symbol: str, points: Iterable[tuple[float, float]]) -> None:
        """Today's earlier prices (e.g. Yahoo's 5-minute spark), so the day's high and low are
        known from the open even when the first live read comes late."""
        sym = (symbol or "").upper()
        for ts, price in sorted(points or []):
            if price and price > 0:
                self._track(sym, float(price), float(ts))

    def set_range(
        self, symbol: str, high: float | None, low: float | None, open_: float | None, ts: float
    ) -> None:
        """The day's bar from a source that has it (Alpaca's daily bar): widens the path's high and
        low to the whole session. The time of a bar high is unknown, so it keeps the observed time
        when the observed high matches it."""
        sym = (symbol or "").upper()
        p = self.paths.get(sym)
        day = datetime.fromtimestamp(ts, ET).date().isoformat()
        if p is None or p.day != day:
            if not high or not low:
                return
            p = self.paths[sym] = DayPath(day, float(high), 0.0, float(low), 0.0)
        if high and high > p.high:
            p.high, p.high_ts = float(high), 0.0
        if low and 0 < low < p.low:
            p.low, p.low_ts = float(low), 0.0
        if open_ and p.open is None:
            p.open = float(open_)

    def path(self, symbol: str, now: float | None = None) -> DayPath | None:
        p = self.paths.get((symbol or "").upper())
        if p is None:
            return None
        if p.day != datetime.fromtimestamp(now or time.time(), ET).date().isoformat():
            return None
        return p

    def jump(
        self, symbol: str, window_s: float, now: float | None = None
    ) -> tuple[float, float, float] | None:
        """The biggest move into the latest price within the last ``window_s``: (% move, price it
        started from, when). Up moves are measured from the window's low, down moves from its high;
        the larger one wins. None without at least two prices in the window."""
        p = self.path(symbol, now)
        if p is None or len(p.hist) < 2:
            return None
        now = now or time.time()
        last_ts, last = p.hist[-1]
        if now - last_ts > window_s:
            return None
        pts = [(t, x) for t, x in p.hist if t >= last_ts - window_s]
        if len(pts) < 2:
            return None
        lo_t, lo = min(pts[:-1], key=lambda r: r[1])
        hi_t, hi = max(pts[:-1], key=lambda r: r[1])
        up = (last / lo - 1.0) * 100.0 if lo > 0 else 0.0
        down = (last / hi - 1.0) * 100.0 if hi > 0 else 0.0
        if up >= -down:
            return (up, lo, lo_t)
        return (down, hi, hi_t)

    def get(self, symbol: str, max_age_s: float, now: float | None = None) -> LiveQuote | None:
        q = self.quotes.get((symbol or "").upper())
        if q is None:
            return None
        if (now or time.time()) - q.ts > max_age_s:
            return None
        return q

    def movers(self, min_pct: float, max_age_s: float, now: float | None = None) -> list[str]:
        """Symbols moving at least ``min_pct`` either way on a fresh quote, biggest first."""
        now = now or time.time()
        rows = [
            q
            for q in self.quotes.values()
            if now - q.ts <= max_age_s and q.change_pct is not None and abs(q.change_pct) >= min_pct
        ]
        rows.sort(key=lambda q: -abs(q.change_pct or 0.0))
        return [q.symbol for q in rows]

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        fresh = [q for q in self.quotes.values() if now - q.ts <= 180]
        by: dict[str, int] = {}
        for q in fresh:
            by[q.source] = by.get(q.source, 0) + 1
        return {"symbols": len(self.quotes), "fresh_3m": len(fresh), "by_source": by, "updates": self.updates}


def _rfc3339(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    text = v.strip().replace("Z", "+00:00")
    if "." in text:  # nanoseconds: Python reads at most microseconds
        head, _, tail = text.partition(".")
        n = 0
        while n < len(tail) and tail[n].isdigit():
            n += 1
        text = f"{head}.{tail[:n][:6].ljust(6, '0')}{tail[n:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_stock_snapshots(doc: Any, today: date) -> dict[str, tuple[float, float | None, float]]:
    """Alpaca ``/v2/stocks/snapshots`` -> {symbol: (price, previous close, epoch of the price)}.

    The price is the latest trade. The previous close is the bar before today's daily bar; before
    today's first trade (the daily bar is still the last session's) it is that bar's close."""
    out: dict[str, tuple[float, float | None, float]] = {}
    if not isinstance(doc, dict):
        return out
    rows = doc.get("snapshots") if isinstance(doc.get("snapshots"), dict) else doc
    for sym, snap in rows.items():
        if not isinstance(snap, dict):
            continue
        trade = snap.get("latestTrade") or {}
        price = trade.get("p")
        when = _rfc3339(trade.get("t"))
        if not price or when is None:
            continue
        bar = snap.get("dailyBar") or {}
        prev_bar = snap.get("prevDailyBar") or {}
        bar_day = _rfc3339(bar.get("t"))
        if bar_day is not None and bar_day.astimezone(ET).date() == today:
            prev = prev_bar.get("c")
        else:
            prev = bar.get("c")
        try:
            out[str(sym).upper()] = (float(price), float(prev) if prev else None, when.timestamp())
        except (TypeError, ValueError):
            continue
    return out


def parse_stock_ranges(doc: Any, today: date) -> dict[str, tuple[float | None, float | None, float | None]]:
    """Alpaca ``/v2/stocks/snapshots`` -> {symbol: (day high, day low, open)} from today's daily
    bar (none before today's first trade)."""
    out: dict[str, tuple[float | None, float | None, float | None]] = {}
    if not isinstance(doc, dict):
        return out
    rows = doc.get("snapshots") if isinstance(doc.get("snapshots"), dict) else doc
    for sym, snap in rows.items():
        if not isinstance(snap, dict):
            continue
        bar = snap.get("dailyBar") or {}
        bar_day = _rfc3339(bar.get("t"))
        if bar_day is None or bar_day.astimezone(ET).date() != today:
            continue
        try:
            out[str(sym).upper()] = tuple(  # type: ignore[assignment]
                float(bar[k]) if bar.get(k) else None for k in ("h", "l", "o")
            )
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- the option stream

STREAM_RUNGS = (3.0, 5.0, 10.0, 20.0, 50.0)  # bid / previous close - 1: +300%, +500%, +1,000% ...


@dataclass
class Watch:
    """One contract on the tripwire."""

    occ: str
    underlying: str
    prev_close: float
    rank: float = 0.0  # higher is watched first
    fired: float = 0.0  # highest rung already triggered today
    bid: float | None = None
    bid_ts: float = 0.0
    fair: float | None = None  # value at the last chain read (neighbor-strike implied vol)
    ask: float | None = None
    misprint_at: float = 0.0  # when this contract last tripped the misprint check
    follow: bool = False  # an alerted contract being followed: every quote goes to ``on_quote``


@dataclass
class StreamStats:
    connected: bool = False
    connects: int = 0
    messages: int = 0
    quotes: int = 0
    triggers: int = 0
    last_message: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    quote_lag_s: list[float] = field(default_factory=list)  # quote timestamp vs arrival, recent

    def to_dict(self) -> dict[str, Any]:
        lag = sorted(self.quote_lag_s)
        return {
            "connected": self.connected,
            "connects": self.connects,
            "messages": self.messages,
            "quotes": self.quotes,
            "triggers": self.triggers,
            "last_message": self.last_message or None,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at or None,
            "quote_lag_median_s": round(lag[len(lag) // 2], 2) if lag else None,
        }


def _ts_seconds(v: Any) -> float | None:
    """A msgpack Timestamp, a datetime, an RFC 3339 string or epoch seconds/nanoseconds."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.timestamp()
    if hasattr(v, "to_unix"):  # msgpack.Timestamp
        try:
            return float(v.to_unix())
        except Exception:  # noqa: BLE001
            return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1e9 if x > 1e14 else x
    if isinstance(v, str):
        d = _rfc3339(v)
        return d.timestamp() if d else None
    return None


class OptionStream:
    """Alpaca's options websocket as a tripwire: quotes of the watched contracts in real time,
    ``on_trigger(underlying, occ, bid, pct)`` when a bid crosses a rung of its previous close."""

    def __init__(
        self,
        url: str,
        key: str,
        secret: str,
        on_trigger: Callable[[str, str, float, float], Any],
        max_quotes: int = 190,
        is_open: Callable[[], bool] | None = None,
        session: Callable[[], Any] | None = None,
        on_misprint: Callable[[Watch, float | None, float | None, float], Any] | None = None,
        misprint_ratio: float = 0.5,
        rich_ratio: float = 2.0,
        on_quote: Callable[[Watch, float | None, float | None, float], Any] | None = None,
    ) -> None:
        self.on_quote = on_quote  # (watch, bid, ask, quote epoch): every quote of a watched contract
        self.on_misprint = on_misprint  # (watch, bid, ask, quote epoch): a quote far from fair value
        self.misprint_ratio = misprint_ratio
        self.rich_ratio = rich_ratio
        self.misprint_trips = 0
        self.url = url
        self.key = key
        self.secret = secret
        self.on_trigger = on_trigger
        self.max_quotes = max_quotes
        self.is_open = is_open or (lambda: True)
        self._session = session
        self.watch: dict[str, Watch] = {}
        self.subscribed: set[str] = set()
        self.stats = StreamStats()
        self._ws: Any = None
        self._dirty = asyncio.Event()
        self._day = ""

    # ------------------------------------------------------------------ what to watch

    def set_candidates(self, underlying: str, rows: Iterable[Watch]) -> None:
        """Replace one company's candidates (keeping today's fired rungs), then re-pick the
        ``max_quotes`` best across all companies."""
        old = {w.occ: w for w in self.watch.values() if w.underlying == underlying}
        for occ in old:
            self.watch.pop(occ, None)
        for w in rows:
            prev = old.get(w.occ)
            if prev is not None:
                w.fired = prev.fired
                w.bid, w.bid_ts = prev.bid, prev.bid_ts
                w.misprint_at = prev.misprint_at
            self.watch[w.occ] = w
        self._trim()
        self._dirty.set()

    def drop(self, underlying: str) -> None:
        for occ in [o for o, w in self.watch.items() if w.underlying == underlying]:
            del self.watch[occ]
        self._dirty.set()

    def _trim(self) -> None:
        if len(self.watch) <= self.max_quotes:
            return
        keep = sorted(self.watch.values(), key=lambda w: -w.rank)[: self.max_quotes]
        self.watch = {w.occ: w for w in keep}

    def wanted(self) -> set[str]:
        return set(self.watch)

    # ------------------------------------------------------------------ messages

    def handle(self, msgs: Any, now: float | None = None) -> list[tuple[str, str, float, float]]:
        """Process decoded messages; returns the triggers fired (and calls ``on_trigger`` for
        each in ``run``)."""
        now = now or time.time()
        if isinstance(msgs, dict):
            msgs = [msgs]
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        if day != self._day:  # a new session: every contract may fire again
            self._day = day
            for w in self.watch.values():
                w.fired = 0.0
        fired: list[tuple[str, str, float, float]] = []
        for m in msgs or []:
            if not isinstance(m, dict):
                continue
            self.stats.messages += 1
            self.stats.last_message = now
            kind = m.get("T")
            if kind == "error":
                code = m.get("code")
                if code == 405:  # symbol limit exceeded: watch fewer
                    self.max_quotes = max(10, int(self.max_quotes * 0.75))
                    self._trim()
                    self._dirty.set()
                raise ConnectionError(f"Alpaca options stream error {code}: {m.get('msg')}")
            if kind == "subscription":
                self.subscribed = set(m.get("quotes") or [])
                continue
            if kind != "q":
                continue
            self.stats.quotes += 1
            occ = str(m.get("S") or "")
            w = self.watch.get(occ)
            if w is None:
                continue
            bid, ask = m.get("bp"), m.get("ap")
            ts = _ts_seconds(m.get("t")) or now
            lag = now - ts
            if -5 < lag < 3600:
                self.stats.quote_lag_s.append(lag)
                del self.stats.quote_lag_s[:-200]
            w.ask = float(ask) if ask else None
            self._misprint_check(w, float(bid) if bid else None, w.ask, ts, now)
            if self.on_quote is not None and w.follow:
                try:
                    self.on_quote(w, float(bid) if bid else None, w.ask, ts)
                except Exception:  # noqa: BLE001 - one bad follow-up must not drop the socket
                    log.exception("options stream follow-up failed")
            if not bid:
                continue
            w.bid, w.bid_ts = float(bid), ts
            if w.prev_close <= 0:
                continue
            pct = (float(bid) / w.prev_close - 1.0) * 100.0
            rung = max((r for r in STREAM_RUNGS if pct >= r * 100.0), default=0.0)
            if rung > w.fired:
                w.fired = rung
                self.stats.triggers += 1
                fired.append((w.underlying, occ, float(bid), pct))
        return fired

    def _misprint_check(self, w: Watch, bid: float | None, ask: float | None, ts: float, now: float) -> None:
        """A stub offer (or bid) can vanish in seconds, so the check happens on the quote itself:
        an ask at ``misprint_ratio`` of the contract's fair value or less, or a bid at
        ``rich_ratio`` times it or more, hands the quote to ``on_misprint`` (once a minute per
        contract), which judges it against the live chain."""
        if self.on_misprint is None or not w.fair or now - w.misprint_at < 60:
            return
        cheap = ask is not None and 0 < ask <= self.misprint_ratio * w.fair
        rich = bid is not None and bid >= self.rich_ratio * w.fair
        if not (cheap or rich):
            return
        w.misprint_at = now
        self.misprint_trips += 1
        try:
            self.on_misprint(w, bid, ask, ts)
        except Exception:  # noqa: BLE001 - one bad check must not drop the socket
            log.exception("options stream misprint check failed")

    # ------------------------------------------------------------------ the socket

    async def _send(self, ws: Any, obj: dict[str, Any]) -> None:
        import msgpack

        await ws.send_bytes(msgpack.packb(obj, use_bin_type=True))

    async def _sync(self, ws: Any) -> None:
        want = self.wanted()
        add, remove = sorted(want - self.subscribed), sorted(self.subscribed - want)
        if remove:
            await self._send(ws, {"action": "unsubscribe", "quotes": remove})
            self.subscribed -= set(remove)
        if add:
            await self._send(ws, {"action": "subscribe", "quotes": add})
            self.subscribed |= set(add)

    @staticmethod
    def decode(data: bytes | str) -> Any:
        if isinstance(data, str):
            import json

            return json.loads(data)
        import msgpack

        return msgpack.unpackb(data, raw=False, timestamp=3)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            import msgpack  # noqa: F401
        except ImportError:
            self.stats.last_error = "the msgpack package is not installed (pip install msgpack)"
            log.warning("options stream off: %s", self.stats.last_error)
            return
        attempt = 0
        while not stop.is_set():
            if not self.is_open() or not self.watch:
                self.stats.connected = False
                await _wait(stop, self._dirty, 30.0)
                self._dirty.clear()
                continue
            try:
                await self._session_run(stop)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect with backoff
                self.stats.connected = False
                self.stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
                self.stats.last_error_at = time.time()
                attempt += 1
                delay = min(300.0, 2 ** min(attempt, 8)) * random.uniform(0.8, 1.2)
                text = str(exc)
                if any(code in text for code in ("401", "402", "409", "410", "412")):
                    delay = max(delay, 600.0)  # keys or plan: don't hammer
                elif "406" in text:
                    delay = max(delay, 120.0)  # another connection holds the slot
                log.warning("options stream: %s; reconnecting in %.0fs", exc, delay)
                await _wait(stop, None, delay)
        self.stats.connected = False

    async def _session_run(self, stop: asyncio.Event) -> None:
        session = self._session() if self._session else None
        if session is None:
            raise RuntimeError("no HTTP session for the options stream")
        async with session.ws_connect(
            self.url, heartbeat=30, headers={"Content-Type": "application/msgpack"}, max_msg_size=8 << 20
        ) as ws:
            self._ws = ws
            await self._send(ws, {"action": "auth", "key": self.key, "secret": self.secret})
            self.subscribed = set()
            self.stats.connects += 1
            authed = False
            while not stop.is_set():
                if not self.is_open():
                    break  # the session closed: free the connection until the next open
                if self._dirty.is_set() and authed:
                    self._dirty.clear()
                    await self._sync(ws)
                try:
                    msg = await ws.receive(timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError("Alpaca options stream closed")
                if msg.type not in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                    continue
                decoded = self.decode(msg.data)
                rows = decoded if isinstance(decoded, list) else [decoded]
                if not authed and any(
                    isinstance(r, dict) and r.get("T") == "success" and r.get("msg") == "authenticated"
                    for r in rows
                ):
                    authed = True
                    self.stats.connected = True
                    log.info("options stream: authenticated (%s)", self.url)
                    await self._sync(ws)
                for underlying, occ, bid, pct in self.handle(rows):
                    try:
                        res = self.on_trigger(underlying, occ, bid, pct)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:  # noqa: BLE001 - one bad trigger must not drop the socket
                        log.exception("options stream trigger failed")
            self._ws = None
            self.stats.connected = False


async def _wait(stop: asyncio.Event, other: asyncio.Event | None, timeout: float) -> None:
    waits = [asyncio.ensure_future(stop.wait())]
    if other is not None:
        waits.append(asyncio.ensure_future(other.wait()))
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


def pick_watch(
    contracts: Iterable[Any],
    underlying: str,
    price: float | None,
    today: date,
    per_symbol: int = 8,
    weight: float = 1.0,
    max_dte: int = 21,
    max_prev: float = 2.0,
) -> list[Watch]:
    """The contracts of one company most likely to multiply: cheap (previous close $0.03 to
    ``max_prev``), near-dated (0 to ``max_dte`` days), strikes within 25% of the stock, ranked by
    how far they have already run today, then how close to expiry and to the money. ``weight``
    lifts a whole company (news and big movers first)."""
    rows: list[Watch] = []
    for c in contracts:
        prev = getattr(c, "prev_close", None)
        if not prev or prev < 0.03 or prev > max_prev:
            continue
        dte = (c.expiry - today).days
        if dte < 0 or dte > max_dte:
            continue
        if price and c.strike and abs(c.strike / price - 1.0) > 0.25:
            continue
        bid = getattr(c, "bid", None) or 0.0
        running = bid / prev if prev else 0.0
        near = 1.0 - min(abs(c.strike / price - 1.0), 0.25) / 0.25 if price else 0.5
        rank = weight * (10.0 * min(running, 10.0) + 3.0 * (1.0 - dte / (max_dte + 1)) + 2.0 * near)
        rows.append(Watch(c.symbol, underlying, float(prev), rank=rank))
    rows.sort(key=lambda w: -w.rank)
    return rows[:per_symbol]
