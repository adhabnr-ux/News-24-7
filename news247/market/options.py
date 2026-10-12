"""The options tape: unusual volume and contracts up thousands of percent, on every $1B+ company.

Options are where the informed money often shows up first: a block of short-dated calls bought
at the ask the day before a deal, a cheap put going from a nickel to five dollars in an hour.
This module reads the full option chain of every listed US company worth at least
``options.min_market_cap`` (default $1B, up to the largest), and flags two things:

* **Spikes**: a contract trading today at 1,000%+ of its previous close (configurable). The
  study of option "lottery ticket" alerts found that most screenshots showing thousands of
  percent were measured from a stale $0.01 prior print, so every spike is checked twice
  before it pushes:

  - *bid-confirmed*: the bid now (what you could sell at) is still a large multiple of the
    previous close, so the move is not one print in an empty book;
  - *real base*: the previous close is not far below what the contract was worth that day
    (Black-Scholes at yesterday's stock price). A $0.01 "close" on a contract worth $0.50 is a
    stale print; the move is then re-measured from the model value and stated honestly.

  A spike that fails either check still shows in the app, with the reason, but does not push.

* **Unusual volume**: contracts trading far more than their open interest with real money
  behind them (contracts x price x 100), summed per company; the premium, the share of
  calls vs puts, and whether the last print was near the ask (likely bought) are stated.

Data: Cboe's public delayed-quotes JSON (``cdn.cboe.com/api/global/delayed_quotes/options/
<SYMBOL>.json``), one request per company for every expiry and strike: bid, ask, last, the
contract's previous close, volume, open interest, implied vol and the last trade time. It is
about 15 minutes delayed and every alert says so. Free and keyless, but it is not a documented
API, so the feed paces itself, backs off on errors, and reports its health on /api/status.

The scan runs in the regular session. Companies moving today, flagged by the radar, or in a
news alert are re-read every few minutes; every other company rotates through, largest first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..config import OptionsConfig
from ..http import HttpClient, HTTPError
from ..sources.base import SourceHealth, _sleep_or_stop
from .live import LiveQuotes, OptionStream, Watch, parse_stock_snapshots, pick_watch
from .universe import SCREENER_HEADERS, Listing, Universe, band, fmt_cap

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

CBOE_HEADERS = {
    "User-Agent": SCREENER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.cboe.com",
    "Referer": "https://www.cboe.com/",
}
# OCC symbol: root, yymmdd, C/P, strike x 1000 (8 digits). Roots with a digit (AAPL1) are
# adjusted contracts after a split or merger: their deliverable is not 100 shares, so their
# prices are not comparable and they are skipped.
OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<k>\d{8})$")
SPIKE_LADDER = (1.0, 2.0, 5.0, 10.0, 25.0)  # rungs as multiples of spike_min_pct: 1000, 2000, 5000% ...
RISK_FREE = 0.04
YEAR_DAYS = 365.0
NO_CHAIN_TTL_S = 24 * 3600  # a company with no listed options is retried the next day


# --------------------------------------------------------------------------- pricing


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(s: float, k: float, t: float, sigma: float, cp: str = "C", r: float = RISK_FREE) -> float:
    """Black-Scholes price of a European call ("C") or put ("P"); t in years.
    At or past expiry, or with no volatility, it is the intrinsic value."""
    if s <= 0 or k <= 0:
        return 0.0
    if t <= 0 or sigma <= 0:
        return max(s - k, 0.0) if cp == "C" else max(k - s, 0.0)
    sq = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / sq
    d2 = d1 - sq
    if cp == "C":
        return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)


def as_vol(v: Any) -> float | None:
    """Volatility as a fraction. Cboe quotes some as 0.25 and others as 25.0; an annual vol
    above 300% is read as a percentage."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x <= 0 or math.isnan(x):
        return None
    return x / 100.0 if x > 3.0 else x


def prev_session(d: date) -> date:
    """The weekday before ``d`` (exchange holidays are not modelled: one day of extra time
    value on a holiday Monday is well inside the stale-base margin)."""
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _cents(x: float) -> float:
    """Money differences to the cent: 0.90 - 0.80 is $0.10, not 0.0999..."""
    return round(x, 4)


def years_left(expiry: date, now: float) -> float:
    """Time to the 16:00 ET close on expiry day, in years (at least an hour): a 4-day option's
    value depends on the hours, not just the days."""
    end = datetime(expiry.year, expiry.month, expiry.day, 16, tzinfo=ET).timestamp()
    return max(end - now, 3600.0) / (YEAR_DAYS * 86400)


def implied_vol(price: float, s: float, k: float, t: float, cp: str) -> float | None:
    """The volatility that prices the option at ``price`` (bisection, 1% to 500%). None when the
    price is outside what any volatility can give (below intrinsic, above the stock)."""
    if price <= 0 or s <= 0 or k <= 0 or t <= 0:
        return None
    lo, hi = 0.01, 5.0
    if price < bs_price(s, k, t, lo, cp) - 1e-4 or price > bs_price(s, k, t, hi, cp):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(s, k, t, mid, cp) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    return (lo + hi) / 2


# --------------------------------------------------------------------------- the chain


@dataclass
class Contract:
    symbol: str  # OCC, e.g. NWE261016C00075000
    underlying: str
    expiry: date
    cp: str  # "C" | "P"
    strike: float
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    prev_close: float | None = None
    volume: int = 0
    open_interest: int | None = None  # None: the feed did not say (never read as 0)
    iv: float | None = None
    last_trade: datetime | None = None  # ET
    quote_time: datetime | None = None  # ET, when the bid/ask was true (None: the feed did not say)
    bid_size: int | None = None  # contracts on the bid / offer (None: the feed did not say)
    ask_size: int | None = None

    @property
    def kind(self) -> str:
        return "call" if self.cp == "C" else "put"

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2
        return None

    @property
    def price(self) -> float:
        """What a contract changed hands at: the last print, else the mid."""
        return self.last or self.mid or 0.0

    @property
    def premium(self) -> float:
        """Dollars traded today: contracts x price x 100 shares."""
        return self.volume * self.price * 100.0

    @property
    def move_pct(self) -> float | None:
        if self.last and self.prev_close and self.prev_close > 0:
            return (self.last / self.prev_close - 1.0) * 100.0
        return None

    @property
    def bid_move_pct(self) -> float | None:
        if self.bid and self.prev_close and self.prev_close > 0:
            return (self.bid / self.prev_close - 1.0) * 100.0
        return None

    def dte(self, today: date) -> int:
        return (self.expiry - today).days

    def label(self) -> str:
        k = f"{self.strike:,.2f}".rstrip("0").rstrip(".")
        return f"{self.underlying} ${k} {self.kind} ({self.expiry:%b} {self.expiry.day})"

    def side(self) -> str:
        """Where the last print sat in the spread: an estimate of who was in a hurry."""
        if not (self.last and self.bid is not None and self.ask and self.ask > (self.bid or 0)):
            return ""
        spread = self.ask - (self.bid or 0.0)
        if self.last >= self.ask - 0.25 * spread:
            return "near the ask (likely bought)"
        if self.last <= (self.bid or 0.0) + 0.25 * spread:
            return "near the bid (likely sold)"
        return "mid-spread"


@dataclass
class Chain:
    symbol: str
    price: float | None = None  # underlying, last
    prev_close: float | None = None
    change_pct: float | None = None
    iv30: float | None = None  # fraction
    quote_time: datetime | None = None  # ET, underlying's last trade
    contracts: list[Contract] = field(default_factory=list)
    skipped: int = 0  # adjusted / unparseable contracts
    source: str = "Cboe delayed quotes (~15 min)"  # shown on every alert
    prev_total_volume: int | None = None  # all contracts, previous session (when the source says)
    stock_source: str = ""  # where the stock's price came from ("" = the option source / universe)
    stock_time: datetime | None = None  # ET, when that stock price was true

    @property
    def stock_prev(self) -> float | None:
        if self.prev_close:
            return self.prev_close
        if self.price and self.change_pct is not None and self.change_pct > -100:
            return self.price / (1 + self.change_pct / 100)
        return None


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _int(v: Any) -> int:
    x = _num(v)
    return int(x) if x is not None and x > 0 else 0


def _oint(v: Any) -> int | None:
    """An integer the feed reported (0 included), or None when it reported nothing."""
    x = _num(v)
    return max(int(x), 0) if x is not None else None


def _ts(v: Any) -> datetime | None:
    """Cboe's '2026-10-07T11:42:13' (exchange-local, read as Eastern)."""
    if not v or not isinstance(v, str):
        return None
    try:  # fromisoformat is C-fast; strptime cost ~0.2 s on an 8,000-contract chain
        return datetime.fromisoformat(v[:19]).replace(tzinfo=ET)
    except ValueError:
        return None


def parse_occ(sym: str) -> tuple[str, date, str, float] | None:
    m = OCC_RE.match(sym or "")
    if not m:
        return None
    ymd = m["ymd"]
    try:
        exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    return m["root"], exp, m["cp"], int(m["k"]) / 1000.0


def parse_chain(doc: dict[str, Any], symbol: str) -> Chain:
    """Cboe delayed-quotes JSON -> Chain. Unknown or missing fields degrade to None, never to
    an invented number."""
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        raise ValueError("no 'data' object in the Cboe answer")
    chain = Chain(
        symbol=symbol,
        price=_num(data.get("current_price")) or _num(data.get("close")),
        prev_close=_num(data.get("prev_day_close")),
        change_pct=_num(data.get("percent_change")),
        iv30=as_vol(data.get("iv30")),
        quote_time=_ts(data.get("last_trade_time")) or _ts(doc.get("timestamp")),
    )
    root = symbol.replace("-", "").replace(".", "").replace("/", "").upper()
    for row in data.get("options") or []:
        if not isinstance(row, dict):
            continue
        occ = parse_occ(str(row.get("option") or ""))
        if occ is None or occ[0] != root:
            chain.skipped += 1
            continue
        _, exp, cp, strike = occ
        chain.contracts.append(
            Contract(
                symbol=str(row["option"]),
                underlying=symbol,
                expiry=exp,
                cp=cp,
                strike=strike,
                bid=_num(row.get("bid")),
                ask=_num(row.get("ask")),
                last=_num(row.get("last_trade_price")),
                prev_close=_num(row.get("prev_day_close")),
                volume=_int(row.get("volume")),
                open_interest=_oint(row.get("open_interest")),
                iv=as_vol(row.get("iv")),
                last_trade=_ts(row.get("last_trade_time")),
                bid_size=_oint(row.get("bid_size")),
                ask_size=_oint(row.get("ask_size")),
            )
        )
    return chain


# --------------------------------------------------------------------------- the detector


@dataclass
class SpikeRow:
    contract: Contract
    move_pct: float  # last vs previous close (what a broker's "% today" shows)
    bid_move_pct: float | None  # bid now vs the honest base
    confirmed: bool  # the bid still holds most of the move
    fair_prev: float | None  # model value at yesterday's close
    stale_base: bool  # the previous close is far below that model value
    honest_move_pct: float  # last vs the honest base: max(previous close, model value) when stale
    big_enough: bool = True  # the honest move still clears the threshold (higher on expiry day)
    same_day: bool = False  # expires today: 0DTE contracts need spike_0dte_multiple x the threshold to push
    no_prior: bool = False  # no previous close: measured from the model value (min $0.01)
    quote_led: bool = False  # measured on the live bid: the trades have not printed yet (feed delay)

    @property
    def real(self) -> bool:
        """Bid-confirmed, and thousands of percent even when measured from a fair base."""
        return self.confirmed and self.big_enough

    def to_dict(self, today: date) -> dict[str, Any]:
        c = self.contract
        return {
            "contract": c.symbol,
            "label": c.label(),
            "type": c.kind,
            "strike": c.strike,
            "expiry": c.expiry.isoformat(),
            "dte": c.dte(today),
            "prev_close": c.prev_close,
            "last": c.last,
            "bid": c.bid,
            "ask": c.ask,
            "volume": c.volume,
            "open_interest": c.open_interest,
            "premium": round(c.premium),
            "move_pct": round(self.move_pct, 1),
            "bid_move_pct": round(self.bid_move_pct, 1) if self.bid_move_pct is not None else None,
            "confirmed": self.confirmed,
            "fair_prev": round(self.fair_prev, 4) if self.fair_prev is not None else None,
            "stale_base": self.stale_base,
            "honest_move_pct": round(self.honest_move_pct, 1),
            "same_day": self.same_day,
            "no_prior": self.no_prior,
            "quote_led": self.quote_led,
            "last_trade": c.last_trade.isoformat() if c.last_trade else None,
            "quote_time": c.quote_time.isoformat() if c.quote_time else None,
        }


@dataclass
class FlowRow:
    contract: Contract
    vol_oi: float | None  # volume / open interest (None when OI is 0)

    def to_dict(self, today: date) -> dict[str, Any]:
        c = self.contract
        return {
            "contract": c.symbol,
            "label": c.label(),
            "type": c.kind,
            "strike": c.strike,
            "expiry": c.expiry.isoformat(),
            "dte": c.dte(today),
            "volume": c.volume,
            "open_interest": c.open_interest,
            "vol_oi": round(self.vol_oi, 1) if self.vol_oi is not None else None,
            "price": c.price,
            "bid": c.bid,
            "ask": c.ask,
            "premium": round(c.premium),
            "side": c.side(),
        }


MISPRINT_LABEL = {
    "vertical": "strike-order arbitrage",
    "calendar": "calendar arbitrage",
    "intrinsic": "below intrinsic value",
    "cheap_ask": "offered far below its value",
    "rich_bid": "bid far above its value",
    "print": "traded far from its value",
}


@dataclass
class MisprintRow:
    kind: str  # vertical | calendar | intrinsic | cheap_ask | rich_bid | print
    contract: Contract  # the mispriced contract (for arbitrage: the leg you buy)
    price: float  # the misprinted quote or trade
    edge: float  # $ a share in your favor: fair - ask, bid - fair, bid - ask, intrinsic - ask
    fair: float | None = None  # value now (value misprints)
    ref: str = ""  # how the value was found
    other: Contract | None = None  # arbitrage: the leg you sell
    other_price: float | None = None
    live: bool = True  # judged on live quotes (executable now)

    @property
    def arbitrage(self) -> bool:
        return self.kind in ("vertical", "calendar", "intrinsic")

    def to_dict(self, today: date) -> dict[str, Any]:
        c = self.contract
        d: dict[str, Any] = {
            "kind": self.kind,
            "what": MISPRINT_LABEL.get(self.kind, self.kind),
            "contract": c.symbol,
            "label": c.label(),
            "dte": c.dte(today),
            "price": self.price,
            "edge": round(self.edge, 4),
            "fair": round(self.fair, 4) if self.fair is not None else None,
            "ref": self.ref,
            "bid": c.bid,
            "ask": c.ask,
            "bid_size": c.bid_size,
            "ask_size": c.ask_size,
            "last": c.last,
            "last_trade": c.last_trade.isoformat() if c.last_trade else None,
            "quote_time": c.quote_time.isoformat() if c.quote_time else None,
            "live": self.live,
        }
        if self.other is not None:
            d.update(other=self.other.symbol, other_label=self.other.label(), other_price=self.other_price)
        return d


@dataclass
class OptionsHit:
    symbol: str
    kind: str  # "spike" | "flow"
    severity: str
    chain: Chain
    listing: Listing | None
    spikes: list[SpikeRow] = field(default_factory=list)
    flow: list[FlowRow] = field(default_factory=list)
    misprints: list[MisprintRow] = field(default_factory=list)
    rung: float = 0.0  # spike: the % rung crossed
    flow_premium: float = 0.0
    call_premium: float = 0.0
    put_premium: float = 0.0
    reasons: list[str] = field(default_factory=list)  # why a spike did not push
    detected: float = field(default_factory=time.time)
    volume_today: int = 0  # surge: all contracts today
    volume_prev: int = 0  # surge: all contracts in the previous session
    budget: str = "options"  # which daily push budget a HIGH spent: "options" | "misprint"

    @property
    def market_cap(self) -> float | None:
        return self.listing.market_cap if self.listing else None

    def to_dict(self) -> dict[str, Any]:
        today = datetime.fromtimestamp(self.detected, ET).date()
        ch = self.chain
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "severity": self.severity,
            "name": self.listing.name if self.listing else "",
            "market_cap": self.market_cap,
            "cap": fmt_cap(self.market_cap),
            "band": band(self.market_cap),
            "stock_price": ch.price,
            "stock_change_pct": round(ch.change_pct, 2) if ch.change_pct is not None else None,
            "iv30": round(ch.iv30, 4) if ch.iv30 else None,
            "quote_time": ch.quote_time.isoformat() if ch.quote_time else None,
            "rung": self.rung,
            "spikes": [r.to_dict(today) for r in self.spikes],
            "flow": [r.to_dict(today) for r in self.flow],
            "misprints": [r.to_dict(today) for r in self.misprints],
            "flow_premium": round(self.flow_premium),
            "call_premium": round(self.call_premium),
            "put_premium": round(self.put_premium),
            "reasons": list(self.reasons),
            "volume_today": self.volume_today,
            "volume_prev": self.volume_prev,
            "detected": self.detected,
            "source": self.chain.source,
        }


@dataclass
class _DayState:
    day: str = ""
    spike_any: float = 0.0  # highest rung shown today
    spike_real: float = 0.0  # highest bid-confirmed, real-base rung shown today
    flow_premium: float = 0.0  # unusual premium at the last flow alert
    spike_pushed: float = 0.0  # rung of the last spike push today (the next needs 10x that)
    surge_volume: float = 0.0  # company option volume at the last surge alert


class OptionsRadar:
    """Chains in, hits out. Pure logic (no I/O), so every rule is unit-tested."""

    def __init__(self, cfg: OptionsConfig, state_path: Path | None = None) -> None:
        self.cfg = cfg
        self.state: dict[str, _DayState] = {}
        self.pushes: dict[str, int] = {}  # ET date -> options pushes sent
        self.board: dict[str, dict[str, Any]] = {}  # latest hit per company, for /api/options
        self.totals: dict[str, tuple[str, int]] = {}  # symbol -> (ET day, most option volume seen)
        self.totals_prev: dict[str, tuple[str, int]] = {}  # the same for the session before
        self.chains_scanned = 0
        self.contracts_scanned = 0
        self.hits_total = 0
        self.misprint_seen: dict[str, float] = {}  # "day|kind|contract" -> biggest edge shown
        self.misprint_pushes: dict[str, int] = {}  # ET date -> misprint pushes sent
        self.state_path = state_path
        self._load()

    # ------------------------------------------------------------------ persistence

    @staticmethod
    def _day(now: float) -> str:
        return datetime.fromtimestamp(now, ET).date().isoformat()

    def _load(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            doc = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        day = str(doc.get("day", ""))
        for sym, v in (doc.get("fired") or {}).items():
            try:
                self.state[sym] = _DayState(day, *(float(x) for x in list(v)[:5]))
            except (TypeError, ValueError, IndexError):
                continue
        self.pushes = {k: int(v) for k, v in (doc.get("pushes") or {}).items() if k == day}

    def save(self, now: float | None = None) -> None:
        if not self.state_path:
            return
        day = self._day(now or time.time())
        fired = {
            s: [st.spike_any, st.spike_real, st.flow_premium, st.spike_pushed, st.surge_volume]
            for s, st in self.state.items()
            if st.day == day and (st.spike_any or st.flow_premium or st.surge_volume)
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"day": day, "fired": fired, "pushes": {day: self.pushes.get(day, 0)}})
            )
        except OSError as exc:
            log.warning("options state not saved: %s", exc)

    # ------------------------------------------------------------------ rules

    def _push_ok(self, day: str) -> bool:
        if self.pushes.get(day, 0) >= self.cfg.daily_pushes:
            return False
        self.pushes[day] = self.pushes.get(day, 0) + 1
        return True

    def live_bid(self, c: Contract, now: float) -> float | None:
        """The bid when the contract's quote is fresh (``live_quote_s``): what it could be sold at
        right now. None when the feed gave no quote time (Cboe: everything ~15 min late)."""
        if not c.bid or c.quote_time is None:
            return None
        age = now - c.quote_time.timestamp()
        return c.bid if -60 <= age <= self.cfg.live_quote_s else None

    def spike_row(self, c: Contract, chain: Chain, today: date, now: float | None = None) -> SpikeRow | None:
        """A contract up ``spike_min_pct``+ today, on its prints with real volume behind them, or
        on its live bid when the feed's trades lag its quotes (Alpaca's free feed: 15 minutes).
        The bid is what the contract could be sold at now, so a quote-led spike is bid-confirmed by
        construction and needs no printed volume yet."""
        cfg = self.cfg
        now = now or time.time()
        move = c.move_pct
        no_prior = False
        if move is None and c.last and not c.prev_close:
            # never traded before today: brokers show "% today" from a ~$0.01 closing mark; we
            # measure from the contract's model value at yesterday's close (at least $0.01)
            fair0 = self.fair_prev(c, chain, today)
            if fair0 is None:
                return None
            c.prev_close = max(round(fair0, 2), 0.01)
            move, no_prior = c.move_pct, True
        traded_today = c.last_trade is not None and c.last_trade.date() == today
        if c.last_trade is not None and not traded_today:
            move = None  # the "last" is an old print: nothing traded today
        live = self.live_bid(c, now)
        quote_led = False
        if live is not None and c.prev_close and c.prev_close > 0 and not no_prior:
            live_move = (live / c.prev_close - 1.0) * 100.0
            if live_move >= cfg.spike_min_pct and (move is None or live_move > move):
                move, quote_led = live_move, True
        if move is None or move < cfg.spike_min_pct:
            return None
        if quote_led:
            if (live or 0) < cfg.spike_min_price:
                return None
        else:
            if c.volume < cfg.spike_min_volume or (c.last or 0) < cfg.spike_min_price:
                return None
            if c.volume * (c.last or 0) * 100 < cfg.spike_min_premium:
                return None
        fair = self.fair_prev(c, chain, today)
        prev = c.prev_close or 0.0
        stale = fair is not None and fair >= cfg.stale_min_fair and prev < cfg.stale_base_ratio * fair
        base = max(prev, fair or 0.0) if stale else prev
        px = live if quote_led else c.last
        honest = (px / base - 1.0) * 100.0 if base > 0 and px else move
        bid_move = (c.bid / base - 1.0) * 100.0 if c.bid and base > 0 else None
        confirmed = bid_move is not None and bid_move >= cfg.spike_min_pct * cfg.spike_confirm_fraction
        same_day = c.expiry == today
        need = cfg.spike_min_pct * (cfg.spike_0dte_multiple if same_day else 1.0)
        return SpikeRow(
            c, move, bid_move, confirmed, fair, stale, honest, honest >= need, same_day, no_prior, quote_led
        )

    def fair_prev(self, c: Contract, chain: Chain, today: date) -> float | None:
        """Model value of the contract at yesterday's close: yesterday's stock price, the time
        left then, and the company's 30-day implied vol (else the contract's own)."""
        s = chain.stock_prev
        sigma = chain.iv30 or c.iv
        if not s or not sigma:
            return None
        t = max((c.expiry - prev_session(today)).days, 0) / YEAR_DAYS
        return bs_price(s, c.strike, t, sigma, c.cp)

    def flow_row(self, c: Contract) -> FlowRow | None:
        cfg = self.cfg
        if c.volume < cfg.flow_min_volume or c.premium < cfg.flow_min_contract_premium:
            return None
        oi = c.open_interest
        if oi is None:
            return None  # unusual against what? without open interest there is no baseline
        if oi > 0 and c.volume < cfg.flow_oi_multiple * oi:
            return None
        return FlowRow(c, c.volume / oi if oi > 0 else None)

    # ------------------------------------------------------------------ scan

    def scan(
        self, chain: Chain, listing: Listing | None = None, now: float | None = None
    ) -> list[OptionsHit]:
        now = now or time.time()
        day = self._day(now)
        today = datetime.fromtimestamp(now, ET).date()
        self.chains_scanned += 1
        self.contracts_scanned += len(chain.contracts)
        st = self.state.setdefault(chain.symbol, _DayState())
        if st.day != day:
            self.state[chain.symbol] = st = _DayState(day=day)
        hits: list[OptionsHit] = []

        live = [c for c in chain.contracts if c.expiry >= today]
        spikes = [r for r in (self.spike_row(c, chain, today, now) for c in live) if r is not None]
        if spikes:
            hit = self._spike_hit(chain, listing, spikes, st, day, now)
            if hit is not None:
                hits.append(hit)

        flow = [r for r in (self.flow_row(c) for c in live) if r is not None]
        if flow:
            hit = self._flow_hit(chain, listing, flow, st, day, now)
            if hit is not None:
                hits.append(hit)

        surge = self._surge_hit(chain, listing, live, st, day, now)
        if surge is not None:
            hits.append(surge)

        misprint = self._misprint_hit(chain, listing, self.misprint_rows(chain, today, now), day, now)
        if misprint is not None:
            hits.append(misprint)

        # one push per company per scan: the first HIGH (spike, then flow, then surge) carries
        # it; the others still show in the app, and their push is handed back to the budget
        pushed = False
        for h in hits:
            if h.severity == "HIGH":
                if pushed:
                    h.severity = "MEDIUM"
                    h.reasons.append("pushed together with this company's other options alert")
                    budget = self.misprint_pushes if h.budget == "misprint" else self.pushes
                    budget[day] = max(0, budget.get(day, 0) - 1)
                pushed = True

        for h in hits:
            self.board[chain.symbol] = h.to_dict()
        if hits and len(self.board) > 500:  # the app shows today's hits; keep memory bounded
            for sym, _ in sorted(self.board.items(), key=lambda kv: kv[1].get("detected", 0))[:100]:
                del self.board[sym]
        self.hits_total += len(hits)
        if hits:
            self.save(now)
        return hits

    # ------------------------------------------------------------------ misprints

    def _quote_live(self, c: Contract, now: float) -> bool:
        if c.quote_time is None:
            return False
        age = now - c.quote_time.timestamp()
        return -60 <= age <= self.cfg.live_quote_s

    def _stock_live(self, chain: Chain, now: float) -> bool:
        return (
            chain.stock_time is not None
            and now - chain.stock_time.timestamp() <= self.cfg.stock_quote_max_age_s
        )

    def _curve(self, chain: Chain, exp: date, cp: str, s: float, now: float) -> list[tuple[float, float]]:
        """(strike, implied vol) for this expiry and type, from contracts with a tight live market
        (spread at most 35% of the mid, mid $0.05+), strikes within 30% of the stock."""
        t = years_left(exp, now)
        pts = []
        for c in chain.contracts:
            if c.expiry != exp or c.cp != cp or not (c.bid and c.ask and c.ask >= c.bid > 0):
                continue
            mid = (c.bid + c.ask) / 2
            if mid < 0.05 or (c.ask - c.bid) > 0.35 * mid or abs(c.strike / s - 1) > 0.30:
                continue
            if not self._quote_live(c, now) and c.quote_time is not None:
                continue
            iv = implied_vol(mid, s, c.strike, t, cp)
            if iv is not None:
                pts.append((c.strike, iv))
        pts.sort()
        return pts

    def _fair(
        self,
        c: Contract,
        chain: Chain,
        s: float,
        now: float,
        curves: dict[tuple[date, str], list[tuple[float, float]]],
    ) -> tuple[float | None, str]:
        """What the contract is worth now: Black-Scholes at the stock price with the implied vol of
        its neighboring strikes (interpolated; the contract's own quote is never used). None when
        the neighbors don't trade tightly enough to say: a company-wide IV misses the skew and a
        day's repricing, and would call ordinary contracts misprinted."""
        key = (c.expiry, c.cp)
        if key not in curves:
            curves[key] = self._curve(chain, c.expiry, c.cp, s, now)
        pts = [(k, v) for k, v in curves[key] if k != c.strike]
        below = [(k, v) for k, v in pts if k < c.strike and c.strike - k <= 0.15 * s]
        above = [(k, v) for k, v in pts if k > c.strike and k - c.strike <= 0.15 * s]
        iv: float | None = None
        ref = ""
        if below and above:
            (k1, v1), (k2, v2) = below[-1], above[0]
            iv = v1 + (v2 - v1) * (c.strike - k1) / (k2 - k1)
            ref = f"the implied vol of its neighboring strikes (${k1:g} and ${k2:g}: {iv * 100:.0f}%)"
        elif below or above:
            k1, v1 = below[-1] if below else above[0]
            if abs(k1 - c.strike) <= 0.075 * s:
                iv, ref = v1, f"the implied vol of the nearest strike (${k1:g}: {v1 * 100:.0f}%)"
        if iv is None:
            return None, ""
        return bs_price(s, c.strike, years_left(c.expiry, now), iv, c.cp), ref

    def misprint_rows(self, chain: Chain, today: date, now: float) -> list[MisprintRow]:
        """Quotes and prints far from what a contract is worth. Arbitrage (model-free: strike
        order, calendar order, intrinsic value) and value misprints are judged on live quotes
        only, so every one is something you could act on now; misprinted trades are shown so you
        know the chain is thin enough for resting orders to get filled far from value."""
        cfg = self.cfg
        if not cfg.misprints:
            return []
        live = [c for c in chain.contracts if 0 <= (c.expiry - today).days <= cfg.misprint_max_dte]
        quoted = [c for c in live if self._quote_live(c, now) and c.bid and c.ask and c.ask >= c.bid > 0]
        asks = [c for c in live if self._quote_live(c, now) and c.ask and c.ask > 0]
        tol = cfg.misprint_min_edge
        rows: list[MisprintRow] = []

        # 1. strike order: a lower-strike call (higher-strike put) is never worth less
        groups: dict[tuple[date, str], list[Contract]] = {}
        for c in quoted:
            groups.setdefault((c.expiry, c.cp), []).append(c)
        for (_, cp), cs in groups.items():
            cs.sort(key=lambda c: c.strike, reverse=(cp == "C"))  # walk from the cheap side
            best: Contract | None = None
            for c in cs:
                if best is not None and _cents((best.bid or 0) - (c.ask or 0)) >= tol:
                    rows.append(
                        MisprintRow(
                            "vertical",
                            c,
                            c.ask or 0.0,
                            _cents((best.bid or 0) - (c.ask or 0)),
                            ref="a lower-strike call (or higher-strike put) is always worth at least the other",
                            other=best,
                            other_price=best.bid,
                        )
                    )
                if best is None or (c.bid or 0) > (best.bid or 0):
                    best = c

        # 2. calendar order: a later expiry (American option) is never worth less
        by_strike: dict[tuple[str, float], list[Contract]] = {}
        for c in quoted:
            by_strike.setdefault((c.cp, c.strike), []).append(c)
        for cs in by_strike.values():
            cs.sort(key=lambda c: c.expiry)
            best = None
            for c in cs:
                if best is not None and _cents((best.bid or 0) - (c.ask or 0)) >= tol:
                    rows.append(
                        MisprintRow(
                            "calendar",
                            c,
                            c.ask or 0.0,
                            _cents((best.bid or 0) - (c.ask or 0)),
                            ref="a later expiry is always worth at least an earlier one",
                            other=best,
                            other_price=best.bid,
                        )
                    )
                if best is None or (c.bid or 0) > (best.bid or 0):
                    best = c

        s_live = chain.price if chain.price and self._stock_live(chain, now) else None
        # 3. intrinsic value: an American option is never worth less than exercising it
        if s_live:
            for c in asks:
                intrinsic = s_live - c.strike if c.cp == "C" else c.strike - s_live
                if _cents(intrinsic - (c.ask or 0)) >= tol:
                    rows.append(
                        MisprintRow(
                            "intrinsic",
                            c,
                            c.ask or 0.0,
                            _cents(intrinsic - (c.ask or 0)),
                            fair=intrinsic,
                            ref=f"intrinsic value at the live stock price ${s_live:,.2f}",
                        )
                    )

        # 4. value: an ask far below (or a bid far above) the contract's fair value now
        curves: dict[tuple[date, str], list[tuple[float, float]]] = {}
        taken = {r.contract.symbol for r in rows}
        if s_live:
            for c in asks:
                if c.symbol in taken:
                    continue
                cheap = (c.ask or 0) <= 0.75 * self._rough(c, chain, s_live, now)
                rich = bool(c.bid) and (c.bid or 0) >= 1.5 * self._rough(c, chain, s_live, now, low=True)
                if not (cheap or rich):
                    continue
                fair, ref = self._fair(c, chain, s_live, now, curves)
                if fair is None or fair < cfg.misprint_min_fair * (0.25 if rich else 1.0):
                    continue
                ask = c.ask or 0.0
                if (
                    fair >= cfg.misprint_min_fair
                    and ask <= cfg.misprint_ratio * fair
                    and fair - ask >= cfg.misprint_value_edge
                ):
                    rows.append(MisprintRow("cheap_ask", c, ask, fair - ask, fair=fair, ref=ref))
                    taken.add(c.symbol)
                elif (
                    c.bid
                    and c.bid >= 0.10
                    and c.bid >= cfg.misprint_rich_ratio * fair
                    and c.bid - fair >= cfg.misprint_value_edge
                ):
                    rows.append(MisprintRow("rich_bid", c, c.bid, c.bid - fair, fair=fair, ref=ref))
                    taken.add(c.symbol)

        # 5. a trade today far below value (informational): someone sold into a thin market and a
        # resting low bid got filled. Prints far *above* value are informed buying (spikes), not this.
        s_any = s_live or chain.price
        if s_any:
            for c in live:
                if c.symbol in taken or not c.last or c.last_trade is None or c.last_trade.date() != today:
                    continue
                if now - c.last_trade.timestamp() > cfg.misprint_print_minutes * 60:
                    continue
                tight = bool(
                    c.bid and c.ask and c.ask >= c.bid > 0 and (c.ask - c.bid) <= 0.35 * (c.ask + c.bid) / 2
                )
                if tight and (c.bid or 0) * 0.9 <= c.last:
                    continue  # a tight market that agrees with the print: nothing misprinted
                if c.last > cfg.misprint_print_ratio * self._rough(c, chain, s_any, now):
                    continue  # can't be far below value: skip the neighbor curve
                fair, ref = self._fair(c, chain, s_any, now, curves)
                if fair is None or fair < cfg.misprint_min_fair:
                    continue
                if c.last <= cfg.misprint_print_ratio * fair and fair - c.last >= cfg.misprint_value_edge:
                    rows.append(
                        MisprintRow("print", c, c.last, fair - c.last, fair=fair, ref=ref, live=False)
                    )
        order = {"vertical": 0, "calendar": 0, "intrinsic": 0, "cheap_ask": 1, "rich_bid": 2, "print": 3}
        rows.sort(key=lambda r: (order[r.kind], -r.edge))
        return rows

    def _rough(self, c: Contract, chain: Chain, s: float, now: float, low: bool = False) -> float:
        """A quick value bound to skip contracts that can't be misprinted before the costlier
        neighbor curve: Black-Scholes at a generous vol (the high bound) or a stingy one (low)."""
        base = chain.iv30 or c.iv or 0.5
        sigma = base * (0.5 if low else 2.5)
        return bs_price(s, c.strike, years_left(c.expiry, now), max(sigma, 0.05), c.cp)

    def _misprint_hit(
        self, chain: Chain, listing: Listing | None, rows: list[MisprintRow], day: str, now: float
    ) -> OptionsHit | None:
        cfg = self.cfg
        new: list[MisprintRow] = []
        for r in rows:
            key = f"{day}|{r.kind}|{r.contract.symbol}"
            seen = self.misprint_seen.get(key)
            if seen is not None and r.edge < 2 * seen:
                continue  # already shown today, and not twice as big
            self.misprint_seen[key] = r.edge
            new.append(r)
        if len(self.misprint_seen) > 20000:
            for k in [k for k in self.misprint_seen if not k.startswith(day)]:
                del self.misprint_seen[k]
        if not new:
            return None
        top = new[0]
        pushable = top.live and (
            (top.arbitrage and top.edge >= cfg.misprint_push_edge)
            or (
                top.kind == "cheap_ask"
                and top.edge >= 2 * cfg.misprint_push_edge
                and "neighboring" in top.ref
            )
        )
        severity, reasons = "MEDIUM", []
        if pushable:
            if self.misprint_pushes.get(day, 0) < cfg.misprint_daily_pushes:
                self.misprint_pushes[day] = self.misprint_pushes.get(day, 0) + 1
                severity = "HIGH"
            else:
                reasons.append("today's push limit for misprints is reached")
        if "indicative" in (chain.source or "").lower() and any(r.live for r in new):
            reasons.append(
                "these are Alpaca's indicative quotes (live but adjusted, not the exact exchange bid/ask): "
                "confirm the quote on your broker before trading"
            )
        if any(r.kind == "print" for r in new):
            reasons.append("a single print can be one leg of a spread or a late report: shown, never pushed")
        return OptionsHit(
            chain.symbol,
            "misprint",
            severity,
            chain,
            listing,
            misprints=new[: cfg.max_rows],
            reasons=reasons,
            detected=now,
            budget="misprint",
        )

    def prev_total(self, chain: Chain, today: date) -> int | None:
        """The company's option volume in the previous session: from the source when it says
        (Alpaca's previous daily bars), else what this process saw on its last session day."""
        if chain.prev_total_volume is not None:
            return chain.prev_total_volume
        seen = self.totals.get(chain.symbol)
        if seen and seen[0] == prev_session(today).isoformat():
            return seen[1]
        return None

    def _surge_hit(
        self,
        chain: Chain,
        listing: Listing | None,
        live: list[Contract],
        st: _DayState,
        day: str,
        now: float,
    ) -> OptionsHit | None:
        """The whole company's options lighting up: today's volume a multiple of yesterday's,
        with real money in it. This is the "unusual options activity" a scanner shows."""
        cfg = self.cfg
        today = datetime.fromtimestamp(now, ET).date()
        total = sum(c.volume for c in live)
        seen = self.totals.get(chain.symbol)
        if seen is None or seen[0] != day or total > seen[1]:
            if seen is not None and seen[0] != day:
                self.totals_prev[chain.symbol] = seen
            self.totals[chain.symbol] = (day, total)
        prev = self.prev_total(chain, today)
        if prev is None:
            back = self.totals_prev.get(chain.symbol)
            prev = back[1] if back and back[0] == prev_session(today).isoformat() else None
        if prev is None or total < cfg.surge_min_volume:
            return None
        ratio = total / max(prev, cfg.surge_floor_volume)
        if ratio < cfg.surge_multiple:
            return None
        if st.surge_volume and total < cfg.flow_refire_multiple * st.surge_volume:
            return None
        traded = [c for c in live if c.volume]
        premium = sum(c.premium for c in traded)
        calls = sum(c.premium for c in traded if c.cp == "C")
        big = ratio >= cfg.surge_push_multiple and premium >= cfg.surge_push_premium
        severity = "HIGH" if big and not st.surge_volume and self._push_ok(day) else "MEDIUM"
        st.surge_volume = total
        rows = [FlowRow(c, c.volume / c.open_interest if c.open_interest else None) for c in traded]
        rows.sort(key=lambda r: r.contract.premium, reverse=True)
        hit = OptionsHit(
            chain.symbol,
            "surge",
            severity,
            chain,
            listing,
            flow=rows[: cfg.max_rows],
            flow_premium=premium,
            call_premium=calls,
            put_premium=premium - calls,
            detected=now,
        )
        hit.volume_today, hit.volume_prev = total, prev
        return hit

    def _rung(self, pct: float) -> float:
        base = self.cfg.spike_min_pct
        return max((base * m for m in SPIKE_LADDER if pct >= base * m), default=0.0)

    def _spike_hit(
        self,
        chain: Chain,
        listing: Listing | None,
        rows: list[SpikeRow],
        st: _DayState,
        day: str,
        now: float,
    ) -> OptionsHit | None:
        rows.sort(key=lambda r: (r.real, r.honest_move_pct if r.real else r.move_pct), reverse=True)
        real = [r for r in rows if r.real and r.honest_move_pct >= self.cfg.spike_min_pct]
        rung_real = self._rung(max((r.honest_move_pct for r in real), default=0.0))
        rung_any = self._rung(max(r.move_pct for r in rows))
        if rung_real <= st.spike_real and rung_any <= st.spike_any:
            return None  # nothing bigger than what was already shown today
        severity = "MEDIUM"
        held = False
        if rung_real > st.spike_real:
            # one push per company per day, and again only at 10x that rung, so a single name
            # climbing 1,000 -> 2,000 -> 5,000% can't spend the whole day's push budget
            if st.spike_pushed and rung_real < 10 * st.spike_pushed:
                held = True
            elif self._push_ok(day):
                severity = "HIGH"
                st.spike_pushed = rung_real
        st.spike_real = max(st.spike_real, rung_real)
        st.spike_any = max(st.spike_any, rung_any)
        top = rows[0]
        reasons: list[str] = []
        if not top.real:
            if top.same_day and top.confirmed and not top.big_enough and not top.stale_base:
                need = self.cfg.spike_min_pct * self.cfg.spike_0dte_multiple
                reasons.append(
                    f"expires today: same-day contracts routinely multiply on ordinary moves, so they push "
                    f"from {need:,.0f}%"
                )
            if top.stale_base and not top.big_enough and top.fair_prev is not None:
                reasons.append(
                    f"stale base: the previous close (${(top.contract.prev_close or 0):.2f}) is far below "
                    f"the contract's model value that day (${top.fair_prev:.2f}); measured from that, "
                    f"{top.honest_move_pct:+,.0f}%"
                )
            if not top.confirmed:
                bid = top.contract.bid
                if bid is None:
                    reasons.append("unconfirmed: no bid in the data, so the last print may not be sellable")
                elif bid <= 0:
                    reasons.append("one-sided: the bid is empty, so the last print may not be sellable")
                else:
                    reasons.append(
                        f"unconfirmed: the bid (${bid:.2f}) is only {top.bid_move_pct or 0:+,.0f}% above the "
                        "base, so most of the move is the last print"
                    )
        elif held:
            reasons.append(
                f"already pushed today at {st.spike_pushed:,.0f}%; this company pushes again from "
                f"{10 * st.spike_pushed:,.0f}%"
            )
        elif severity == "MEDIUM":
            reasons.append("today's push limit for options alerts is reached")
        return OptionsHit(
            chain.symbol,
            "spike",
            severity,
            chain,
            listing,
            spikes=rows[: self.cfg.max_rows],
            rung=max(rung_real, rung_any),
            reasons=reasons,
            detected=now,
        )

    def _flow_hit(
        self,
        chain: Chain,
        listing: Listing | None,
        rows: list[FlowRow],
        st: _DayState,
        day: str,
        now: float,
    ) -> OptionsHit | None:
        cfg = self.cfg
        total = sum(r.contract.premium for r in rows)
        if total < cfg.flow_min_premium:
            return None
        if st.flow_premium and total < cfg.flow_refire_multiple * st.flow_premium:
            return None  # already shown; speak again only when it has grown a lot
        rows.sort(key=lambda r: r.contract.premium, reverse=True)
        calls = sum(r.contract.premium for r in rows if r.contract.cp == "C")
        top = rows[0]
        big = total >= cfg.flow_push_premium and (
            top.vol_oi is None or top.vol_oi >= cfg.flow_push_oi_multiple
        )
        severity = "HIGH" if big and not st.flow_premium and self._push_ok(day) else "MEDIUM"
        st.flow_premium = total
        return OptionsHit(
            chain.symbol,
            "flow",
            severity,
            chain,
            listing,
            flow=rows[: self.cfg.max_rows],
            flow_premium=total,
            call_premium=calls,
            put_premium=total - calls,
            detected=now,
        )

    def snapshot(self, limit: int = 40) -> list[dict[str, Any]]:
        return sorted(self.board.values(), key=lambda e: e.get("detected", 0), reverse=True)[:limit]


# --------------------------------------------------------------------------- the sources


class NoChain(Exception):
    """The source lists no options for this company."""


def _blank_stats() -> dict[str, Any]:
    return {"reads": 0, "errors": 0, "no_chain": 0, "last_error": "", "last_ok": None}


class CboeSource:
    """Cboe's public delayed-quotes JSON: one request per company, every expiry and strike."""

    name = "cboe"
    label = "Cboe delayed quotes (~15 min)"

    def __init__(self, cfg: OptionsConfig, http: HttpClient) -> None:
        self.cfg = cfg
        self.http = http
        self.interval_s = cfg.request_interval_s

    def url(self, symbol: str) -> str:
        return self.cfg.chain_url.replace("{symbol}", symbol.replace("-", ".").upper())

    async def fetch(self, symbol: str, listing: Listing | None, pace: Callable[[float], Any]) -> Chain:
        await pace(self.interval_s)
        try:
            resp = await self.http.get(self.url(symbol), headers=CBOE_HEADERS, timeout_s=30)
        except HTTPError as exc:
            if exc.status in (403, 404):  # Cboe's CDN answers 403 for a symbol it has no file for
                raise NoChain(symbol) from exc
            raise
        raw = resp.body
        if len(raw) > 256_000:  # a big chain is parsed off the event loop: news must not wait on it
            return await asyncio.to_thread(lambda: parse_chain(json.loads(raw), symbol))
        return parse_chain(json.loads(raw), symbol)


ALPACA_FEEDS = {
    "indicative": "Alpaca indicative feed (free: trades ~15 min delayed, quotes adjusted)",
    "opra": "Alpaca OPRA feed (real time)",
}


def _utc_ts(v: Any) -> datetime | None:
    """Alpaca's RFC 3339 UTC time ('2026-10-07T15:42:13.123456789Z') in Eastern time."""
    if not v or not isinstance(v, str):
        return None
    text = v.strip().replace("Z", "+00:00")
    if "." in text:  # nanoseconds: Python reads at most microseconds
        head, _, tail = text.partition(".")
        n = 0
        while n < len(tail) and tail[n].isdigit():  # the fraction stops where the zone starts
            n += 1
        digits, zone = tail[:n], tail[n:]
        text = f"{head}.{digits[:6].ljust(6, '0')}{zone}"
    try:
        return datetime.fromisoformat(text).astimezone(ET)
    except ValueError:
        return None


def parse_alpaca_chain(
    snapshots: dict[str, Any],
    symbol: str,
    listing: Listing | None,
    open_interest: dict[str, int] | None,
    today: date,
    source: str,
) -> Chain:
    """Alpaca option snapshots (keyed by OCC symbol) -> Chain.

    Per contract: ``latestQuote`` {bp, ap, t}, ``latestTrade`` {p, t}, ``dailyBar`` and
    ``prevDailyBar`` {c, v, t}, ``impliedVolatility``. Today's volume is the daily bar's when
    that bar is today's; the previous close is the bar before it (else the latest bar's close).
    The stock's price comes from the universe (Nasdaq, refreshed every 10 minutes in session);
    the company's 30-day implied vol is the median IV of near-the-money contracts 7-60 days out.
    """
    root = symbol.replace("-", "").replace(".", "").replace("/", "").upper()
    price = listing.price if listing else None
    change = listing.change_pct if listing else None
    chain = Chain(symbol=symbol, price=price, change_pct=change, source=source, stock_source="universe")
    newest: datetime | None = None
    near_iv: list[float] = []
    yday = prev_session(today)
    prev_total, prev_seen = 0, False
    for occ, snap in (snapshots or {}).items():
        parsed = parse_occ(str(occ))
        if parsed is None or parsed[0] != root or not isinstance(snap, dict):
            chain.skipped += 1
            continue
        _, exp, cp, strike = parsed
        quote = snap.get("latestQuote") or {}
        trade = snap.get("latestTrade") or {}
        bar = snap.get("dailyBar") or {}
        prev_bar = snap.get("prevDailyBar") or {}
        bar_day = _utc_ts(bar.get("t"))
        prev_day = _utc_ts(prev_bar.get("t"))
        if bar_day is not None and bar_day.date() == today:
            volume, prev_close = _int(bar.get("v")), _num(prev_bar.get("c"))
            if prev_day is not None and prev_day.date() == yday:
                prev_total += _int(prev_bar.get("v"))
        else:
            volume, prev_close = 0, _num(bar.get("c"))
            if bar_day is not None and bar_day.date() == yday:
                prev_total += _int(bar.get("v"))
        prev_seen = prev_seen or bar_day is not None
        q_time, t_time = _utc_ts(quote.get("t")), _utc_ts(trade.get("t"))
        for t in (q_time, t_time):
            if t is not None and (newest is None or t > newest):
                newest = t
        iv = as_vol(snap.get("impliedVolatility"))
        chain.contracts.append(
            Contract(
                symbol=str(occ),
                underlying=symbol,
                expiry=exp,
                cp=cp,
                strike=strike,
                bid=_num(quote.get("bp")),
                ask=_num(quote.get("ap")),
                last=_num(trade.get("p")),
                prev_close=prev_close,
                volume=volume,
                open_interest=open_interest.get(str(occ)) if open_interest is not None else None,
                iv=iv,
                last_trade=t_time,
                quote_time=q_time,
                bid_size=_oint(quote.get("bs")),
                ask_size=_oint(quote.get("as")),
            )
        )
        if iv and price and 7 <= (exp - today).days <= 60 and abs(strike / price - 1) <= 0.05:
            near_iv.append(iv)
    chain.quote_time = newest
    chain.prev_total_volume = prev_total if prev_seen else None
    if near_iv:
        near_iv.sort()
        chain.iv30 = near_iv[len(near_iv) // 2]
    return chain


class AlpacaSource:
    """Alpaca's options market data: the chain snapshot (quotes, trades, daily bars, IV) and,
    once a day per company, open interest from the option-contracts list. Needs API keys
    (a free paper-trading account works). Free accounts get the indicative feed."""

    name = "alpaca"

    def __init__(self, cfg: OptionsConfig, http: HttpClient) -> None:
        self.cfg = cfg
        self.http = http
        self.interval_s = cfg.alpaca_request_interval_s
        self.feed = cfg.alpaca_feed
        self.label = ALPACA_FEEDS.get(self.feed, "Alpaca")
        self._oi: dict[str, tuple[str, dict[str, int]]] = {}  # symbol -> (ET date, {occ: OI})
        self.oi_fail_run = 0  # open-interest reads failed in a row
        self.oi_last_error = ""

    @property
    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.cfg.alpaca_key,
            "APCA-API-SECRET-KEY": self.cfg.alpaca_secret,
            "Accept": "application/json",
        }

    async def _pages(
        self, url: str, params: dict[str, Any], key: str, pace: Callable[[float], Any]
    ) -> list[Any]:
        out: list[Any] = []
        token: str | None = None
        for _ in range(self.cfg.alpaca_max_pages):
            await pace(self.interval_s)
            q = dict(params, **({"page_token": token} if token else {}))
            resp = await self.http.get(url, headers=self.headers, params=q, timeout_s=30)
            doc = resp.json()
            if not isinstance(doc, dict):
                raise ValueError("unexpected Alpaca answer")
            out.append(doc.get(key))
            token = doc.get("next_page_token")
            if not token:
                break
        return out

    async def open_interest(
        self, symbol: str, today: date, pace: Callable[[float], Any]
    ) -> dict[str, int] | None:
        """Yesterday's open interest per contract (Alpaca updates it once a day), cached for the
        day. None when it cannot be read: unusual volume is then not judged for this chain."""
        day = today.isoformat()
        hit = self._oi.get(symbol)
        if hit and hit[0] == day:
            return hit[1]
        try:
            pages = await self._pages(
                self.cfg.alpaca_trading_url.rstrip("/") + "/v2/options/contracts",
                {"underlying_symbols": symbol.replace("-", "."), "expiration_date_gte": day, "limit": 10000},
                "option_contracts",
                pace,
            )
        except (HTTPError, aiohttp.ClientError, ValueError, asyncio.TimeoutError, OSError) as exc:
            log.info("alpaca open interest for %s unavailable: %s", symbol, exc)
            self.oi_fail_run += 1
            self.oi_last_error = f"{type(exc).__name__}: {exc}"[:200]
            return None
        self.oi_fail_run = 0
        oi: dict[str, int] = {}
        for page in pages:
            for row in page or []:
                v = _oint(row.get("open_interest")) if isinstance(row, dict) else None
                if v is not None and row.get("symbol"):
                    oi[str(row["symbol"])] = v
        self._oi[symbol] = (day, oi)
        if len(self._oi) > 5000:
            self._oi.pop(next(iter(self._oi)))
        return oi

    async def stock_snapshots(
        self, symbols: list[str], pace: Callable[[float], Any]
    ) -> dict[str, tuple[float, float | None, float]]:
        """Real-time stock prices for up to 100 companies a request (the free plan's IEX feed is
        live): {symbol: (price, previous close, epoch)}."""
        out: dict[str, tuple[float, float | None, float]] = {}
        today = datetime.now(ET).date()
        syms = [s.replace("-", ".").upper() for s in symbols]
        for i in range(0, len(syms), 100):
            await pace(self.interval_s)
            resp = await self.http.get(
                self.cfg.alpaca_data_url.rstrip("/") + "/v2/stocks/snapshots",
                headers=self.headers,
                params={"symbols": ",".join(syms[i : i + 100]), "feed": self.cfg.alpaca_stock_feed},
                timeout_s=20,
            )
            for sym, row in parse_stock_snapshots(resp.json(), today).items():
                out[sym.replace(".", "-")] = row
        return out

    async def fetch(self, symbol: str, listing: Listing | None, pace: Callable[[float], Any]) -> Chain:
        today = datetime.now(ET).date()
        params: dict[str, Any] = {"limit": 1000}
        if self.feed:
            params["feed"] = self.feed
        pages = await self._pages(
            self.cfg.alpaca_data_url.rstrip("/") + f"/v1beta1/options/snapshots/{symbol.replace('-', '.')}",
            params,
            "snapshots",
            pace,
        )
        snaps: dict[str, Any] = {}
        for page in pages:
            if isinstance(page, dict):
                snaps.update(page)
        if not snaps:
            raise NoChain(symbol)
        oi = await self.open_interest(symbol, today, pace)
        if len(snaps) > 1500:  # large chains parse off the event loop
            return await asyncio.to_thread(parse_alpaca_chain, snaps, symbol, listing, oi, today, self.label)
        return parse_alpaca_chain(snaps, symbol, listing, oi, today, self.label)


def build_sources(cfg: OptionsConfig, http: HttpClient) -> list[CboeSource | AlpacaSource]:
    """The primary source first, then the fallback (if ``fallback`` is on). ``auto`` prefers
    Alpaca when its keys are set (a keyed API with published terms), with Cboe behind it."""
    alpaca = AlpacaSource(cfg, http) if cfg.alpaca_key and cfg.alpaca_secret else None
    cboe = CboeSource(cfg, http)
    order: list[CboeSource | AlpacaSource]
    if cfg.provider == "cboe":
        order = [cboe] + ([alpaca] if alpaca else [])
    elif cfg.provider == "alpaca" and alpaca is None:
        raise ValueError("options.provider 'alpaca' needs ALPACA_KEY and ALPACA_SECRET")
    elif alpaca is not None:  # "alpaca", or "auto" with keys
        order = [alpaca, cboe]
    else:
        order = [cboe]
    return order if cfg.fallback else order[:1]


# --------------------------------------------------------------------------- the feed


class OptionsFeed:
    """Fetches chains on a priority schedule and runs them through the radar."""

    def __init__(
        self,
        cfg: OptionsConfig,
        http: HttpClient,
        universe: Universe,
        radar: OptionsRadar,
        market_phase: Callable[[float], str] | None = None,
        movers: Callable[[], Iterable[str]] | None = None,
        sources: list[Any] | None = None,
        live: LiveQuotes | None = None,
    ) -> None:
        self.cfg = cfg
        self.sources = sources if sources is not None else build_sources(cfg, http)
        # live: stock prices from the fastest source (radar sweep, Alpaca snapshots), the urgent
        # lane (news, radar, the stream) that skips the rotation, and the websocket tripwire
        self.live = live if live is not None else LiveQuotes()
        self._urgent: dict[str, str] = {}  # symbol -> why, waiting for the urgent lane
        self._urgent_read: dict[str, float] = {}  # symbol -> last urgent read
        self._urgent_wake: asyncio.Event | None = None
        self._urgent_sem: asyncio.Semaphore | None = None
        self.urgent_reads = 0
        self.urgent_why: dict[str, tuple[str, float]] = {}  # symbol -> (why, when): shown on its alert
        self.stock_reads = 0
        self.stock_errors = 0
        self.stock_swept = 0.0  # last full sweep of every company's live price
        self._moving: set[str] = set()  # moving hot_move_pct+ at the last sweep
        self.stream: OptionStream | None = None
        self._chains: dict[str, Chain] = {}  # the last chain of each company on the tripwire
        self._on_hits: Callable[[list[OptionsHit]], Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.stream_misprints = 0
        alpaca = next((x for x in self.sources if getattr(x, "name", "") == "alpaca"), None)
        if cfg.stream and alpaca is not None:
            self.stream = OptionStream(
                f"{cfg.alpaca_stream_url.rstrip('/')}/v1beta1/{cfg.alpaca_feed or 'indicative'}",
                cfg.alpaca_key,
                cfg.alpaca_secret,
                self._on_trigger,
                max_quotes=cfg.stream_max_quotes,
                is_open=lambda: self._phase(time.time()) == "open",
                session=lambda: getattr(http, "session", None),
                on_misprint=self._on_stream_misprint if cfg.misprints else None,
                misprint_ratio=cfg.misprint_ratio,
                rich_ratio=cfg.misprint_rich_ratio,
            )
        self.source_stats: dict[str, dict[str, Any]] = {src.name: _blank_stats() for src in self.sources}
        self.fallbacks = 0  # chains read from the fallback source after the primary failed
        self._big_nochain = 0  # $10B+ companies in a row a source said have no options
        self.movers = movers  # e.g. the price radar's live board: names moving right now
        self.http = http
        self.universe = universe
        self.radar = radar
        self.market_phase = market_phase
        self.health = SourceHealth()
        self.priority: dict[str, float] = {}  # symbol -> until (radar / news put it here)
        self.fetched: dict[str, float] = {}  # symbol -> last read
        self.no_chain: dict[str, float] = {}  # symbol -> when Cboe had no chain for it
        self.stale_chains = 0
        self._cursor = 0
        self._next_slot = 0.0
        self._backoff_until = 0.0
        self._stale_warned = ""
        self.sweeps = 0
        self._sem: asyncio.Semaphore | None = None
        self._newest_quote = 0.0  # newest stock quote time seen (epoch)
        self._stale_run = 0  # stale chains in a row
        self._problem_day = ""  # the ET day the feed last reported a problem

    # ------------------------------------------------------------------ what to read next

    def prioritize(self, symbols: Iterable[str], hold_s: float = 2 * 3600) -> None:
        """Companies in the news or on the radar are re-read every ``hot_seconds`` for a while."""
        until = time.time() + hold_s
        for s in symbols:
            s = (s or "").upper()
            if s and self.eligible(self.universe.get(s)):
                self.priority[s] = max(self.priority.get(s, 0.0), until)

    def eligible(self, li: Listing | None) -> bool:
        return bool(li and li.common and (li.market_cap or 0) >= self.cfg.min_market_cap)

    def companies(self) -> list[Listing]:
        """Every eligible company, largest first."""
        now = time.time()
        return sorted(
            (
                li
                for li in self.universe.by_symbol.values()
                if self.eligible(li) and now - self.no_chain.get(li.symbol, -1e12) > NO_CHAIN_TTL_S
            ),
            key=lambda li: -(li.market_cap or 0),
        )

    def hot(self, now: float) -> list[str]:
        """Due for a re-read: radar/news names, then today's biggest movers."""
        for s, until in list(self.priority.items()):
            if until < now:
                del self.priority[s]
        movers = sorted(
            (
                li
                for li in self.universe.by_symbol.values()
                if self.eligible(li) and abs(li.change_pct or 0) >= self.cfg.hot_move_pct
            ),
            key=lambda li: -abs(li.change_pct or 0),
        )
        live: list[str] = []
        if self.movers is not None:
            try:
                live = [s for s in self.movers() if self.eligible(self.universe.get(s))]
            except Exception:  # noqa: BLE001 - a radar bug must not stop the options feed
                live = []
        moving = [
            s
            for s in self.live.movers(self.cfg.hot_move_pct, self.cfg.stock_quote_max_age_s, now)
            if self.eligible(self.universe.get(s))
        ]
        order = list(dict.fromkeys([*self.priority, *live, *moving, *(li.symbol for li in movers)]))
        due = [
            s
            for s in order
            if now - self.fetched.get(s, 0.0) >= self.cfg.hot_seconds
            and now - self.no_chain.get(s, -1e12) > NO_CHAIN_TTL_S
        ]
        return due[: self.cfg.hot_size]

    def next_batch(self, now: float, size: int) -> list[str]:
        """Hot names first, the rest from the rotation (largest companies first)."""
        batch = self.hot(now)[:size]
        everyone = self.companies()
        if not everyone:
            return batch
        n = len(everyone)
        tries = 0
        while len(batch) < size and tries < n:
            if self._cursor >= n:
                self._cursor = 0
                self.sweeps += 1
            sym = everyone[self._cursor].symbol
            self._cursor += 1
            tries += 1
            if sym not in batch and now - self.fetched.get(sym, 0.0) >= self.cfg.min_refetch_seconds:
                batch.append(sym)
        for sym in batch:  # scheduled counts as read: a failed read waits its turn, never loops
            self.fetched[sym] = now
        return batch

    # ------------------------------------------------------------------ I/O

    @property
    def label(self) -> str:
        return " → ".join(getattr(src, "label", src.name) for src in self.sources)

    async def fetch_chain(self, symbol: str) -> Chain | None:
        """One company's full chain from the first source that answers; None when the source
        lists no options for it. A source that says "no options" for ten $10B+ companies in a
        row is treated as failing: a blocked server can look exactly like that."""
        listing = self.universe.get(symbol)
        big = bool(listing and (listing.market_cap or 0) >= 10e9)
        last_exc: BaseException | None = None
        for i, src in enumerate(self.sources):
            st = self.source_stats.setdefault(src.name, _blank_stats())
            st["reads"] += 1
            try:
                chain = await src.fetch(symbol, listing, self._pace)
            except NoChain:
                st["no_chain"] += 1
                if big:
                    self._big_nochain += 1
                    if self._big_nochain >= 10:
                        last_exc = RuntimeError(
                            f"{src.name} said 10 companies of $10B+ in a row have no options; "
                            "it may be blocking this server"
                        )
                        st["errors"] += 1
                        st["last_error"] = str(last_exc)
                        for sym, ts in list(self.no_chain.items()):  # those were not real answers
                            if time.time() - ts < 3600:
                                del self.no_chain[sym]
                        continue
                self.no_chain[symbol] = time.time()
                return None
            except (
                HTTPError,
                aiohttp.ClientError,
                ValueError,
                KeyError,
                TypeError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                st["errors"] += 1
                st["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                last_exc = exc
                continue
            st["last_ok"] = time.time()
            if big:
                self._big_nochain = 0
            if i > 0:
                self.fallbacks += 1
            return chain
        if last_exc is None:
            raise RuntimeError("no options source configured")
        raise last_exc

    async def _pace(self, interval_s: float) -> None:
        now = time.monotonic()
        wait = self._next_slot - now
        self._next_slot = max(now, self._next_slot) + interval_s
        if wait > 0:
            await asyncio.sleep(wait)

    def fresh(self, chain: Chain, now: float) -> bool:
        """A chain is stale when its stock quote lags the newest quote seen by more than
        ``max_quote_age_s``. Measuring against the feed's own newest quote (and only against
        the clock with an hour of slack) keeps a time-zone shift in Cboe's timestamps (Central
        vs Eastern) from silencing every chain, while a frozen chain is still caught."""
        if chain.quote_time is None:
            return True
        q = chain.quote_time.timestamp()
        self._newest_quote = max(self._newest_quote, min(q, now + 3600))
        if self._newest_quote - q > self.cfg.max_quote_age_s:
            return False
        return now - q <= self.cfg.max_quote_age_s + 3600

    async def read(self, symbol: str, now: float) -> list[OptionsHit]:
        self.health.polls += 1
        t0 = time.monotonic()
        try:
            chain = await self.fetch_chain(symbol)
        except (
            HTTPError,
            aiohttp.ClientError,
            ValueError,
            KeyError,
            TypeError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
        ) as exc:
            self._error(exc)
            return []
        self.fetched[symbol] = time.time()
        self.health.last_ok = time.time()
        self.health.consecutive_errors = 0
        self.health.last_fetch_ms = (time.monotonic() - t0) * 1000
        if chain is None:
            return []
        self.health.items += len(chain.contracts)
        self.apply_live(chain, now)
        if not self.fresh(chain, now):
            self.stale_chains += 1
            self._stale_run += 1
            self._stale_warned = (
                f"{symbol}: last quote {chain.quote_time:%H:%M} ET" if chain.quote_time else symbol
            )
            return []  # never alert on stale data
        self._stale_run = 0
        hits = self.radar.scan(chain, self.universe.get(symbol), now)
        self._watch(chain, now, bool(hits))
        why = self.urgent_why.get(symbol)
        if why and now - why[1] < 600:
            for h in hits:
                h.reasons.append(f"read at once: {why[0]}")
        return hits

    # ------------------------------------------------------------------ live

    def apply_live(self, chain: Chain, now: float) -> None:
        """The stock's price from the live store when it is fresh. Otherwise a price that came
        from a universe older than 15 minutes says nothing about today's move (on Oct 12 2026 it
        still showed Friday's close after the open), so the move and the model base are dropped
        rather than computed from the wrong day."""
        q = self.live.get(chain.symbol, self.cfg.stock_quote_max_age_s, now)
        if q is not None:
            chain.price = q.price
            if q.prev_close:
                chain.prev_close = q.prev_close
                chain.change_pct = q.change_pct
            chain.stock_source = q.source
            chain.stock_time = datetime.fromtimestamp(q.ts, ET)
            return
        if chain.stock_source == "universe" and self.universe.age > 900:
            chain.change_pct = None
            chain.prev_close = None
            chain.stock_source = "universe (stale)"

    def urgent(self, symbols: Iterable[str], why: str = "") -> int:
        """Read these companies' chains now, ahead of the rotation (news, the radar, the stream).
        Returns how many were queued."""
        n = 0
        now = time.time()
        for s in symbols:
            s = (s or "").upper()
            if not s or not self.eligible(self.universe.get(s)):
                continue
            if now - self._urgent_read.get(s, 0.0) < self.cfg.urgent_gap_s and s not in self._urgent:
                continue
            if s not in self._urgent:
                n += 1
            self._urgent[s] = why or self._urgent.get(s, "")
            self.prioritize([s])
        if n and self._urgent_wake is not None:
            self._urgent_wake.set()
        return n

    def _on_trigger(self, underlying: str, occ: str, bid: float, pct: float) -> None:
        parsed = parse_occ(occ)
        label = occ
        if parsed is not None:
            k = f"{parsed[3]:,.2f}".rstrip("0").rstrip(".")
            label = f"${k} {'call' if parsed[2] == 'C' else 'put'} ({parsed[1]:%b} {parsed[1].day})"
        why = f"the live bid on the {label} hit ${bid:.2f} ({pct:+,.0f}% on its close)"
        self.urgent_why[underlying] = (why, time.time())
        self.urgent([underlying], why)

    async def refresh_stocks(self, symbols: list[str]) -> int:
        """Real-time stock prices for a batch, from Alpaca when its keys are set (one request per
        100 companies). Returns how many were updated."""
        alpaca = next((x for x in self.sources if getattr(x, "name", "") == "alpaca"), None)
        if alpaca is None or not self.cfg.stock_quotes or not symbols:
            return 0
        try:
            rows = await alpaca.stock_snapshots(symbols, self._pace)
        except (HTTPError, aiohttp.ClientError, ValueError, TypeError, asyncio.TimeoutError, OSError) as exc:
            self.stock_errors += 1
            log.info("alpaca stock snapshots failed: %s", exc)
            return 0
        self.stock_reads += 1
        for sym, (price, prev, ts) in rows.items():
            self.live.update(sym, price, prev, ts, "alpaca")
        return len(rows)

    async def sweep_stocks(self, now: float) -> list[str]:
        """Every covered company's live price (Alpaca, 100 a request) every ``stock_sweep_s``, so
        a gap at the open is seen in a couple of minutes on any $1B+ name. Names that newly
        start moving ``hot_move_pct``+ go to the urgent lane, biggest moves first. Returns them."""
        if not self.cfg.stock_sweep_s or now - self.stock_swept < self.cfg.stock_sweep_s:
            return []
        alpaca = next((x for x in self.sources if getattr(x, "name", "") == "alpaca"), None)
        if alpaca is None or not self.cfg.stock_quotes:
            return []
        self.stock_swept = now
        if not await self.refresh_stocks([li.symbol for li in self.companies()]):
            return []
        moving = self.live.movers(self.cfg.hot_move_pct, self.cfg.stock_quote_max_age_s, time.time())
        new = [s for s in moving if s not in self._moving][: self.cfg.stock_sweep_urgent]
        self._moving = set(moving)
        for s in new:
            q = self.live.get(s, self.cfg.stock_quote_max_age_s)
            pct = f" {q.change_pct:+.1f}%" if q is not None and q.change_pct is not None else ""
            self.urgent([s], f"moving{pct} on a live price")
        return new

    def _watch(self, chain: Chain, now: float, hit: bool) -> None:
        """Keep the tripwire on this company's most explosive contracts while it is hot (news,
        radar, moving today, or a hit just now); take it off when it cools."""
        if self.stream is None:
            return
        sym = chain.symbol
        pr = self.priority.get(sym, 0.0) > now
        chg = abs(chain.change_pct or 0.0)
        if not (pr or hit or chg >= self.cfg.hot_move_pct):
            self.stream.drop(sym)
            self._chains.pop(sym, None)
            return
        weight = (3.0 if pr else 1.0) + (2.0 if hit else 0.0) + min(chg, 20.0) / 5.0
        today = datetime.fromtimestamp(now, ET).date()
        rows: list[Watch] = pick_watch(
            chain.contracts, sym, chain.price, today, per_symbol=self.cfg.stream_per_symbol, weight=weight
        )
        if self.cfg.misprints and chain.price:
            curves: dict[tuple[date, str], list[tuple[float, float]]] = {}
            by_occ = {c.symbol: c for c in chain.contracts}
            for w in rows:
                c = by_occ.get(w.occ)
                if c is not None:
                    w.fair, _ = self.radar._fair(c, chain, chain.price, now, curves)
        self._chains[sym] = chain
        self.stream.set_candidates(sym, rows)

    def _on_stream_misprint(self, w: Watch, bid: float | None, ask: float | None, ts: float) -> None:
        """A watched contract's live quote crossed its fair value by the misprint ratio: patch the
        cached chain with that quote and judge it at once (no REST round-trip: a stub quote can be
        gone in seconds)."""
        chain = self._chains.get(w.underlying)
        if chain is None or self._on_hits is None:
            return
        c = next((x for x in chain.contracts if x.symbol == w.occ), None)
        if c is None:
            return
        if bid is not None:
            c.bid = bid
        if ask is not None:
            c.ask = ask
        c.quote_time = datetime.fromtimestamp(ts, ET)
        now = time.time()
        self.apply_live(chain, now)
        today = datetime.fromtimestamp(now, ET).date()
        rows = [
            r
            for r in self.radar.misprint_rows(chain, today, now)
            if r.contract.symbol == w.occ or (r.other is not None and r.other.symbol == w.occ)
        ]
        hit = self.radar._misprint_hit(
            chain, self.universe.get(w.underlying), rows, self.radar._day(now), now
        )
        if hit is None:
            return
        self.stream_misprints += 1
        hit.reasons.append("caught on the live websocket quote")
        task = asyncio.get_running_loop().create_task(self._emit([hit]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _emit(self, hits: list[OptionsHit]) -> None:
        if self._on_hits is None:
            return
        try:
            await self._on_hits(hits)
        except Exception:  # noqa: BLE001
            log.exception("stream options hits not published")

    async def _urgent_loop(self, on_hits: Callable[[list[OptionsHit]], Any], stop: asyncio.Event) -> None:
        if self._urgent_wake is None:
            self._urgent_wake = asyncio.Event()
        if self._urgent_sem is None:
            self._urgent_sem = asyncio.Semaphore(self.cfg.urgent_concurrency)
        while not stop.is_set():
            if not self._urgent:
                self._urgent_wake.clear()
                try:
                    await asyncio.wait_for(self._urgent_wake.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
            now = time.time()
            if self._phase(now) != "open" or now < self._backoff_until:
                self._urgent.clear()
                continue
            batch = list(self._urgent)
            self._urgent.clear()
            for s in batch:
                self._urgent_read[s] = now
                self.fetched[s] = now
            await self.refresh_stocks(batch)

            async def one(sym: str) -> list[OptionsHit]:
                assert self._urgent_sem is not None
                async with self._urgent_sem:
                    if stop.is_set():
                        return []
                    self.urgent_reads += 1
                    return await self.read(sym, time.time())

            results = await asyncio.gather(*(one(s) for s in batch))
            hits = [h for r in results for h in r]
            if hits:
                try:
                    await on_hits(hits)
                except Exception:  # noqa: BLE001
                    log.exception("urgent options hits not published")

    def _error(self, exc: BaseException) -> None:
        h = self.health
        h.errors += 1
        h.consecutive_errors += 1
        h.last_error = f"{type(exc).__name__}: {exc}"[:300]
        h.last_error_at = time.time()
        if (isinstance(exc, HTTPError) and exc.status == 429) or h.consecutive_errors >= 5:
            wait = min(900.0, 30.0 * 2 ** min(h.consecutive_errors, 5))
            if isinstance(exc, HTTPError) and exc.retry_after:
                wait = max(wait, exc.retry_after)
            self._backoff_until = time.time() + wait
            log.warning("options feed backing off %.0fs after %s", wait, h.last_error)

    def _phase(self, now: float) -> str:
        if self.market_phase is None:
            return "open"
        try:
            return str(self.market_phase(now))
        except Exception:  # noqa: BLE001 - a calendar bug must not stop the feed
            return "open"

    def problem(self, now: float) -> str:
        """Once a day, a plain-language note when the feed has stopped working: so a blocked
        or frozen feed is reported rather than mistaken for a quiet market."""
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        if self._problem_day == day:
            return ""
        h = self.health
        msg = ""
        if h.consecutive_errors >= 10:
            msg = (
                f"The options tape has failed {h.consecutive_errors} chain reads in a row "
                f"(last error: {h.last_error}). No options alerts until it recovers; it retries "
                f"on its own. Sources: {self.label}."
            )
        elif any(getattr(src, "oi_fail_run", 0) >= 25 for src in self.sources):
            src = next(x for x in self.sources if getattr(x, "oi_fail_run", 0) >= 25)
            msg = (
                f"Alpaca open interest has failed {src.oi_fail_run} times in a row ({src.oi_last_error}), "
                "so unusual-volume alerts are paused; spikes still alert. With live (not paper) keys, "
                "set ALPACA_TRADING_URL=https://api.alpaca.markets."
            )
        elif self._stale_run >= 25:
            msg = (
                f"The options tape skipped {self._stale_run} chains in a row as stale ({self._stale_warned}). "
                "No alerts are sent on stale quotes; it keeps checking."
            )
        if msg:
            self._problem_day = day
        return msg

    async def run(
        self,
        on_hits: Callable[[list[OptionsHit]], Any],
        stop: asyncio.Event,
        on_problem: Callable[[str], Any] | None = None,
    ) -> None:
        """Regular session only: options quotes do not move outside it. Alongside the rotation:
        the urgent lane and the websocket tripwire."""
        self._urgent_wake = asyncio.Event()
        self._on_hits = on_hits
        side = [asyncio.create_task(self._urgent_loop(on_hits, stop), name="options-urgent")]
        if self.stream is not None:
            side.append(asyncio.create_task(self.stream.run(stop), name="options-stream"))
        try:
            await self._rotate(on_hits, stop, on_problem)
        finally:
            for t in side:
                t.cancel()
            await asyncio.gather(*side, return_exceptions=True)

    async def _rotate(
        self,
        on_hits: Callable[[list[OptionsHit]], Any],
        stop: asyncio.Event,
        on_problem: Callable[[str], Any] | None = None,
    ) -> None:
        while not stop.is_set():
            now = time.time()
            if self._phase(now) != "open" or now < self._backoff_until:
                await _sleep_or_stop(stop, 60.0)
                continue
            await self.sweep_stocks(now)
            batch = self.next_batch(now, self.cfg.batch_size)
            if not batch:
                await _sleep_or_stop(stop, 30.0)
                continue
            await self.refresh_stocks(batch)
            results = await asyncio.gather(*(self._one(s, stop) for s in batch))
            note = self.problem(time.time())
            if note and on_problem is not None:
                try:
                    await on_problem(note)
                except Exception:  # noqa: BLE001
                    log.exception("options problem not published")
            hits = [h for r in results for h in r]
            if hits:
                try:
                    await on_hits(hits)
                except Exception:  # noqa: BLE001 - one bad alert must not stop the feed
                    log.exception("options hits not published")

    async def _one(self, sym: str, stop: asyncio.Event) -> list[OptionsHit]:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.cfg.concurrency)
        async with self._sem:
            if stop.is_set() or time.time() < self._backoff_until:
                return []
            return await self.read(sym, time.time())

    def status(self) -> dict[str, Any]:
        everyone = self.companies()
        return {
            "enabled": True,
            "source": self.label,
            "sources": {
                k: {
                    **v,
                    **(
                        {"open_interest_fail_run": src.oi_fail_run, "open_interest_error": src.oi_last_error}
                        if (src := next((x for x in self.sources if x.name == k), None)) is not None
                        and hasattr(src, "oi_fail_run")
                        else {}
                    ),
                }
                for k, v in self.source_stats.items()
            },
            "fallbacks": self.fallbacks,
            "companies": len(everyone),
            "min_market_cap": self.cfg.min_market_cap,
            "read_today": sum(1 for t in self.fetched.values() if time.time() - t < 86400),
            "sweeps": self.sweeps,
            "rotation_position": self._cursor,
            "hot": len(self.priority),
            "no_chain": len(self.no_chain),
            "chains_scanned": self.radar.chains_scanned,
            "contracts_scanned": self.radar.contracts_scanned,
            "hits": self.radar.hits_total,
            "stale_chains": self.stale_chains,
            "last_stale": self._stale_warned,
            "backing_off_s": max(0, round(self._backoff_until - time.time())),
            "live": {
                "stock_quotes": self.live.status(),
                "stock_snapshot_reads": self.stock_reads,
                "last_stock_sweep": self.stock_swept or None,
                "moving_now": len(self._moving),
                "stock_snapshot_errors": self.stock_errors,
                "urgent_reads": self.urgent_reads,
                "stream_misprints": self.stream_misprints,
                "urgent_waiting": len(self._urgent),
                "stream": (
                    {
                        "enabled": True,
                        "url": self.stream.url,
                        "watching": len(self.stream.watch),
                        "subscribed": len(self.stream.subscribed),
                        "max_quotes": self.stream.max_quotes,
                        **self.stream.stats.to_dict(),
                    }
                    if self.stream is not None
                    else {"enabled": False, "why": "needs Alpaca keys" if self.cfg.stream else "off"}
                ),
            },
            **self.health.to_dict(),
        }
