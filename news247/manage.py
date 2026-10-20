"""Managing an open paper call: the nudges a good desk would give between the entry and the exit.

Most of what is won or lost on an idea happens after the entry. For each open call a phone tracks
by hand (the autopilot manages its own), the live price is read every minute against what the call
has done so far:

- **Breakeven:** up a full R (the distance from the entry to the stop): move the stop to the entry
  and the call can no longer lose. One tap does it.
- **Given back:** it was up 2%+ (or a full R) and has given back 70% of that: lock in what's left
  or close it.
- **Dead money:** a session and more after it opened, it's within ±0.5% of the entry; and when
  its kind of news has a record, that record says the move usually happens by the close.
- **A report while you hold it:** the stock reports today or tomorrow.

Each nudge pushes once per call (when the phone allows it) and stays in Needs you while it holds.
Advice from the call's own numbers and history, never an order: the system never trades.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
GIVEBACK_MIN = 2.0  # % a call must have been up before giving it back counts
GIVEBACK_SHARE = 0.3  # ...and what's left of that best: 30% or less
DEAD_BAND = 0.5  # % from the entry that counts as flat
CUT_ODDS = 0.3  # live odds this low, on a call going the alert's way, and it's a cut


def _px(v: float) -> str:
    return f"${v:,.2f}"


def _pc(v: float) -> str:
    """+2.5%, -1.0%, and a flat 0.0% (never "-0.0%")."""
    s = f"{v:+.1f}%"
    return "0.0%" if s in ("+0.0%", "-0.0%") else s


def _r(t: dict[str, Any]) -> float | None:
    """1R in %: how far the original stop sat from the entry (None without a stop on the loss side)."""
    stop, entry = t.get("risk_stop") or t.get("stop"), t.get("entry")
    if stop is None or not entry:
        return None
    r = (entry - stop) / entry * 100 if t.get("direction", "long") == "long" else (stop - entry) / entry * 100
    return r if r > 0 else None


def _pnl(t: dict[str, Any], price: float) -> float:
    r = (price / t["entry"] - 1) * 100
    return r if t.get("direction", "long") == "long" else -r


def nudges(engine: Engine, device: str, now: float | None = None) -> list[dict[str, Any]]:
    """The nudges that hold right now on this phone's open calls (no side effects)."""
    from .autopilot import exit_time
    from .dossier import _direction
    from .liveodds import live_odds

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    today = datetime.fromtimestamp(now, ET).date()
    out = []
    for t in p.get("tracked") or []:
        if t.get("auto"):
            continue
        q = engine.price_of(t["symbol"], 1800, now=now)
        if not q or not q[1] or not t.get("entry"):
            continue
        sym, side, price = t["symbol"], t.get("direction", "long"), q[1]
        pnl = _pnl(t, price)
        best = max(t.get("best") if t.get("best") is not None else pnl, pnl)
        r = _r(t)
        long = side == "long"
        stop = t.get("stop")
        at_risk = stop is not None and (stop < t["entry"] if long else stop > t["entry"])

        base = {"call": t["id"], "symbol": sym, "pnl": round(pnl, 2), "best": round(best, 2)}

        def add(
            kind: str, urgency: float, title: str, detail: str, do: dict[str, Any] | None, base=base
        ) -> None:
            out.append({**base, "kind": kind, "urgency": urgency, "title": title, "detail": detail, "do": do})

        be = {
            "label": "Stop to breakeven",
            "action": "track_levels",
            "body": {"id": t["id"], "stop": t["entry"]},
        }
        if r and at_risk and pnl >= r:
            add("breakeven", 74, f"{sym} is +{pnl / r:.1f}R ({_pc(pnl)}): move your stop to breakeven?",
                f"{side} from {_px(t['entry'])} with the stop at {_px(stop)} (1R = {r:.1f}%). A stop at the entry and it can't lose.",
                be)  # fmt: skip
        elif best >= max(GIVEBACK_MIN, r or 0) and pnl <= GIVEBACK_SHARE * best:
            keep = pnl > 0.1 and at_risk
            add("giveback", 76, f"{sym} gave back most of its gain: {_pc(best)} at best, {_pc(pnl)} now",
                f"{side} from {_px(t['entry'])} · now {_px(price)}. " + ("Lock in the entry, or close it." if keep else "Close it, or give it its stop."),
                be if keep else {"label": "Close it", "action": "track_close", "body": {"id": t["id"], "lesson": "gave back the gain"}})  # fmt: skip
        if now >= exit_time("d1", float(t.get("ts") or now), engine.calendar) and abs(pnl) < DEAD_BAND:
            why = ""
            a = engine.storage.get_alert(t["alert_id"]) if t.get("alert_id") else None
            cb = ((a or {}).get("edge") or {}).get("cat_base")
            if cb and cb.get("close"):
                why = f" {cb['label']} news usually does its move by the close (median {cb['close']['median']:+.1f}%, {cb['close']['n']} graded)."
            days = (now - float(t.get("ts") or now)) / 86400
            add("dead", 50, f"{sym} is flat {days:.0f} day{'s' if days >= 1.5 else ''} on ({_pc(pnl)}): dead money?",
                f"{side} from {_px(t['entry'])} · now {_px(price)}.{why}",
                {"label": "Close it", "action": "track_close", "body": {"id": t["id"], "lesson": "dead money"}})  # fmt: skip
        a = engine.storage.get_alert(t["alert_id"]) if t.get("alert_id") else None
        lo = live_odds(engine, a, now) if a is not None else None
        aligned = a is not None and long == (_direction(a) == "up")  # the call is on the alert's side
        if lo and aligned and lo["p"] <= CUT_ODDS:
            add("cut", 72, f"{sym}: {lo['hours']:g} hours in and {_pc(lo['since'])} its way. Alerts like this finished their way {round(lo['p'] * 100)}%",
                f"Of {lo['from']} that was {lo['band']} after an hour, {round(lo['p'] * 100)}% finished the day its way (median {lo['median']:+.1f}%, {lo['n']}).",
                {"label": "Close it", "action": "track_close", "body": {"id": t["id"], "lesson": "cut on the live odds"}})  # fmt: skip
        e = book.lookup(sym, today) if book is not None else None
        if e and e.get("in_days") in (0, 1):
            when = (" " + e["when"]) if e.get("when") else ""
            add("event", 70, f"{sym} reports{when} {'today' if e['in_days'] == 0 else 'tomorrow'}, and you hold a paper {side}",
                f"{_pc(pnl)} so far. A report can gap it past your stop.", None)  # fmt: skip
    return out


async def check_manage(engine: Engine, now: float | None = None) -> int:
    """Every minute: track each open call's best and worst, and push each new nudge once."""
    now = now or time.time()
    sent = 0
    for dev, p in list(engine.desk.profiles.items()):
        calls = [t for t in p.get("tracked") or [] if not t.get("auto")]
        if not calls:
            continue
        changed = False
        for t in calls:
            q = engine.price_of(t["symbol"], 1800, now=now)
            if not q or not q[1] or not t.get("entry"):
                continue
            pnl = round(_pnl(t, q[1]), 2)
            if t.get("best") is None or pnl > t["best"]:
                t["best"], changed = pnl, True
            if t.get("worst") is None or pnl < t["worst"]:
                t["worst"], changed = pnl, True
            if t.get("stop") is not None and "risk_stop" not in t and _r(t):
                t["risk_stop"], changed = t["stop"], True  # 1R stays the original risk after the stop moves
        if changed:
            engine.desk.save(dev)
        if not (p.get("prefs") or {}).get("manage", True) or engine.webpush is None:
            continue
        by_id = {t["id"]: t for t in calls}
        for n in nudges(engine, dev, now):
            t = by_id.get(n["call"])
            if t is None or n["kind"] in (t.get("nudged") or []):
                continue
            t.setdefault("nudged", []).append(n["kind"])
            engine.desk.save(dev)
            probe = {"kind": "news", "severity": "HIGH", "tickers": [n["symbol"]], "edge": {}}
            if not engine.desk.allows(dev, probe, now):
                continue
            icon = {"breakeven": "🛡", "giveback": "↘", "dead": "💤", "event": "📅", "cut": "✂"}[n["kind"]]
            payload = {"id": f"mg-{t['id']}-{n['kind']}", "title": f"{icon} {n['title']}"[:160], "body": n["detail"][:300],
                       "severity": "HIGH", "kind": "system", "tickers": [n["symbol"]], "tag": f"mg-{t['id']}",
                       "ts": now, "open": f"/app/#t={n['symbol']}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    return sent
