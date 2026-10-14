"""Ignition: a stock starting to move, minutes before it is a big mover, and what confirms it.

The radar's day-change ladder fires when a stock is already 4-35% from yesterday's close. On
real tape that is late: in the study behind this module (106 US stocks, 1,455 sessions of
1-minute bars, Sep 21 - Oct 13 2026; docs/research/radar-ignition-2026-10-14.md) the stock
closed lower than the radar's alert price more often than not (median -0.6%), and only 19% of
the day's move was left when it fired.

Two earlier triggers, each on the live tape (price, the day's volume, the day's range):

* **Ignition**: in the last ~5 minutes the stock moved ``R`` (3% small cap, 1.5% mid, 1.0%
  large, 0.6% mega) on 4x+ the volume normal for that time of day, to a new high (or low) of
  the session, while the day move before it was still under the radar's line.
* **Build-up**: a steady climb (or slide) to a new session extreme, half-way to the radar's
  line, on 2x+ normal volume so far: the grinding moves (FICO +10.8% on Oct 12) that never
  burst.

Together they saw 33 of the 39 big intraday moves in the sample first, a median 22 minutes
before the radar, with 45% of the move left (19% at the radar).

What they do not do on their own is pick winners: on all ignitions, the stock was higher an hour
later about 56% of the time and flat by the close on average. Moves backed by information
behave differently (Chan 2003, "Stock price reaction to news and no-news": price moves with news
drift on, those without reverse; Easley, O'Hara and Srinivas 1998 and Pan and Poteshman 2006:
option volume leads the stock). So a hit says what backs it (``confirmed_by``): a headline or
filing, an insider purchase, or unusual options volume on the same side. The engine pushes
confirmed ignitions and keeps the rest on the radar board, reading their option chains at once.

"Expected volume" is the stock's normal day volume times the share of a day's volume that
trades in that stretch of the session (``data/volume_profile.json``, measured on the same
sample: the first 30 minutes carry 9%, the first hour 17%, the last 5 minutes and the close 12%).

Pure logic, no I/O. The tape feed (market/tape.py) supplies the observations.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Any
from zoneinfo import ZoneInfo

from .universe import band, fmt_cap

ET = ZoneInfo("America/New_York")
SESSION_MIN = 390
HISTORY_S = 30 * 60


def _load_profile() -> list[float]:
    doc = json.loads(resources.files("news247.data").joinpath("volume_profile.json").read_text())
    step = int(doc["step_minutes"])
    pts = [float(x) for x in doc["cumulative"]]
    out = []
    for m in range(SESSION_MIN + 1):  # cumulative share at minute m (0 = the open)
        i, f = divmod(m, step)
        out.append(pts[i] if i + 1 >= len(pts) else pts[i] + (pts[i + 1] - pts[i]) * f / step)
    return out


PROFILE = _load_profile()


def session_minute(ts: float) -> float | None:
    """Minutes since 09:30 ET (fractional); None outside the regular session."""
    dt = datetime.fromtimestamp(ts, ET)
    m = (dt.hour * 60 + dt.minute + dt.second / 60.0) - (9 * 60 + 30)
    return m if 0 <= m <= SESSION_MIN else None


def volume_share(m0: float, m1: float, profile: list[float] = PROFILE) -> float:
    """Share of a normal day's volume traded between session minutes m0 and m1."""

    def at(m: float) -> float:
        m = min(max(m, 0.0), float(SESSION_MIN))
        i = int(m)
        if i >= SESSION_MIN:
            return profile[SESSION_MIN]
        return profile[i] + (profile[i + 1] - profile[i]) * (m - i)

    return max(0.0, at(m1) - at(m0))


@dataclass
class Tick:
    """One live observation of a stock (a snapshot or a quote)."""

    symbol: str
    ts: float  # when the price was true (epoch)
    price: float
    prev_close: float | None
    volume: float | None  # the session's volume so far (one feed throughout)
    base_volume: float | None  # a normal day's volume on the same feed
    high: float | None = None  # the session's high / low so far
    low: float | None = None
    vwap: float | None = None
    market_cap: float | None = None
    name: str = ""


@dataclass
class IgnitionHit:
    symbol: str
    kind: str  # "ignition" | "buildup"
    direction: str  # "up" | "down"
    price: float
    prev_close: float
    day_pct: float
    launch: float  # price at the start of the window
    window_s: float
    move_pct: float  # over the window
    burst: float | None  # window volume / normal for that stretch of the day
    rvol: float | None  # the day's volume so far / normal by this time
    stop: float  # past the launch (or the VWAP, whichever is nearer): where the move has failed
    vwap: float | None = None
    market_cap: float | None = None
    name: str = ""
    strong: bool = False
    primed: list[str] = field(
        default_factory=list
    )  # what was already pointing at it (options, insider, news)
    detected: float = field(default_factory=time.time)

    @property
    def risk_pct(self) -> float:
        return abs(self.price - self.stop) / self.price * 100 if self.price else 0.0

    @property
    def band(self) -> str:
        return band(self.market_cap)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "direction": self.direction,
            "price": self.price,
            "prev_close": self.prev_close,
            "day_pct": round(self.day_pct, 2),
            "launch": self.launch,
            "window_s": round(self.window_s),
            "move_pct": round(self.move_pct, 2),
            "burst": round(self.burst, 1) if self.burst is not None else None,
            "rvol": round(self.rvol, 1) if self.rvol is not None else None,
            "stop": round(self.stop, 4),
            "risk_pct": round(self.risk_pct, 2),
            "vwap": round(self.vwap, 4) if self.vwap else None,
            "market_cap": self.market_cap,
            "cap": fmt_cap(self.market_cap),
            "band": self.band,
            "name": self.name,
            "strong": self.strong,
            "primed": list(self.primed),
            "detected": self.detected,
        }


@dataclass
class _Sym:
    day: str = ""
    points: deque[tuple[float, float, float, float, float]] = field(default_factory=lambda: deque(maxlen=120))
    fired: set[str] = field(default_factory=set)  # "ignition:up", "buildup:down", ...


class IgnitionScanner:
    """Turns ticks into ignition and build-up hits (see the module docstring)."""

    def __init__(self, cfg: Any, profile: list[float] = PROFILE) -> None:
        self.cfg = cfg
        self.profile = profile
        self.state: dict[str, _Sym] = {}
        self.primes: dict[str, list[tuple[float, str, str]]] = {}  # symbol -> [(until, direction, why)]
        self.scans = 0
        self.hits_total = 0

    # ------------------------------------------------------------------ thresholds

    def move_threshold(self, cap: float | None) -> float:
        c = self.cfg
        b = band(cap)
        if b in ("nano cap", "micro cap", "small cap"):
            return c.ignition_pct_small
        if b == "mid cap":
            return c.ignition_pct_mid
        if b == "large cap":
            return c.ignition_pct_large
        if b == "mega cap":
            return c.ignition_pct_mega
        return c.ignition_pct_small

    def day_threshold(self, cap: float | None) -> float:
        """The radar's own day-move line for this size: past it, the radar has already said it."""
        c = self.cfg
        cap = cap or 0.0
        if cap < c.radar_max_cap:
            return c.radar_min_pct
        if cap < 10e9:
            return c.radar_mid_min_pct
        if cap < 200e9:
            return c.radar_large_min_pct
        return c.radar_mega_min_pct

    # ------------------------------------------------------------------ priming

    def prime(
        self, symbol: str, direction: str, why: str, hold_s: float | None = None, now: float | None = None
    ) -> None:
        """Something already points at this stock (options bought, an insider buying, a headline):
        its first push is read with lower thresholds and the hit says why."""
        now = now or time.time()
        until = now + (hold_s if hold_s is not None else self.cfg.ignition_prime_s)
        rows = [p for p in self.primes.get(symbol, []) if p[0] > now and p[2] != why]
        rows.append((until, direction, why))
        self.primes[symbol] = rows[-5:]

    def primed(self, symbol: str, direction: str, now: float) -> list[str]:
        rows = [p for p in self.primes.get(symbol, []) if p[0] > now]
        if not rows:
            self.primes.pop(symbol, None)
            return []
        self.primes[symbol] = rows
        return [why for _, d, why in rows if d in (direction, "either")]

    # ------------------------------------------------------------------ the scan

    def observe(self, ticks: list[Tick], now: float | None = None) -> list[IgnitionHit]:
        now = now or time.time()
        self.scans += 1
        hits: list[IgnitionHit] = []
        for t in ticks:
            hit = self._one(t, now)
            if hit is not None:
                hits.append(hit)
        self.hits_total += len(hits)
        if len(self.state) > 20000:
            for sym in list(self.state)[:5000]:
                del self.state[sym]
        return hits

    def _one(self, t: Tick, now: float) -> IgnitionHit | None:
        c = self.cfg
        if not t.price or not t.prev_close or t.price < c.min_price:
            return None
        if t.base_volume is not None and t.base_volume < c.ignition_min_base_volume:
            return None  # one print is a "burst" on a feed this thin
        m = session_minute(t.ts)
        if m is None:
            return None
        day = datetime.fromtimestamp(t.ts, ET).date().isoformat()
        st = self.state.setdefault(t.symbol, _Sym())
        if st.day != day:
            st.day, st.fired = day, set()
            st.points.clear()
        high = max(t.high or t.price, t.price)
        low = min(t.low or t.price, t.price)
        vol = t.volume if t.volume is not None else float("nan")
        prev_pt = st.points[-1] if st.points else None
        if prev_pt is not None and t.ts <= prev_pt[0]:
            return None  # an old or repeated quote
        st.points.append((t.ts, t.price, vol, high, low))
        while st.points and t.ts - st.points[0][0] > HISTORY_S:
            st.points.popleft()
        if m < c.ignition_start_min:
            return None
        rvol = None
        if t.volume and t.base_volume and t.base_volume > 0:
            share = volume_share(0.0, m, self.profile)
            rvol = t.volume / (t.base_volume * share) if share > 0 else None
        day_pct = (t.price / t.prev_close - 1.0) * 100.0
        line = self.day_threshold(t.market_cap)
        hit = self._ignition(t, st, m, rvol, day_pct, line, now)
        if hit is None and c.ignition_buildup:
            hit = self._buildup(t, st, m, rvol, day_pct, line, prev_pt, now)
        return hit

    def _window_start(self, st: _Sym, ts: float) -> tuple[float, float, float, float, float] | None:
        """The newest point at least ~the window old (and not far older)."""
        w = self.cfg.ignition_window_s
        best = None
        for p in st.points:
            age = ts - p[0]
            if age < 0.8 * w:
                break
            if age <= 2.5 * w:
                best = p
        return best

    def _ignition(
        self, t: Tick, st: _Sym, m: float, rvol: float | None, day_pct: float, line: float, now: float
    ) -> IgnitionHit | None:
        c = self.cfg
        start = self._window_start(st, t.ts)
        if start is None:
            return None
        t0, p0, v0, h0, l0 = start
        move = (t.price / p0 - 1.0) * 100.0
        direction = "up" if move > 0 else "down"
        key = f"ignition:{direction}"
        if key in st.fired:
            return None
        primed = self.primed(t.symbol, direction, now)
        need = self.move_threshold(t.market_cap) * (c.ignition_prime_factor if primed else 1.0)
        if abs(move) < need:
            return None
        burst = None
        m0 = session_minute(t0)
        if t.volume is not None and not math.isnan(v0) and t.base_volume and m0 is not None:
            exp = t.base_volume * volume_share(m0, m, self.profile)
            burst = (t.volume - v0) / exp if exp > 0 else None
        if burst is None or burst < (c.ignition_burst_primed if primed else c.ignition_burst):
            return None
        # a new session extreme: above the high (below the low) as it stood when the push began
        if (direction == "up" and t.price <= h0) or (direction == "down" and t.price >= l0):
            return None
        before = (p0 / t.prev_close - 1.0) * 100.0
        if abs(before) >= line:
            return None  # already a big mover: the radar's ladder has it
        st.fired.add(key)
        stop = self._stop(direction, p0, t.vwap, t.price)
        strong = (rvol or 0) >= c.ignition_strong_rvol and burst >= c.ignition_strong_burst
        return IgnitionHit(
            t.symbol, "ignition", direction, t.price, t.prev_close, day_pct, p0, t.ts - t0, move, burst, rvol,
            stop, t.vwap, t.market_cap, t.name, strong, primed, now,
        )  # fmt: skip

    def _buildup(
        self,
        t: Tick,
        st: _Sym,
        m: float,
        rvol: float | None,
        day_pct: float,
        line: float,
        prev_pt: tuple[float, float, float, float, float] | None,
        now: float,
    ) -> IgnitionHit | None:
        c = self.cfg
        if m < c.ignition_buildup_start_min or prev_pt is None:
            return None
        direction = "up" if day_pct > 0 else "down"
        key = f"buildup:{direction}"
        if key in st.fired or (f"ignition:{direction}" in st.fired):
            return None
        primed = self.primed(t.symbol, direction, now)
        frac = c.ignition_buildup_frac * (c.ignition_prime_factor if primed else 1.0)
        if not (frac * line <= abs(day_pct) < line):
            return None
        if rvol is None or rvol < c.ignition_buildup_rvol:
            return None
        # at a new session extreme: past the high/low of the previous observation
        if (direction == "up" and t.price <= prev_pt[3]) or (direction == "down" and t.price >= prev_pt[4]):
            return None
        st.fired.add(key)
        stop = self._stop(direction, t.prev_close, t.vwap, t.price)
        return IgnitionHit(
            t.symbol, "buildup", direction, t.price, t.prev_close, day_pct, t.prev_close, 0.0, day_pct, None, rvol,
            stop, t.vwap, t.market_cap, t.name, False, primed, now,
        )  # fmt: skip

    @staticmethod
    def _stop(direction: str, launch: float, vwap: float | None, price: float) -> float:
        """Past the launch or the session VWAP, whichever is nearer the price (but on the right side)."""
        levels = [launch] + ([vwap] if vwap else [])
        if direction == "up":
            below = [x for x in levels if x < price]
            return max(below) if below else launch
        above = [x for x in levels if x > price]
        return min(above) if above else launch

    def status(self) -> dict[str, Any]:
        return {
            "scans": self.scans,
            "hits": self.hits_total,
            "tracked": len(self.state),
            "primed": sorted(self.primes)[:50],
        }
