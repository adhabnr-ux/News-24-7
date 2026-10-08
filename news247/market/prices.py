"""Price feeds that drive the move detector.

* ``yahoo``  — free, no key. Polls Yahoo's spark endpoint (20 symbols per request) every
  ``poll_seconds``; falls back to the per-symbol chart endpoint if spark misbehaves.
  Includes pre/post-market prices.
* ``finnhub`` — free key, real-time trade websocket (≈50 symbols on the free tier).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..config import MarketConfig
from ..http import BROWSER_HEADERS, HttpClient
from ..models import PriceMove
from ..sources.base import SourceHealth, _sleep_or_stop
from .detector import MoveDetector

log = logging.getLogger(__name__)

OnMoves = Callable[[list[PriceMove]], Awaitable[None]]

SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _series(timestamps: list[Any] | None, closes: list[Any] | None) -> list[tuple[float, float]]:
    out = []
    for ts, c in zip(timestamps or [], closes or []):
        if ts is not None and c is not None:
            out.append((float(ts), float(c)))
    return out


def parse_spark(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize both spark response shapes to {symbol: {points, prev_close, price, ts}}."""
    out: dict[str, dict[str, Any]] = {}
    if "spark" in data:  # v7 shape
        for res in data["spark"].get("result") or []:
            sym = res.get("symbol")
            for r in res.get("response") or []:
                meta = r.get("meta", {})
                q = (r.get("indicators", {}).get("quote") or [{}])[0]
                out[sym] = {
                    "points": _series(r.get("timestamp"), q.get("close")),
                    "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
                    "price": meta.get("regularMarketPrice"),
                    "ts": meta.get("regularMarketTime"),
                }
        return out
    for sym, r in data.items():  # v8 shape: {SYM: {timestamp, close, chartPreviousClose, ...}}
        if not isinstance(r, dict):
            continue
        out[sym] = {
            "points": _series(r.get("timestamp"), r.get("close")),
            "prev_close": r.get("chartPreviousClose") or r.get("previousClose"),
            "price": r.get("regularMarketPrice"),
            "ts": r.get("regularMarketTime"),
        }
    return out


def parse_chart(data: dict[str, Any]) -> dict[str, Any] | None:
    res = (data.get("chart", {}).get("result") or [None])[0]
    if not res:
        return None
    meta = res.get("meta", {})
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    return {
        "points": _series(res.get("timestamp"), q.get("close")),
        "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "price": meta.get("regularMarketPrice"),
        "ts": meta.get("regularMarketTime"),
    }


class PriceMonitor:
    """Runs a price feed, feeds the detector and reports moves."""

    def __init__(self, cfg: MarketConfig, http: HttpClient, detector: MoveDetector) -> None:
        self.cfg = cfg
        self.http = http
        self.detector = detector
        self.health = SourceHealth()
        self.mode = "spark"
        self._seeded: set[str] = set()

    async def run(self, on_moves: OnMoves, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._group_loop(on_moves, stop), name="groups")]
        if self.cfg.provider == "finnhub":
            tasks.append(asyncio.create_task(self._finnhub(on_moves, stop), name="finnhub"))
        else:
            tasks.append(asyncio.create_task(self._yahoo(on_moves, stop), name="yahoo"))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    async def _group_loop(self, on_moves: OnMoves, stop: asyncio.Event) -> None:
        while not stop.is_set():
            moves = self.detector.check_groups()
            if moves:
                await on_moves(moves)
            await _sleep_or_stop(stop, 5.0)

    # ------------------------------------------------------------------ yahoo

    def ingest(self, sym: str, snap: dict[str, Any], now: float) -> list[PriceMove]:
        """Feed one symbol's snapshot into the detector."""
        self.detector.set_prev_close(sym, snap.get("prev_close"))
        points = snap["points"]
        price = points[-1][1] if points else snap.get("price")
        if not price:
            return []
        if sym not in self._seeded:
            # seed with completed bars; the live (last) bar is fed as a fresh tick below
            self.detector.seed(sym, points[:-1])
            self._seeded.add(sym)
        # 1-minute bars are stamped at the bar's start; the latest close is "now"
        ts = now
        if points and now - points[-1][0] > 180:  # market closed / stale data
            ts = points[-1][0]
            if sym in self.detector.last_price and self.detector.last_price[sym][0] >= ts:
                return []
        return self.detector.update(sym, float(price), ts)

    async def poll_yahoo_once(self) -> list[PriceMove]:
        syms = list(self.cfg.symbols)
        snaps: dict[str, dict[str, Any]] = {}
        if self.mode == "spark":
            try:
                for i in range(0, len(syms), 20):
                    chunk = syms[i : i + 20]
                    resp = await self.http.get(
                        SPARK_URL,
                        params={
                            "symbols": ",".join(chunk),
                            "range": "1d",
                            "interval": "1m",
                            "includePrePost": str(self.cfg.extended_hours).lower(),
                        },
                        headers=BROWSER_HEADERS,
                    )
                    snaps.update(parse_spark(resp.json()))
                if not snaps:
                    raise ValueError("spark returned no symbols")
            except Exception as exc:  # noqa: BLE001
                log.warning("Yahoo spark failed (%s); switching to per-symbol chart endpoint", exc)
                self.mode = "chart"
                snaps = {}
        if self.mode == "chart":
            sem = asyncio.Semaphore(4)

            async def one(sym: str) -> None:
                async with sem:
                    try:
                        resp = await self.http.get(
                            CHART_URL.format(symbol=sym),
                            params={
                                "range": "1d",
                                "interval": "1m",
                                "includePrePost": str(self.cfg.extended_hours).lower(),
                            },
                            headers=BROWSER_HEADERS,
                        )
                        snap = parse_chart(resp.json())
                        if snap:
                            snaps[sym] = snap
                    except Exception as exc:  # noqa: BLE001
                        log.debug("chart %s failed: %s", sym, exc)

            await asyncio.gather(*(one(s) for s in syms))
            if not snaps:
                raise RuntimeError("no prices from Yahoo (spark and chart both failed)")
        now = time.time()
        moves: list[PriceMove] = []
        for sym, snap in snaps.items():
            moves.extend(self.ingest(sym, snap, now))
        return moves

    async def _yahoo(self, on_moves: OnMoves, stop: asyncio.Event) -> None:
        spark_retry_at = 0.0
        while not stop.is_set():
            if self.mode == "chart" and time.time() > spark_retry_at:
                self.mode, spark_retry_at = "spark", time.time() + 3600  # periodically retry the batch API
            try:
                t0 = time.monotonic()
                moves = await self.poll_yahoo_once()
                self.health.last_fetch_ms = (time.monotonic() - t0) * 1000
                self.health.polls += 1
                self.health.consecutive_errors = 0
                self.health.last_ok = time.time()
                if moves:
                    await on_moves(moves)
                delay = self.cfg.poll_seconds
                if self.mode == "chart":
                    delay = max(delay, len(self.cfg.symbols) * 0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.errors += 1
                self.health.consecutive_errors += 1
                self.health.last_error = f"{type(exc).__name__}: {exc}"[:300]
                self.health.last_error_at = time.time()
                if self.health.consecutive_errors in (1, 3) or self.health.consecutive_errors % 20 == 0:
                    log.warning("price feed error (%d in a row): %s", self.health.consecutive_errors, exc)
                delay = min(300.0, self.cfg.poll_seconds * 2 ** min(self.health.consecutive_errors, 5))
            await _sleep_or_stop(stop, delay * random.uniform(0.95, 1.05))

    # ------------------------------------------------------------------ finnhub

    async def _finnhub_prev_closes(self, stop: asyncio.Event) -> None:
        for sym in self.cfg.symbols:
            if stop.is_set():
                return
            try:
                resp = await self.http.get(
                    "https://finnhub.io/api/v1/quote", params={"symbol": sym, "token": self.cfg.finnhub_token}
                )
                q = resp.json()
                self.detector.set_prev_close(sym, q.get("pc"))
                if q.get("c") and sym not in self.detector.last_price:
                    self.detector.update(sym, float(q["c"]), float(q.get("t") or time.time()))
            except Exception as exc:  # noqa: BLE001
                log.debug("finnhub quote %s failed: %s", sym, exc)
            await asyncio.sleep(1.1)  # free tier: 60 calls/minute

    def handle_finnhub_message(self, raw: str) -> list[PriceMove]:
        msg = json.loads(raw)
        if msg.get("type") != "trade":
            return []
        latest: dict[str, tuple[float, float]] = {}
        for t in msg.get("data") or []:
            sym, price, ts = t.get("s"), t.get("p"), t.get("t")
            if sym and price and ts:
                prev = latest.get(sym)
                if prev is None or ts / 1000.0 >= prev[0]:
                    latest[sym] = (ts / 1000.0, float(price))
        moves: list[PriceMove] = []
        for sym, (ts, price) in latest.items():
            moves.extend(self.detector.update(sym, price, ts))
        return moves

    async def _finnhub(self, on_moves: OnMoves, stop: asyncio.Event) -> None:
        prev_task = asyncio.create_task(self._finnhub_prev_closes(stop))
        attempt = 0
        try:
            while not stop.is_set():
                try:
                    url = f"wss://ws.finnhub.io?token={self.cfg.finnhub_token}"
                    async with self.http.session.ws_connect(url, heartbeat=30, receive_timeout=90) as ws:
                        for sym in self.cfg.symbols:
                            await ws.send_str(json.dumps({"type": "subscribe", "symbol": sym}))
                        self.health.connected = True
                        self.health.consecutive_errors = 0
                        attempt = 0
                        log.info("finnhub price stream connected (%d symbols)", len(self.cfg.symbols))
                        async for msg in ws:
                            if stop.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.health.polls += 1
                                self.health.last_ok = time.time()
                                moves = self.handle_finnhub_message(msg.data)
                                if moves:
                                    await on_moves(moves)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                        raise ConnectionError("finnhub stream closed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.health.connected = False
                    self.health.errors += 1
                    self.health.consecutive_errors += 1
                    self.health.last_error = f"{type(exc).__name__}: {exc}"[:300]
                    attempt += 1
                    delay = min(120.0, 2 ** min(attempt, 7))
                    if not stop.is_set():
                        log.warning("finnhub stream error (%s); reconnecting in %.0fs", exc, delay)
                    await _sleep_or_stop(stop, delay)
        finally:
            prev_task.cancel()
