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
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(v[:26], fmt).replace(tzinfo=ET)
        except ValueError:
            continue
    return None


def parse_occ(sym: str) -> tuple[str, date, str, float] | None:
    m = OCC_RE.match(sym or "")
    if not m:
        return None
    try:
        exp = datetime.strptime(m["ymd"], "%y%m%d").date()
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
    big_enough: bool = True  # the honest move still clears the threshold

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
            "last_trade": c.last_trade.isoformat() if c.last_trade else None,
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


@dataclass
class OptionsHit:
    symbol: str
    kind: str  # "spike" | "flow"
    severity: str
    chain: Chain
    listing: Listing | None
    spikes: list[SpikeRow] = field(default_factory=list)
    flow: list[FlowRow] = field(default_factory=list)
    rung: float = 0.0  # spike: the % rung crossed
    flow_premium: float = 0.0
    call_premium: float = 0.0
    put_premium: float = 0.0
    reasons: list[str] = field(default_factory=list)  # why a spike did not push
    detected: float = field(default_factory=time.time)

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
            "flow_premium": round(self.flow_premium),
            "call_premium": round(self.call_premium),
            "put_premium": round(self.put_premium),
            "reasons": list(self.reasons),
            "detected": self.detected,
            "source": self.chain.source,
        }


@dataclass
class _DayState:
    day: str = ""
    spike_any: float = 0.0  # highest rung shown today
    spike_real: float = 0.0  # highest bid-confirmed, real-base rung shown today
    flow_premium: float = 0.0  # unusual premium at the last flow alert


class OptionsRadar:
    """Chains in, hits out. Pure logic (no I/O), so every rule is unit-tested."""

    def __init__(self, cfg: OptionsConfig, state_path: Path | None = None) -> None:
        self.cfg = cfg
        self.state: dict[str, _DayState] = {}
        self.pushes: dict[str, int] = {}  # ET date -> options pushes sent
        self.board: dict[str, dict[str, Any]] = {}  # latest hit per company, for /api/options
        self.chains_scanned = 0
        self.contracts_scanned = 0
        self.hits_total = 0
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
                self.state[sym] = _DayState(day, float(v[0]), float(v[1]), float(v[2]))
            except (TypeError, ValueError, IndexError):
                continue
        self.pushes = {k: int(v) for k, v in (doc.get("pushes") or {}).items() if k == day}

    def save(self, now: float | None = None) -> None:
        if not self.state_path:
            return
        day = self._day(now or time.time())
        fired = {
            s: [st.spike_any, st.spike_real, st.flow_premium]
            for s, st in self.state.items()
            if st.day == day and (st.spike_any or st.flow_premium)
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

    def spike_row(self, c: Contract, chain: Chain, today: date) -> SpikeRow | None:
        """A contract up ``spike_min_pct``+ on today's prints, with real volume behind it."""
        cfg = self.cfg
        move = c.move_pct
        if move is None or move < cfg.spike_min_pct:
            return None
        if c.last_trade is not None and c.last_trade.date() != today:
            return None  # the "last" is an old print: nothing traded today
        if c.volume < cfg.spike_min_volume or (c.last or 0) < cfg.spike_min_price:
            return None
        if c.volume * (c.last or 0) * 100 < cfg.spike_min_premium:
            return None
        fair = self.fair_prev(c, chain, today)
        prev = c.prev_close or 0.0
        stale = fair is not None and fair >= cfg.stale_min_fair and prev < cfg.stale_base_ratio * fair
        base = max(prev, fair or 0.0) if stale else prev
        honest = (c.last / base - 1.0) * 100.0 if base > 0 and c.last else move
        bid_move = (c.bid / base - 1.0) * 100.0 if c.bid and base > 0 else None
        confirmed = bid_move is not None and bid_move >= cfg.spike_min_pct * cfg.spike_confirm_fraction
        return SpikeRow(c, move, bid_move, confirmed, fair, stale, honest, honest >= cfg.spike_min_pct)

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
        spikes = [r for r in (self.spike_row(c, chain, today) for c in live) if r is not None]
        if spikes:
            hit = self._spike_hit(chain, listing, spikes, st, day, now)
            if hit is not None:
                hits.append(hit)

        flow = [r for r in (self.flow_row(c) for c in live) if r is not None]
        if flow:
            hit = self._flow_hit(chain, listing, flow, st, day, now)
            if hit is not None:
                hits.append(hit)

        for h in hits:
            self.board[chain.symbol] = h.to_dict()
        if hits and len(self.board) > 500:  # the app shows today's hits; keep memory bounded
            for sym, _ in sorted(self.board.items(), key=lambda kv: kv[1].get("detected", 0))[:100]:
                del self.board[sym]
        self.hits_total += len(hits)
        if hits:
            self.save(now)
        return hits

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
        if rung_real > st.spike_real and self._push_ok(day):
            severity = "HIGH"
        st.spike_real = max(st.spike_real, rung_real)
        st.spike_any = max(st.spike_any, rung_any)
        top = rows[0]
        reasons: list[str] = []
        if not top.real:
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
        doc = await asyncio.to_thread(json.loads, raw) if len(raw) > 512_000 else json.loads(raw)
        return parse_chain(doc, symbol)


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
    chain = Chain(symbol=symbol, price=price, change_pct=change, source=source)
    newest: datetime | None = None
    near_iv: list[float] = []
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
        if bar_day is not None and bar_day.date() == today:
            volume, prev_close = _int(bar.get("v")), _num(prev_bar.get("c"))
        else:
            volume, prev_close = 0, _num(bar.get("c"))
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
            )
        )
        if iv and price and 7 <= (exp - today).days <= 60 and abs(strike / price - 1) <= 0.05:
            near_iv.append(iv)
    chain.quote_time = newest
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
    ) -> None:
        self.cfg = cfg
        self.sources = sources if sources is not None else build_sources(cfg, http)
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
        order = list(dict.fromkeys([*self.priority, *live, *(li.symbol for li in movers)]))
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
        if not self.fresh(chain, now):
            self.stale_chains += 1
            self._stale_run += 1
            self._stale_warned = (
                f"{symbol}: last quote {chain.quote_time:%H:%M} ET" if chain.quote_time else symbol
            )
            return []  # never alert on stale data
        self._stale_run = 0
        return self.radar.scan(chain, self.universe.get(symbol), now)

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
        """Regular session only: options quotes do not move outside it."""
        while not stop.is_set():
            now = time.time()
            if self._phase(now) != "open" or now < self._backoff_until:
                await _sleep_or_stop(stop, 60.0)
                continue
            batch = self.next_batch(now, self.cfg.batch_size)
            if not batch:
                await _sleep_or_stop(stop, 30.0)
                continue
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
            **self.health.to_dict(),
        }
