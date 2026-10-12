"""The day-move desk: a big stock's directional day, and which of its options to buy, at what
price, and when the move is over.

The question (Oct 12 2026): "options across all big stocks go crazy depending on the direction of
the day (MSFT bullish: some call $50-60 out of the money goes crazy). How many days out? At what
price do we buy? How do we know when the volume is gone?" The answers come from the real OPRA
tape of 7 directional days and 4 days whose early move failed (Sep 21 - Oct 12 2026, META, MSFT,
TSLA, NVDA, AMD, MU; docs/research/options-volume-strategy-2026-10-12.md):

* **The direction call is the trade.** An option's spike is the stock's move: bought at the open,
  10%-out-of-the-money contracts made a median +30% by the close on the days the stock trended,
  and lost 24-83% on the days its opening move failed (TSLA Sep 25, MSFT Oct 1, NVDA Oct 9, MU's
  bounce today). So the call waits for the **9:45 hold**: the stock 1%+ from yesterday's close,
  at 9:45 at least as far out as its first 5-minute close, and within 10% of its best level of the
  day. That rule took none of the four failed days and 11 trend-day trades (median +10% to the
  close, 7 of 11 up, median worst drawdown -17%). Later in the day the call needs the stock
  pressing its extreme (90%+ of its best move held).
* **Days to expiry decides how much of the move you get after the call.** On META's +4.1% day,
  bought at 9:45, the same 10%-out strike made: 2 days +54%, 4 days +29%, 11 days +10%, 18 days
  +1%, 39 days -1%. The month-out contracts had repriced in the first 15 minutes. They only paid
  on a trend that kept going all day or for days (META Sep 21: 25 days +141%, 60 days +64%).
  So the desk picks by delta, not a fixed distance: a ~0.25-delta contract 1-7 days out for the
  day, a ~0.30-delta 14-35 days out for a trend that may carry over.
* **The volume spike is not the top; the stock's grip on its move is.** 8 of 41 option volume
  climaxes were within 10 minutes of the day's high and 29 saw a higher price later. With the
  stock holding 90%+ of its move at the climax, 18 of 24 made new highs later. With it having
  given back 30%+, 0 of 6 closed higher (median -16%). So the desk's pullback zone is the stock
  between 85% and 70% of its move, and under 70% the call is off.
* **"Back to a penny" only happens on contracts expiring today**, when the stock gives its move
  back (MU's $990 put: $15.30 to $0.90). Contracts 2+ days out gave back a median of about 20%
  from their high on trend days and never returned to pennies. A penny contract is a bet that the
  stock re-tests its extreme in the hours left. The desk shows that as a separate "lotto" tier
  only when turned on.

The system never trades. Every price here is a limit to consider, with the stock level that
invalidates it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .options import bs_price, implied_vol, years_left

if TYPE_CHECKING:  # pragma: no cover
    from ..config import OptionsConfig
    from .live import DayPath, LiveQuote
    from .options import Chain, Contract

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _mins(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


# --------------------------------------------------------------------------- the call


@dataclass
class DayCall:
    """A big stock's directional day, called."""

    symbol: str
    cp: str  # "C" on an up day, "P" on a down day
    ts: float  # when the call was made
    price: float  # the stock then
    prev: float  # yesterday's close
    best: float  # its best level of the session in the call's direction (so far)
    how: str  # "9:45 hold" | "pressing its high" | "pressing its low"
    cap: float | None = None
    state: str = "called"  # called -> (zone) -> faded | the session ends
    zone_ts: float = 0.0
    faded_ts: float = 0.0
    below_ts: float = 0.0  # since when it has been under the fade line (a dip back above resets it)
    last: float = 0.0  # the latest stock price seen
    picks: list[Pick] = field(default_factory=list)
    read: bool = False  # its chain has been read and the alert built

    @property
    def side(self) -> str:
        return "calls" if self.cp == "C" else "puts"

    @property
    def sign(self) -> int:
        return 1 if self.cp == "C" else -1

    def move_pct(self, price: float | None = None) -> float:
        return ((price or self.price) / self.prev - 1.0) * 100.0

    def kept(self, price: float) -> float:
        """The share of the best move still held at ``price`` (1.0 = at the extreme)."""
        best = self.best - self.prev
        if not best:
            return 0.0
        return (price - self.prev) / best

    def line(self, kept: float) -> float:
        """The stock price that holds ``kept`` of the best move."""
        return self.prev + kept * (self.best - self.prev)

    def observe(self, price: float) -> None:
        self.last = price
        if (self.sign > 0 and price > self.best) or (self.sign < 0 and price < self.best):
            self.best = price

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "picks"}
        d["side"] = self.side
        d["move_pct"] = round(self.move_pct(), 2)
        d["picks"] = [p.to_dict() for p in self.picks]
        return d


def day_call(
    symbol: str,
    quote: LiveQuote | None,
    path: DayPath | None,
    now: float,
    cfg: OptionsConfig,
    cap: float | None = None,
) -> DayCall | None:
    """Is this a directional day for ``symbol`` now? The 9:45 hold before 10:00, the stock pressing
    its extreme after; none before 9:45 or from ``daymove_until`` on."""
    if quote is None or not quote.prev_close or path is None:
        return None
    mins = _mins(now)
    if mins < 9 * 60 + 45 or mins >= cfg.daymove_until_min:
        return None
    if now - quote.ts > cfg.stock_quote_max_age_s:
        return None
    prev, px = quote.prev_close, quote.price
    move = (px / prev - 1.0) * 100.0
    if abs(move) < cfg.daymove_min_pct:
        return None
    sign = 1 if move > 0 else -1
    ext = path.s_high if sign > 0 else path.s_low
    best = px if ext is None else (max(ext, px) if sign > 0 else min(ext, px))
    held = (px - prev) / (best - prev) if best != prev else 1.0
    if held < cfg.daymove_hold:
        return None  # it has come off its best level: not pressing
    if mins < 10 * 60:
        if path.first5 is None or path.first5_ts < path.s_first_ts:
            if abs(move) < 1.5 * cfg.daymove_min_pct:
                return None  # no first bar to compare with (a late start): ask for more
            how = "9:45 hold"
        elif sign * (px - path.first5) < 0:
            return None  # fading since the first five minutes (MU, AMD, TSLA Sep 25)
        else:
            how = "9:45 hold"
    else:
        how = "pressing its high" if sign > 0 else "pressing its low"
    return DayCall(symbol, "C" if sign > 0 else "P", now, px, prev, best, how, cap=cap, last=px)


# --------------------------------------------------------------------------- the contracts


@dataclass
class Pick:
    tier: str  # "day" (1-7 days) | "swing" (14-35 days) | "lotto" (0-1 days)
    occ: str
    label: str
    cp: str
    strike: float
    expiry: str
    dte: int
    bid: float
    ask: float
    mid: float
    iv: float
    delta: float
    per_1pct: float  # $ a share it gains on a 1% stock move its way, now
    leverage: float  # % it gains per 1% stock move
    breakeven: float  # the stock at expiry for the ask to pay back
    zone_hi: float  # its value with the stock at 85% of its move (a pullback limit)
    zone_lo: float  # ... at 70% (under that the call is off)
    volume: int = 0
    open_interest: int | None = None
    spread_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _delta(s: float, k: float, t: float, iv: float, cp: str) -> float:
    from .follow import bs_delta

    return bs_delta(s, k, t, iv, cp)


def pick_contracts(chain: Chain, call: DayCall, now: float, cfg: OptionsConfig) -> list[Pick]:
    """For each tier, the contract on the call's side whose delta is nearest the tier's target,
    among those with a live two-sided market no wider than ``daymove_max_spread`` of its mid."""
    s = chain.price or call.last or call.price
    if not s:
        return []
    today = datetime.fromtimestamp(now, ET).date()
    tiers = [
        ("day", cfg.daymove_day_min_dte, cfg.daymove_day_max_dte, cfg.daymove_day_delta),
        ("swing", cfg.daymove_swing_min_dte, cfg.daymove_swing_max_dte, cfg.daymove_swing_delta),
    ]
    if cfg.daymove_lotto:
        tiers.append(("lotto", 0, 1, cfg.daymove_lotto_delta))
    hi_line, lo_line = call.line(0.85), call.line(cfg.daymove_fade)
    out: list[Pick] = []
    for tier, lo, hi, target in tiers:
        best: tuple[float, Pick] | None = None
        for c in chain.contracts:
            if c.cp != call.cp:
                continue
            dte = (c.expiry - today).days
            if not lo <= dte <= hi:
                continue
            p = _pick(c, s, call, now, dte, tier, hi_line, lo_line, cfg)
            if p is None or not 0.03 <= p.delta <= 0.5:
                continue
            score = abs(p.delta - target) + 0.1 * p.spread_pct  # a tight market breaks near-ties
            if best is None or score < best[0]:
                best = (score, p)
        if best is not None:
            out.append(best[1])
    return out


def _pick(
    c: Contract,
    s: float,
    call: DayCall,
    now: float,
    dte: int,
    tier: str,
    hi_line: float,
    lo_line: float,
    cfg: OptionsConfig,
) -> Pick | None:
    if not (c.bid and c.ask and c.ask >= c.bid > 0):
        return None
    mid = (c.bid + c.ask) / 2.0
    spread = (c.ask - c.bid) / mid
    if spread > cfg.daymove_max_spread or mid < cfg.daymove_min_price:
        return None
    t = years_left(c.expiry, now)
    iv = implied_vol(mid, s, c.strike, t, c.cp)
    if iv is None:
        return None
    delta = abs(_delta(s, c.strike, t, iv, c.cp))
    way = 1.0 if c.cp == "C" else -1.0
    per = bs_price(s * (1 + 0.01 * way), c.strike, t, iv, c.cp) - bs_price(s, c.strike, t, iv, c.cp)
    return Pick(
        tier=tier,
        occ=c.symbol,
        label=c.label(),
        cp=c.cp,
        strike=c.strike,
        expiry=c.expiry.isoformat(),
        dte=dte,
        bid=c.bid,
        ask=c.ask,
        mid=round(mid, 4),
        iv=round(iv, 4),
        delta=round(delta, 3),
        per_1pct=round(per, 4),
        leverage=round(per / mid * 100.0, 1) if mid else 0.0,
        breakeven=round(c.strike + c.ask if c.cp == "C" else c.strike - c.ask, 2),
        zone_hi=round(bs_price(hi_line, c.strike, t, iv, c.cp), 2),
        zone_lo=round(bs_price(lo_line, c.strike, t, iv, c.cp), 2),
        volume=c.volume,
        open_interest=c.open_interest,
        spread_pct=round(spread, 3),
    )


# --------------------------------------------------------------------------- the volume


@dataclass
class VolumeTape:
    """One contract's traded volume between chain reads: how fast it is trading, its peak rate,
    and whether the wave is building, has peaked, or has dried up. (On Alpaca's free feed the
    trades behind it run about 15 minutes late.)"""

    samples: list[tuple[float, int]] = field(default_factory=list)  # (epoch, cumulative volume)
    peak: float = 0.0  # contracts a minute
    peak_ts: float = 0.0

    def add(self, ts: float, volume: int) -> None:
        if self.samples and (ts <= self.samples[-1][0] or volume < self.samples[-1][1]):
            return
        self.samples.append((ts, volume))
        del self.samples[:-40]
        r = self.rate()
        if r is not None and r > self.peak:
            self.peak, self.peak_ts = r, ts

    def rate(self) -> float | None:
        if len(self.samples) < 2:
            return None
        (t0, v0), (t1, v1) = self.samples[-2], self.samples[-1]
        return (v1 - v0) / max((t1 - t0) / 60.0, 1.0)

    def state(self) -> str:
        r = self.rate()
        if r is None or self.peak <= 0:
            return "too early to tell"
        if r >= 0.5 * self.peak:
            return "building" if r >= self.peak else "still heavy"
        rates = []
        for (a, va), (b, vb) in zip(self.samples[-3:], self.samples[-2:]):
            rates.append((vb - va) / max((b - a) / 60.0, 1.0))
        if len(rates) >= 2 and all(x < 0.25 * self.peak for x in rates):
            return "dried up"
        return "past its peak"


# --------------------------------------------------------------------------- the desk


@dataclass
class DayMoveEvent:
    kind: str  # "call" | "zone" | "faded"
    call: DayCall
    why: str
    chain: Chain | None = None


class DayMoveDesk:
    """Today's directional calls on big stocks, one per company a day: made from live stock
    prices, priced from the company's chain, watched until the move fades or the session ends."""

    def __init__(self, cfg: OptionsConfig) -> None:
        self.cfg = cfg
        self.day = ""
        self.calls: dict[str, DayCall] = {}
        self.tapes: dict[str, VolumeTape] = {}  # occ -> its volume between reads
        self.made_today = 0
        self.history: list[dict[str, Any]] = []  # recent events, for the app

    def _roll(self, now: float) -> None:
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        if day != self.day:
            self.day, self.calls, self.tapes, self.made_today = day, {}, {}, 0

    def scan(
        self, quotes: dict[str, LiveQuote], paths: dict[str, DayPath], caps: dict[str, float], now: float
    ) -> list[DayCall]:
        """New calls among ``caps`` (the covered big companies), biggest first; and the moves of
        the calls already made."""
        if not self.cfg.daymove:
            return []
        self._roll(now)
        new: list[DayCall] = []
        for sym, cap in sorted(caps.items(), key=lambda kv: -kv[1]):
            q = quotes.get(sym)
            if sym in self.calls:
                if q is not None:
                    self.calls[sym].observe(q.price)
                continue
            if self.made_today >= self.cfg.daymove_daily:
                continue
            c = day_call(sym, q, paths.get(sym), now, self.cfg, cap)
            if c is not None:
                self.calls[sym] = c
                self.made_today += 1
                new.append(c)
        return new

    def watch(self, now: float) -> list[DayMoveEvent]:
        """The pullback zone (the stock back to 85-70% of its move) and the fade (under 70%):
        each once a call."""
        out: list[DayMoveEvent] = []
        for c in self.calls.values():
            if not c.read or c.state == "faded" or not c.last:
                continue
            k = c.kept(c.last)
            if k < self.cfg.daymove_fade:
                if not c.below_ts:
                    c.below_ts = now
                if now - c.below_ts < self.cfg.daymove_fade_s:
                    continue  # one sharp dip is not a fade (MSFT Sep 25: under at 10:14, +3.65% at the close)
                c.state, c.faded_ts = "faded", now
                out.append(
                    DayMoveEvent(
                        "faded",
                        c,
                        f"{c.symbol} ${c.last:,.2f} has given back {(1 - k) * 100:.0f}% of its move "
                        f"(best ${c.best:,.2f}). In the study, with the stock under 70% of its move, "
                        "0 of 6 option spikes closed higher",
                    )
                )
            c.below_ts = 0.0
            if k <= 0.85 and c.state == "called" and _mins(now) < self.cfg.daymove_until_min:
                c.state, c.zone_ts = "zone", now
                out.append(
                    DayMoveEvent(
                        "zone",
                        c,
                        f"{c.symbol} ${c.last:,.2f} pulled back to {k * 100:.0f}% of its move and is holding "
                        f"above the 70% line (${c.line(self.cfg.daymove_fade):,.2f})",
                    )
                )
        for e in out:
            self.history.append({"ts": now, "kind": e.kind, "symbol": e.call.symbol, "why": e.why})
        del self.history[:-50]
        return out

    def on_chain(self, chain: Chain, now: float) -> DayCall | None:
        """A chain read of a called company: price its picks (the first read of a call, or the
        read after its zone event). Returns the call when there is something to send."""
        c = self.calls.get(chain.symbol)
        if c is None:
            return None
        if chain.price:
            c.observe(chain.price)
        c.picks = pick_contracts(chain, c, now, self.cfg)
        for p in c.picks:
            self.tapes.setdefault(p.occ, VolumeTape()).add(now, p.volume)
        if not c.read:
            c.read = True
            return c
        return None

    def volume_state(self, occ: str) -> str:
        t = self.tapes.get(occ)
        return t.state() if t is not None else "too early to tell"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.daymove,
            "day": self.day,
            "calls_today": self.made_today,
            "calls": [c.to_dict() for c in self.calls.values()],
            "events": list(reversed(self.history[-20:])),
        }


def caps_of(listings: Any, min_cap: float) -> dict[str, float]:
    return {li.symbol: float(li.market_cap) for li in listings if (li.market_cap or 0) >= min_cap}


__all__ = [
    "DayCall",
    "DayMoveDesk",
    "DayMoveEvent",
    "Pick",
    "VolumeTape",
    "caps_of",
    "day_call",
    "pick_contracts",
]
