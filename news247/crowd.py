"""Crowded days: the third chip-maker alert of the morning is not the first.

When several stocks in one sector get news the same day pointing the same way (a round of contract
awards, an industry-wide guidance cut, a sector rally with headlines on every name) the desk sends
an alert for each. Each looks like its own trade. They're one bet, and the later ones may already
be priced in by the move in the first. Each alert now knows how many directional alerts on stocks
in the same sector, the same way, came before it that day:

- **The first** of its sector that day;
- **The second;**
- **The third or later** (a crowded day).

The record says how each went its way by the close, overall and in its sector when that has the
counts (8+), and the alert's sheet flags the 2nd and later ("👥 The 3rd semiconductor alert today,
the same way. The 3rd or later went their way 44% here (31); the first 61% (120)").

And the playbook autopilot keeps it one bet: it won't open a 3rd paper call on the same sector, the
same way, on the same phone, while two are open. History, not a forecast.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 8
MAX_SAME = 2  # open playbook calls on one sector, one way, per phone
SPOTS = {"first": "the first", "second": "the second", "crowded": "the third or later"}


def spot(n_before: int) -> str:
    return "first" if n_before == 0 else "second" if n_before == 1 else "crowded"


def _sector(engine: Engine, a: dict[str, Any]) -> str:
    o = (a.get("edge") or {}).get("outcome") or {}
    sym = next(iter(o), None) or ((a.get("tickers") or [""])[0])
    li = engine.universe.get(sym) if sym and getattr(engine, "universe", None) is not None else None
    return (getattr(li, "sector", "") or "") if li else ""


def _way(a: dict[str, Any]) -> str:
    from .dossier import _direction

    return _direction(a) or ""


def crowd_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"*" or sector: {spot: {"n", "hit", "avg"}}}: how the first, second and later alerts in a sector
    on one day (the same way) did by the close, its way."""
    now = now or time.time()
    alerts = [
        a for a in engine.storage.recent_alerts(8000, since=now - days * 86400) if a.get("kind") != "system"
    ]
    alerts.sort(key=lambda a: a["created"])
    seen: dict[tuple[str, str, str], int] = {}
    pools: dict[str, dict[str, list[float]]] = {}
    for a in alerts:
        way, sec = _way(a), _sector(engine, a)
        if way not in ("up", "down") or not sec:
            continue
        day = datetime.fromtimestamp(a["created"], ET).date().isoformat()
        k = (day, sec, way)
        n = seen.get(k, 0)
        seen[k] = n + 1
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("close") is None:
            continue
        x = row["sign"] * row["close"]
        for g in ("*", sec):
            pools.setdefault(g, {}).setdefault(spot(n), []).append(x)
    out: dict[str, Any] = {}
    for g, sp in pools.items():
        rows = {s: {"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "avg": round(sum(xs) / len(xs), 2)}
                for s, xs in sp.items()}  # fmt: skip
        if any(r["n"] >= MIN_N for s, r in rows.items() if s != "first"):
            out[g] = {"group": g, **rows}
    return out


def stamp(engine: Engine, a: dict[str, Any]) -> dict[str, Any] | None:
    """For a new alert: how many in its sector came first today, the same way, and the record."""
    way, sec = _way(a), _sector(engine, a)
    if way not in ("up", "down") or not sec:
        return None
    t = float(a.get("created") or time.time())
    day0 = datetime.fromtimestamp(t, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    before = [
        b for b in engine.storage.recent_alerts(600, since=day0)
        if b.get("id") != a.get("id") and b.get("kind") != "system" and float(b["created"]) < t
        and _way(b) == way and _sector(engine, b) == sec
    ]  # fmt: skip
    syms = sorted({((b.get("tickers") or [""])[0]) for b in before} - {""})
    out: dict[str, Any] = {
        "sector": sec,
        "n_before": len(before),
        "spot": spot(len(before)),
        "before": syms[:6],
    }
    rates = getattr(engine, "crowd_rates", None) or {}
    for g in (sec, "*"):
        r = rates.get(g) or {}
        if (r.get(out["spot"]) or {}).get("n", 0) >= MIN_N and (r.get("first") or {}).get("n", 0) >= MIN_N:
            out["rec"] = {"group": g, "here": r[out["spot"]], "first": r["first"]}
            break
    out["line"] = line(out)
    return out


def line(c: dict[str, Any] | None) -> str:
    if not c or c["spot"] == "first":
        return ""
    nth = {"second": "2nd"}.get(c["spot"], f"{c['n_before'] + 1}{'rd' if c['n_before'] + 1 == 3 else 'th'}")
    head = f"The {nth} {c['sector'].lower()} alert today, the same way"
    if c.get("before"):
        head += f" (after {', '.join(c['before'])})"
    r = c.get("rec")
    if r:
        where = "in its sector" if r["group"] != "*" else "here"
        head += (f". {SPOTS[c['spot']].capitalize()} went their way {round(r['here']['hit'] * 100)}% {where} ({r['here']['n']}); "
                 f"the first {round(r['first']['hit'] * 100)}% ({r['first']['n']})")  # fmt: skip
    return head + "."


def too_crowded(engine: Engine, p: dict[str, Any], alert: dict[str, Any], rule_id: str) -> bool:
    """True when this phone already has MAX_SAME open calls from this rule on the alert's sector, the same way."""
    way, sec = _way(alert), _sector(engine, alert)
    if not sec:
        return False
    want = "long" if way == "up" else "short"
    n = 0
    for t in p.get("tracked") or []:
        if t.get("auto") != rule_id or t.get("direction") != want:
            continue
        li = engine.universe.get(t["symbol"]) if getattr(engine, "universe", None) is not None else None
        if li is not None and getattr(li, "sector", "") == sec:
            n += 1
    return n >= MAX_SAME
