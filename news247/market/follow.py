"""After the alert: how far into the move you are, what the contract does if you get in now, and a
live verdict (still running, holding, fading, stopped) with follow-up alerts.

Why this exists (Oct 12 2026): the META $785 call (Oct 14) alert arrived at 12:51 ET. The
contract had closed Friday at $0.18. It ran to $1.03 by 9:45 ET, sank to $0.63 while the stock
held, ran again to $1.83 by 11:15, and was trading $1.05 to $1.30 when the alert went out. The
alert said nothing about any of that, and nothing came after it. A late entrant could not tell
whether they were buying a breakout or the third push of a tired move, or when to get out.

Three pieces:

* ``option_path``: the contract's session from its 5-minute bars (Alpaca): where it began to
  run, its high and when, how many legs, and how far under the high it is now.
* ``play_card``: for the price you would pay *now* (the ask): delta, the stock's breakeven at
  expiry, what the contract is worth if the stock holds, gives back half its move, or goes 1%
  further, a vol-crush case, and the stop levels on the option and on the stock.
* ``FollowDesk``: every alerted contract is followed on live quotes (the websocket, and a chain
  re-read every ``follow_read_s``) until the close. Its state is judged on the *bid* (what you
  can sell for) against the ask at the alert (what a late entrant paid) and the best bid since:

  - RUNNING: a new high since the alert, ``follow_run_step`` above the last one pushed;
  - HOLDING: under the high but above the fade line;
  - FADING: the bid ``follow_fade`` under its high, or the stock has given back half its move;
  - STOPPED: the bid ``follow_stop`` under the entry or the high, or the stock's whole move is
    gone (back to yesterday's close). A stopped contract is still watched: a new high above the
    one before the stop sends "running again" and the lines start over from that price (the META
    call was stopped by this rule at 9:55, with the stock holding, and ran to $1.83 by 11:20).

  A drop must show on two quotes ``follow_confirm_s`` apart (a market maker pulling a bid for a
  second is not a stop), and highs count only when two quotes in a row reach them.

The system never trades: these are levels and states, the same rules every time, so a late
entry has an exit decided before it is made.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .options import RISK_FREE, _ncdf, bs_price, implied_vol, years_left

if TYPE_CHECKING:  # pragma: no cover
    from ..config import OptionsConfig
    from .live import DayPath
    from .options import Chain, Contract, OptionsHit

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

RUNNING, HOLDING, FADING, STOPPED = "RUNNING", "HOLDING", "FADING", "STOPPED"
_RANK = {RUNNING: 0, HOLDING: 1, FADING: 2, STOPPED: 3}


def _et(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


# --------------------------------------------------------------------------- the option's day


@dataclass
class OptionPath:
    """One contract's session so far, from its bars (built from trades: on Alpaca's free feed they
    run about 15 minutes behind, so ``to_ts`` says how far they reach)."""

    base: float  # yesterday's honest close
    open: float
    low: float
    low_ts: float
    high: float
    high_ts: float
    last: float
    to_ts: float  # the start of the last bar
    began_ts: float | None  # the first bar that traded at 2x+ the base
    legs: int  # runs to a new high, each after a bar opened or closed 35% under the high before it
    volume: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_option_bars(doc: Any) -> dict[str, list[tuple[float, float, float, float, float, int]]]:
    """Alpaca ``/v1beta1/options/bars`` -> {occ: [(epoch, open, high, low, close, volume)]}."""
    from .live import _rfc3339

    out: dict[str, list[tuple[float, float, float, float, float, int]]] = {}
    bars = doc.get("bars") if isinstance(doc, dict) else None
    if not isinstance(bars, dict):
        return out
    for occ, rows in bars.items():
        for b in rows or []:
            if not isinstance(b, dict):
                continue
            when = _rfc3339(b.get("t"))
            try:
                row = (
                    when.timestamp() if when else 0.0,
                    float(b["o"]),
                    float(b["h"]),
                    float(b["l"]),
                    float(b["c"]),
                    int(b.get("v") or 0),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if row[0] > 0:
                out.setdefault(str(occ), []).append(row)
    for rows in out.values():
        rows.sort()
    return out


def option_path(
    bars: list[tuple[float, float, float, float, float, int]], base: float, pullback: float = 0.35
) -> OptionPath | None:
    """The session from today's bars: high/low and when, the first bar at 2x the base, and the
    legs. A leg ends when a bar opens or closes ``pullback`` (35%, the stop) under the leg's high
    (the bodies, not the wicks: a 5-minute bar does not say whether its low came before its high);
    the next new high starts another."""
    if not bars:
        return None
    hi, hi_ts = bars[0][2], bars[0][0]
    lo, lo_ts = bars[0][3], bars[0][0]
    began = None
    legs = 0
    run_hi = 0.0  # the high of the current leg
    pulled = True  # the first push counts as a leg
    for ts, o, h, low, c, _v in bars:
        if began is None and base > 0 and h >= 2.0 * base:
            began = ts
        if run_hi and min(o, c) <= (1.0 - pullback) * run_hi:
            pulled = True
        if h > run_hi:
            if pulled and (base <= 0 or h >= 2.0 * base):
                legs += 1
                pulled = False
            run_hi = h
        if h > hi:
            hi, hi_ts = h, ts
        if low < lo:
            lo, lo_ts = low, ts
    return OptionPath(
        base=base,
        open=bars[0][1],
        low=lo,
        low_ts=lo_ts,
        high=hi,
        high_ts=hi_ts,
        last=bars[-1][4],
        to_ts=bars[-1][0],
        began_ts=began,
        legs=legs,
        volume=sum(b[5] for b in bars),
    )


# --------------------------------------------------------------------------- if you get in now


@dataclass
class Play:
    contract: str  # OCC
    label: str
    cp: str
    entry: float  # what a buyer pays now: the ask, else the mid, else the last print
    entry_how: str  # "ask" | "mid" | "last"
    base: float  # yesterday's honest close
    stock: float | None
    stock_prev: float | None
    iv: float | None  # implied vol at the entry price
    delta: float | None
    per_1pct: float | None  # $ a share the contract gains on a 1% stock move its way, now
    breakeven: float  # the stock at expiry for the entry to pay back
    breakeven_pct: float | None  # that, vs the stock now
    scenarios: list[tuple[str, float, float]] = field(default_factory=list)  # (case, value, % vs entry)
    stop: float = 0.0  # option: the stop on the bid (the mid now - follow_stop; it trails the best bid)
    fade: float = 0.0  # option: the warning line (the mid now - follow_fade)
    stock_fade: float | None = None  # the stock giving back half its move
    stock_stop: float | None = None  # the stock's move erased (yesterday's close)
    path: OptionPath | None = None
    off_high_pct: float | None = None  # entry vs the session's high, %
    verdict: str = ""  # where in the move a buyer now is

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scenarios"] = [{"case": c, "value": round(v, 4), "pct": round(p, 1)} for c, v, p in self.scenarios]
        return d


def bs_delta(s: float, k: float, t: float, sigma: float, cp: str, r: float = RISK_FREE) -> float:
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return (1.0 if s > k else 0.0) if cp == "C" else (-1.0 if s < k else 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return _ncdf(d1) if cp == "C" else _ncdf(d1) - 1.0


def _close_today(now: float) -> float:
    d = datetime.fromtimestamp(now, ET)
    return d.replace(hour=16, minute=0, second=0, microsecond=0).timestamp()


def play_card(
    c: Contract,
    chain: Chain,
    base: float,
    now: float,
    cfg: OptionsConfig,
    iv_prev: float | None = None,
    path: OptionPath | None = None,
    stock_path: DayPath | None = None,
) -> Play | None:
    """What a buyer at the current ask is getting into, and where the exits are."""
    if c.ask and c.ask > 0:
        entry, how = float(c.ask), "ask"
    elif c.mid:
        entry, how = float(c.mid), "mid"
    elif c.last:
        entry, how = float(c.last), "last"
    else:
        return None
    s = chain.price
    prev = chain.stock_prev
    call = c.cp == "C"
    breakeven = c.strike + entry if call else c.strike - entry
    play = Play(
        contract=c.symbol,
        label=c.label(),
        cp=c.cp,
        entry=entry,
        entry_how=how,
        base=base,
        stock=s,
        stock_prev=prev,
        iv=None,
        delta=None,
        per_1pct=None,
        breakeven=breakeven,
        breakeven_pct=((breakeven / s - 1.0) * 100.0) if s else None,
        stop=round((c.mid or entry) * (1.0 - cfg.follow_stop), 2),
        fade=round((c.mid or entry) * (1.0 - cfg.follow_fade), 2),
        path=path,
    )
    if s and prev:
        peak = s
        if stock_path is not None:
            peak = max(peak, stock_path.high) if call else min(peak, stock_path.low)
        if (call and peak > prev) or (not call and peak < prev):
            play.stock_fade = round(prev + 0.5 * (peak - prev), 2)
            play.stock_stop = round(prev, 2)
    if s:
        t = years_left(c.expiry, now)
        mark = c.mid or entry
        iv = implied_vol(mark, s, c.strike, t, c.cp) or c.iv
        if iv:
            play.iv = iv
            play.delta = bs_delta(s, c.strike, t, iv, c.cp)
            way = 1.0 if call else -1.0
            up = bs_price(s * (1 + 0.01 * way), c.strike, t, iv, c.cp)
            play.per_1pct = up - bs_price(s, c.strike, t, iv, c.cp)
            t_close = years_left(c.expiry, max(now, _close_today(now)))

            def case(what: str, stock: float, tt: float, vol: float) -> None:
                v = bs_price(stock, c.strike, tt, vol, c.cp)
                play.scenarios.append((what, v, (v / entry - 1.0) * 100.0))

            case(f"{c.underlying} {'1% higher' if call else '1% lower'} now", s * (1 + 0.01 * way), t, iv)
            if t_close < t - 1e-9:
                case(f"{c.underlying} flat into the close", s, t_close, iv)
            if prev and abs(s / prev - 1.0) >= 0.005:
                case(f"{c.underlying} gives back half today's move", prev + 0.5 * (s - prev), t, iv)
            if iv_prev and iv > 1.15 * iv_prev:
                case(f"implied vol back to yesterday's {iv_prev * 100:.0f}%", s, t, iv_prev)
    if path is not None and path.high > 0:
        play.off_high_pct = (entry / path.high - 1.0) * 100.0
        play.verdict = _verdict(play, path)
    return play


def _verdict(play: Play, path: OptionPath) -> str:
    off = play.off_high_pct or 0.0
    bits = []
    if path.legs >= 3:
        bits.append(f"push #{path.legs} of the day: late in the move")
    elif path.legs == 2:
        bits.append("its second run of the day")
    if off > 0:
        bits.append(
            f"above the day's ${path.high:.2f} high: a new high, the move is live, and the stop is close"
        )
    elif off >= -10.0:
        bits.append(
            f"within {-off:.0f}% of the day's ${path.high:.2f} high: the move is live, but you pay near the top"
        )
    elif off <= -25.0:
        bits.append(
            f"{-off:.0f}% under the day's ${path.high:.2f} high: a pullback, not the run; "
            f"it is only running again above ${path.high:.2f}"
        )
    else:
        bits.append(f"{-off:.0f}% under the day's ${path.high:.2f} high")
    return "; ".join(bits)


# --------------------------------------------------------------------------- following


@dataclass
class Followed:
    occ: str
    underlying: str
    label: str
    cp: str
    strike: float
    expiry: str  # ISO date
    kind: str  # the alert: "spike" | "blowup"
    day: str  # the ET session
    base: float
    alert_ts: float
    entry: float  # the ask at the alert: what a late entrant paid
    alert_bid: float | None
    mark: float  # the mid at the alert: lines are measured from it (or the high), so the spread alone trips nothing
    stock_alert: float | None
    stock_prev: float | None  # the stock's previous close: the line that erases its move
    high: float  # the best bid since the alert (on two quotes in a row)
    high_ts: float
    day_high: float | None = None  # the contract's session high from its bars at the alert
    day_high_ts: float | None = None
    bid: float | None = None
    ask: float | None = None
    ts: float = 0.0  # the latest quote's time
    prev_bid: float | None = None  # the quote before it (two-quote highs)
    stock: float | None = None
    stock_peak: float | None = None  # the stock's best level today in the trade's direction
    state: str = HOLDING
    pending: str = ""  # a drop seen once, waiting for a second quote
    pending_ts: float = 0.0
    next_run: float = 0.0  # the bid that sends the next "still running"
    pushes: int = 0
    last_push: float = 0.0
    quotes: int = 0
    ended: str = ""
    ended_ts: float = 0.0
    low: float | None = None  # the worst bid since the alert
    held: float = 0.0  # the bid held on the last two quotes (what highs and "running" are judged on)
    stopped_high: float = 0.0  # the high before the last stop: a bid above it is "running again"
    stops: int = 0
    reruns: int = 0

    @property
    def call(self) -> bool:
        return self.cp == "C"

    _stop_frac: float = 0.35
    _fade_frac: float = 0.20

    @property
    def ref(self) -> float:
        """What the lines hang from: the best bid since the alert, or the mid at the alert."""
        return max(self.high, self.mark)

    @property
    def stop(self) -> float:
        return round(self.ref * (1.0 - self._stop_frac), 2)

    @property
    def fade(self) -> float:
        return round(self.ref * (1.0 - self._fade_frac), 2)

    @property
    def recover(self) -> float:
        """Back above this after a fade, a later fade alerts again (half-way back to the ref)."""
        return round(self.ref * (1.0 - self._fade_frac / 2), 2)

    @property
    def stock_fade(self) -> float | None:
        if self.stock_prev is None or self.stock_peak is None:
            return None
        if (self.call and self.stock_peak <= self.stock_prev) or (
            not self.call and self.stock_peak >= self.stock_prev
        ):
            return None
        return self.stock_prev + 0.5 * (self.stock_peak - self.stock_prev)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d.update(stop=self.stop, fade=self.fade, stock_fade=self.stock_fade)
        if self.bid is not None and self.entry:
            d["since_alert_pct"] = round((self.bid / self.entry - 1.0) * 100.0, 1)
        return d


@dataclass
class FollowEvent:
    kind: str  # "running" | "fading" | "stopped" | "rerun"
    severity: str  # "HIGH" | "MEDIUM"
    f: Followed
    why: str
    ts: float


class FollowDesk:
    """The alerted contracts being followed, their states, and the follow-up alerts."""

    def __init__(self, cfg: OptionsConfig, state_path: Path | None = None) -> None:
        self.cfg = cfg
        self.state_path = state_path
        self.items: dict[str, Followed] = {}
        self.day = ""
        self.pushed_today = 0
        self.events: list[dict[str, Any]] = []  # recent, for the app
        self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            doc = json.loads(self.state_path.read_text())
            today = datetime.now(ET).date().isoformat()
            if doc.get("day") != today:
                return
            self.day = today
            self.pushed_today = int(doc.get("pushed") or 0)
            for row in doc.get("items") or []:
                row = {k: v for k, v in row.items() if k in Followed.__dataclass_fields__}
                f = Followed(**row)
                self._arm(f)
                self.items[f.occ] = f
            self.events = list(doc.get("events") or [])[-50:]
        except (OSError, ValueError, TypeError) as exc:
            log.warning("follow state not loaded: %s", exc)

    def save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "day": self.day,
                        "pushed": self.pushed_today,
                        "items": [f.to_dict() for f in self.items.values()],
                        "events": self.events[-50:],
                    }
                )
            )
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("follow state not saved: %s", exc)

    def _arm(self, f: Followed) -> None:
        f._stop_frac = self.cfg.follow_stop
        f._fade_frac = self.cfg.follow_fade

    def _roll(self, now: float) -> None:
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        if day != self.day:
            self.day = day
            self.pushed_today = 0
            self.items = {k: f for k, f in self.items.items() if f.day == day}

    # ------------------------------------------------------------------ in

    def track(self, hit: OptionsHit, now: float | None = None) -> list[Followed]:
        """Follow the lead contract(s) of a spike or blowup alert. A contract already followed is
        left as it is; one that was stopped starts over from this alert (a new entry)."""
        if not self.cfg.follow or hit.kind not in ("spike", "blowup", "daymove"):
            return []
        now = now or time.time()
        self._roll(now)
        out = []
        for c, base in lead_contracts(hit)[: self.cfg.follow_per_alert]:
            cur = self.items.get(c.symbol)
            if cur is not None and not cur.ended and cur.state != STOPPED:
                continue
            entry = c.ask or c.mid or c.last
            if not entry:
                continue
            ch = hit.chain
            path = hit.paths.get(c.symbol)
            alert_bid = c.bid if c.bid and c.bid > 0 else None
            mark = c.mid or (alert_bid if alert_bid and alert_bid <= entry else None) or entry
            f = Followed(
                occ=c.symbol,
                underlying=c.underlying,
                label=c.label(),
                cp=c.cp,
                strike=c.strike,
                expiry=c.expiry.isoformat(),
                kind=hit.kind,
                day=self.day,
                base=base,
                alert_ts=now,
                entry=float(entry),
                alert_bid=alert_bid,
                mark=float(mark),
                stock_alert=ch.price,
                stock_prev=ch.stock_prev,
                high=float(alert_bid or entry),
                high_ts=now,
                day_high=path.high if path else None,
                day_high_ts=path.high_ts if path else None,
                bid=alert_bid,
                prev_bid=alert_bid,
                ask=c.ask,
                ts=c.quote_time.timestamp() if c.quote_time else now,
                stock=ch.price,
                stock_peak=ch.price,
                low=alert_bid,
            )
            self._arm(f)
            f.next_run = round(f.entry * (1.0 + self.cfg.follow_run_step), 2)
            self.items[c.symbol] = f
            out.append(f)
        if len(self.items) > self.cfg.follow_max:  # the oldest go first
            for occ in sorted(self.items, key=lambda o: self.items[o].alert_ts)[
                : len(self.items) - self.cfg.follow_max
            ]:
                del self.items[occ]
        if out:
            self.save()
        return out

    def active(self) -> list[Followed]:
        return [f for f in self.items.values() if not f.ended]

    def underlyings(self) -> set[str]:
        return {f.underlying for f in self.active()}

    def is_followed(self, occ: str) -> bool:
        f = self.items.get(occ)
        return f is not None and not f.ended

    # ------------------------------------------------------------------ quotes

    def observe(
        self,
        occ: str,
        bid: float | None,
        ask: float | None,
        ts: float,
        stock: float | None = None,
        stock_path: DayPath | None = None,
        now: float | None = None,
    ) -> list[FollowEvent]:
        """One quote of a followed contract (from the websocket or a chain read)."""
        f = self.items.get(occ)
        if f is None or f.ended:
            return []
        now = now if now is not None else ts  # gaps run on the quote's clock unless told otherwise
        if ts < f.ts:  # an older quote than the one already seen
            return []
        if ask:
            f.ask = ask
        if stock:
            f.stock = stock
            if f.stock_peak is None:
                f.stock_peak = stock
            f.stock_peak = max(f.stock_peak, stock) if f.call else min(f.stock_peak, stock)
        if stock_path is not None:
            ext = stock_path.high if f.call else stock_path.low
            if ext and f.stock_peak is not None:
                f.stock_peak = max(f.stock_peak, ext) if f.call else min(f.stock_peak, ext)
        if not bid or bid <= 0:
            f.ts = max(f.ts, ts)
            return []  # no bid: nothing to judge (a pulled bid is not a price)
        f.quotes += 1
        two = min(bid, f.prev_bid) if f.prev_bid else None  # a high must hold on two quotes in a row
        f.held = two or 0.0
        if two is not None and two > f.high:
            f.high, f.high_ts = two, ts
        f.prev_bid, f.bid, f.ts = bid, bid, ts
        f.low = bid if f.low is None else min(f.low, bid)
        return self._judge(f, now)

    @staticmethod
    def _move_gone(f: Followed) -> bool:
        """The stock is back to (or through) yesterday's close: its whole move is erased."""
        if f.stock is None or f.stock_prev is None:
            return False
        return f.stock <= f.stock_prev if f.call else f.stock >= f.stock_prev

    def classify(self, f: Followed) -> tuple[str, str]:
        """The state the latest quote alone says, and why."""
        bid = f.bid or 0.0
        if self._move_gone(f):
            return STOPPED, (
                f"{f.underlying} is back to yesterday's close (${f.stock_prev or 0:,.2f}): its whole move is gone"
            )
        if bid <= f.stop:
            return STOPPED, f"the bid (${bid:.2f}) is {self._under(f, bid)}"
        half = f.stock_fade
        if half is not None and f.stock is not None and (f.stock <= half if f.call else f.stock >= half):
            return (
                FADING,
                f"{f.underlying} has given back half its move (${f.stock:,.2f}, half-way line ${half:,.2f})",
            )
        if bid <= f.fade:
            return FADING, f"the bid (${bid:.2f}) is {self._under(f, bid)}"
        if bid >= f.high * 0.97 and bid >= f.entry:
            return RUNNING, f"a new high since {'it ran again' if f.reruns else 'the alert'} (${bid:.2f} bid)"
        return HOLDING, ""

    def _judge(self, f: Followed, now: float) -> list[FollowEvent]:
        events: list[FollowEvent] = []
        if f.state == STOPPED:
            return self._record(self._rerun(f, now), f, now)
        state, why = self.classify(f)
        if _RANK[state] > _RANK[HOLDING]:  # a drop: it must show again confirm_s later
            if not f.pending or f.pending_ts > f.ts:
                f.pending, f.pending_ts = state, f.ts
                return []
            if f.ts - f.pending_ts < self.cfg.follow_confirm_s:
                if _RANK[state] < _RANK[f.pending]:
                    f.pending = state  # the milder reading counts
                return []
            confirmed = state if _RANK[state] <= _RANK[f.pending] else f.pending
            f.pending, f.pending_ts = "", 0.0
            if confirmed != state:
                state, why = self.classify_as(f, confirmed)
            if _RANK[state] > _RANK[f.state] or (state == STOPPED and f.state != STOPPED):
                f.state = state
                ev = FollowEvent(state.lower(), "HIGH", f, why, now)
                if self._may_push(f, now, final=state == STOPPED, gap=state != STOPPED):
                    events.append(ev)
                if state == STOPPED:
                    f.stopped_high, f.stops = f.high, f.stops + 1
            return self._record(events, f, now)
        f.pending, f.pending_ts = "", 0.0
        if state == RUNNING and f.held >= f.next_run:
            f.state = RUNNING
            f.next_run = round(f.held * (1.0 + self.cfg.follow_run_step), 2)
            if self._may_push(f, now, final=False, gap=True):
                events.append(FollowEvent("running", "MEDIUM", f, why, now))
        elif f.state == FADING and f.bid is not None and f.bid >= f.recover:
            f.state = state  # well back above the fade line: a later fade may alert again
        elif state == RUNNING and f.state == HOLDING:
            f.state = RUNNING
        elif state == HOLDING and f.state == RUNNING:
            f.state = HOLDING
        return self._record(events, f, now)

    @staticmethod
    def _under(f: Followed, bid: float) -> str:
        since = "it ran again" if f.reruns else "the alert"
        if f.high > f.mark:
            return f"{(1 - bid / f.high) * 100:.0f}% under its ${f.high:.2f} high since {since}"
        return f"{(1 - bid / f.mark) * 100:.0f}% under the ${f.mark:.2f} mid when {'it ran again' if f.reruns else 'alerted'}"

    def _rerun(self, f: Followed, now: float) -> list[FollowEvent]:
        """After a stop: a bid above the high before the stop, on two quotes, is the move running
        again (and the stock still has its move). The lines start over from the price now."""
        if f.held <= f.stopped_high:
            return []
        if self._move_gone(f):
            return []
        old = f.stopped_high
        f.state = RUNNING
        f.entry = float(f.ask or f.bid)
        f.mark = round((f.bid + f.ask) / 2, 4) if f.ask and f.ask >= f.bid else f.bid
        f.high, f.high_ts = f.held, f.ts
        f.next_run = round(f.entry * (1.0 + self.cfg.follow_run_step), 2)
        f.reruns += 1
        why = f"a new high (${f.held:.2f} bid on two quotes) above the ${old:.2f} it reached before the stop"
        if not self._may_push(f, now, final=False):
            return []
        return [FollowEvent("rerun", "MEDIUM", f, why, now)]

    def classify_as(self, f: Followed, state: str) -> tuple[str, str]:
        if state == FADING:
            return FADING, f"the bid (${(f.bid or 0.0):.2f}) is {self._under(f, f.bid or 0.0)}"
        return self.classify(f)

    def _may_push(self, f: Followed, now: float, final: bool, gap: bool = False) -> bool:
        if not final:
            if f.pushes >= self.cfg.follow_max_pushes:
                return False
            if gap and now - f.last_push < self.cfg.follow_gap_s:
                return False
        if self.pushed_today >= self.cfg.follow_daily_pushes and not final:
            return False
        f.pushes += 1
        f.last_push = now
        self.pushed_today += 1
        return True

    def _record(self, events: list[FollowEvent], f: Followed, now: float) -> list[FollowEvent]:
        for e in events:
            self.events.append(
                {
                    "ts": now,
                    "kind": e.kind,
                    "occ": f.occ,
                    "label": f.label,
                    "bid": f.bid,
                    "entry": f.entry,
                    "high": f.high,
                    "why": e.why,
                }
            )
        if events:
            self.events = self.events[-50:]
            self.save()
        return events

    # ------------------------------------------------------------------ out

    def due(self, now: float, every_s: float) -> set[str]:
        """Companies whose followed contracts have no quote for ``every_s``: re-read their chains."""
        return {f.underlying for f in self.active() if now - f.ts >= every_s}

    def close(self, now: float, why: str = "the session closed") -> int:
        """End every follow (at the close, or for contracts past expiry)."""
        n = 0
        for f in self.active():
            f.ended, f.ended_ts = why, now
            n += 1
        if n:
            self.save()
        return n

    def expire(self, now: float) -> int:
        """End follows older than ``follow_hours`` and contracts that have expired."""
        today = datetime.fromtimestamp(now, ET).date()
        n = 0
        for f in self.active():
            if now - f.alert_ts > self.cfg.follow_hours * 3600 or date.fromisoformat(f.expiry) < today:
                f.ended, f.ended_ts = "time", now
                n += 1
        if n:
            self.save()
        return n

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.follow,
            "following": len(self.active()),
            "pushed_today": self.pushed_today,
            "items": sorted((f.to_dict() for f in self.items.values()), key=lambda d: -d["alert_ts"])[:20],
            "events": list(reversed(self.events[-20:])),
            "rules": {
                "fade": self.cfg.follow_fade,
                "stop": self.cfg.follow_stop,
                "run_step": self.cfg.follow_run_step,
                "confirm_s": self.cfg.follow_confirm_s,
            },
        }


def lead_contracts(hit: OptionsHit) -> list[tuple[Contract, float]]:
    """The alert's contracts in order, each with its honest base (yesterday's close, or its
    value then when the close was stale or missing)."""
    out: list[tuple[Contract, float]] = []
    if hit.kind == "spike":
        for r in hit.spikes:
            c = r.contract
            base = c.prev_close or 0.0
            if (r.stale_base or r.no_prior) and r.fair_prev:
                base = max(base, r.fair_prev)
            if base > 0:
                out.append((c, base))
    elif hit.kind == "blowup":
        out = [(r.contract, r.base) for r in hit.blowups if r.base > 0]
    elif hit.kind == "daymove" and hit.daycall is not None and hit.stage in ("call", "zone"):
        by = {c.symbol: c for c in hit.chain.contracts}
        for p in hit.daycall.picks:
            c = by.get(p.occ)
            if c is not None:
                out.append((c, c.prev_close or p.mid))
    return out
