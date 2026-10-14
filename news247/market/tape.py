"""The whole market's live tape: price, the day's volume and range for every stock, every
``tape_seconds`` in the regular session. It feeds the ignition scanner (market/ignition.py) and the
shared live-price store the options tape reads.

Two providers:

* **alpaca** (keys set): ``/v2/stocks/snapshots``, ``tape_batch`` symbols a request: the latest
  trade, today's bar (high, low, volume, VWAP) and yesterday's bar (close, volume). On the free
  plan the feed is IEX, a few percent of all trading; every volume comparison is made within that
  one feed (today on IEX against a normal day on IEX), so the ratios stay meaningful. Requests go
  through the options tape's pacer when there is one, so the two share Alpaca's 200 a minute.
  Names Alpaca has no quote for (over-the-counter Fannie Mae and Freddie Mac) are read from Yahoo.
* **yahoo** (no keys): Yahoo's quote endpoint (all-exchange volume, the 10-day average volume,
  the day's range; no VWAP), at most once a minute.

A normal day's volume on the Alpaca feed is an average of the previous sessions' volume on the
same feed, kept in ``tape_volume.json``: it starts from yesterday's bar and moves 25% toward each
new session's volume, once a day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..config import SmallCapConfig
from ..http import HttpClient, HTTPError
from ..sources.base import SourceHealth, _sleep_or_stop
from .ignition import IgnitionHit, IgnitionScanner, Tick
from .live import _rfc3339
from .universe import Universe

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
EMA_ALPHA = 0.25
_ERRORS = (HTTPError, aiohttp.ClientError, ValueError, KeyError, TypeError, asyncio.TimeoutError, OSError)


def _f(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("raw")
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # NaN


def parse_tape_snapshots(doc: Any, today: date) -> dict[str, dict[str, Any]]:
    """Alpaca ``/v2/stocks/snapshots`` -> {symbol: {price, ts, prev_close, volume, prev_volume,
    high, low, open, vwap}}. Before today's first trade the daily bar is the last session's: the
    volume today is then 0 and that bar is "yesterday"."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(doc, dict):
        return out
    rows = doc.get("snapshots") if isinstance(doc.get("snapshots"), dict) else doc
    for sym, snap in rows.items():
        if not isinstance(snap, dict):
            continue
        trade = snap.get("latestTrade") or {}
        price = _f(trade.get("p"))
        when = _rfc3339(trade.get("t"))
        if not price or when is None:
            continue
        bar = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        bar_day = _rfc3339(bar.get("t"))
        if bar_day is not None and bar_day.astimezone(ET).date() == today:
            row = {
                "prev_close": _f(prev.get("c")),
                "prev_volume": _f(prev.get("v")),
                "volume": _f(bar.get("v")) or 0.0,
                "high": _f(bar.get("h")),
                "low": _f(bar.get("l")),
                "open": _f(bar.get("o")),
                "vwap": _f(bar.get("vw")),
            }
        else:
            row = {"prev_close": _f(bar.get("c")), "prev_volume": _f(bar.get("v")), "volume": 0.0,
                   "high": None, "low": None, "open": None, "vwap": None}  # fmt: skip
        row.update({"price": price, "ts": when.timestamp()})
        out[str(sym).upper().replace(".", "-")] = row
    return out


def parse_yahoo_quotes(doc: Any) -> dict[str, dict[str, Any]]:
    """Yahoo ``/v7/finance/quote`` -> the same rows (no VWAP; volume is all exchanges)."""
    out: dict[str, dict[str, Any]] = {}
    res = ((doc or {}).get("quoteResponse") or {}).get("result") or []
    for q in res:
        sym = str(q.get("symbol") or "").upper()
        price = _f(q.get("regularMarketPrice"))
        ts = _f(q.get("regularMarketTime"))
        if not sym or not price or not ts:
            continue
        out[sym.replace(".", "-")] = {
            "price": price,
            "ts": ts,
            "prev_close": _f(q.get("regularMarketPreviousClose")),
            "volume": _f(q.get("regularMarketVolume")),
            "avg_volume": _f(q.get("averageDailyVolume10Day")) or _f(q.get("averageDailyVolume3Month")),
            "high": _f(q.get("regularMarketDayHigh")),
            "low": _f(q.get("regularMarketDayLow")),
            "open": _f(q.get("regularMarketOpen")),
            "vwap": None,
            "market_cap": _f(q.get("marketCap")),
            "name": str(q.get("longName") or q.get("shortName") or ""),
        }
    return out


class VolumeBase:
    """A normal day's volume per symbol on one feed: an average of past sessions, updated once a
    day from yesterday's bar."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.rows: dict[str, tuple[float, str]] = {}  # symbol -> (average, day of the last update)
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            doc = json.loads(self.path.read_text())
            self.rows = {k: (float(v[0]), str(v[1])) for k, v in doc.items()}
        except (OSError, ValueError, TypeError, IndexError):
            self.rows = {}

    def update(self, symbol: str, prev_volume: float | None, day: str) -> float | None:
        cur = self.rows.get(symbol)
        if prev_volume and prev_volume > 0 and (cur is None or cur[1] != day):
            avg = prev_volume if cur is None else cur[0] + EMA_ALPHA * (prev_volume - cur[0])
            self.rows[symbol] = (avg, day)
            self._dirty = True
            return avg
        return cur[0] if cur else None

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: [round(v[0]), v[1]] for k, v in self.rows.items()}))
            tmp.replace(self.path)
            self._dirty = False
        except OSError as exc:
            log.warning("tape volume averages not saved: %s", exc)


class TapeFeed:
    def __init__(
        self,
        cfg: SmallCapConfig,
        http: HttpClient,
        universe: Universe,
        scanner: IgnitionScanner,
        live: Any = None,
        market_phase: Callable[[float], str] | None = None,
        alpaca: Any = None,
        pace: Callable[[float], Any] | None = None,
        yahoo_json: Callable[..., Any] | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.universe = universe
        self.scanner = scanner
        self.live = live
        self.market_phase = market_phase
        self.alpaca = alpaca if cfg.tape_provider in ("auto", "alpaca") else None
        self.provider = "alpaca" if self.alpaca is not None else "yahoo"
        self._pace_fn = pace
        self._next_slot = 0.0
        self.yahoo_json = yahoo_json
        self.base = VolumeBase(data_dir / "tape_volume.json" if data_dir else None)
        self.health = SourceHealth()
        self.reads = 0
        self.ticks = 0
        self.last_sweep_s: float | None = None

    @property
    def interval(self) -> float:
        return self.cfg.tape_seconds if self.provider == "alpaca" else max(60.0, self.cfg.tape_seconds)

    def symbols(self) -> list[str]:
        """Every common stock of ``tape_min_cap``+ and ``min_price``+, largest first."""
        rows = [
            li
            for li in self.universe.by_symbol.values()
            if li.common
            and (li.market_cap or 0) >= self.cfg.tape_min_cap
            and (li.price or 0) >= self.cfg.min_price
        ]
        rows.sort(key=lambda li: -(li.market_cap or 0))
        return [li.symbol for li in rows]

    async def _pace(self, interval_s: float) -> None:
        if self._pace_fn is not None:
            await self._pace_fn(interval_s)
            return
        now = time.monotonic()
        wait = self._next_slot - now
        self._next_slot = max(now, self._next_slot) + interval_s
        if wait > 0:
            await asyncio.sleep(wait)

    # ------------------------------------------------------------------ reads

    async def read_alpaca(self, symbols: list[str], today: date) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        acfg = self.alpaca.cfg
        for i in range(0, len(symbols), self.cfg.tape_batch):
            chunk = [s.replace("-", ".") for s in symbols[i : i + self.cfg.tape_batch]]
            await self._pace(acfg.alpaca_request_interval_s)
            resp = await self.http.get(
                acfg.alpaca_data_url.rstrip("/") + "/v2/stocks/snapshots",
                headers=self.alpaca.headers,
                params={"symbols": ",".join(chunk), "feed": acfg.alpaca_stock_feed},
                timeout_s=20,
            )
            self.reads += 1
            out.update(parse_tape_snapshots(resp.json(), today))
        day = today.isoformat()
        for sym, row in out.items():
            row["base_volume"] = self.base.update(sym, row.get("prev_volume"), day)
        return out

    async def read_yahoo(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if self.yahoo_json is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(symbols), 150):
            chunk = symbols[i : i + 150]
            doc = await self.yahoo_json(YAHOO_QUOTE_URL, {"symbols": ",".join(chunk)})
            self.reads += 1
            for sym, row in parse_yahoo_quotes(doc).items():
                row["base_volume"] = row.pop("avg_volume", None)
                out[sym] = row
        return out

    async def sweep(self, now: float | None = None) -> list[IgnitionHit]:
        """Read every stock once, share the prices, and scan them for ignitions."""
        t0 = time.monotonic()
        now = now or time.time()
        today = datetime.fromtimestamp(now, ET).date()
        syms = self.symbols()
        if not syms:
            return []
        self.health.polls += 1
        try:
            if self.provider == "alpaca":
                rows = await self.read_alpaca(syms, today)
                missing = [
                    s for s in syms if s not in rows and (li := self.universe.get(s)) is not None and li.otc
                ]
                if missing:
                    rows.update(await self.read_yahoo(missing))
            else:
                rows = await self.read_yahoo(syms)
        except _ERRORS as exc:
            self.health.errors += 1
            self.health.consecutive_errors += 1
            self.health.last_error = f"{type(exc).__name__}: {exc}"[:300]
            self.health.last_error_at = time.time()
            log.info("tape sweep failed: %s", exc)
            return []
        self.health.consecutive_errors = 0
        self.health.last_ok = time.time()
        self.health.items += len(rows)
        self.last_sweep_s = round(time.monotonic() - t0, 2)
        ticks = self.ticks_from(rows)
        self.ticks += len(ticks)
        self.share(ticks)
        self.base.save()
        return self.scanner.observe(ticks, now)

    def ticks_from(self, rows: dict[str, dict[str, Any]]) -> list[Tick]:
        out = []
        for sym, r in rows.items():
            li = self.universe.get(sym)
            out.append(
                Tick(
                    sym, float(r["ts"]), float(r["price"]), r.get("prev_close"), r.get("volume"),
                    r.get("base_volume"), r.get("high"), r.get("low"), r.get("vwap"),
                    (li.market_cap if li is not None else None) or r.get("market_cap"),
                    (li.name if li is not None else "") or r.get("name", ""),
                )
            )  # fmt: skip
        return out

    def share(self, ticks: Iterable[Tick]) -> None:
        """Prices go to the live store (the options tape's movers and model values)."""
        if self.live is None:
            return
        for t in ticks:
            self.live.update(t.symbol, t.price, t.prev_close, t.ts, f"tape:{self.provider}")
            if t.high or t.low:
                self.live.set_range(t.symbol, t.high, t.low, None, t.ts)

    # ------------------------------------------------------------------ loop

    def _phase(self, now: float) -> str:
        if self.market_phase is None:
            return "open"
        try:
            return str(self.market_phase(now))
        except Exception:  # noqa: BLE001 - a calendar bug must not stop the tape
            return "open"

    async def run(self, on_hits: Callable[[list[IgnitionHit]], Any], stop: asyncio.Event) -> None:
        """Regular session only: the volume profile ignition is measured against is the session's."""
        while not stop.is_set():
            now = time.time()
            if self._phase(now) != "open":
                await _sleep_or_stop(stop, 30.0)
                continue
            started = time.monotonic()
            hits = await self.sweep(now)
            if hits:
                try:
                    await on_hits(hits)
                except Exception:  # noqa: BLE001 - one bad alert must not stop the tape
                    log.exception("ignition hits not published")
            took = time.monotonic() - started
            await _sleep_or_stop(stop, max(1.0, self.interval - took))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "provider": self.provider,
            "interval_s": self.interval,
            "covered": len(self.symbols()),
            "reads": self.reads,
            "ticks": self.ticks,
            "last_sweep_s": self.last_sweep_s,
            "volume_averages": len(self.base.rows),
            "scanner": self.scanner.status(),
            **self.health.to_dict(),
        }
