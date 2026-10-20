"""Equity curves: a strategy judged the way it's lived, call by call, with its drawdowns.

A win rate and an average say how a strategy does on a typical call; the curve says what it's like
to follow it. This builds, from closed paper calls, the cumulative return (each call the same size:
the sum of their returns, in order of closing) and from it the deepest drawdown (the largest fall
from a high), the win rate, the average, and a per-call Sharpe ratio (the average over the spread
of results). One curve for the phone's own calls, one per autopilot, and one for "every alert at
the close" (every graded alert of the period, its way, as if each were followed), beside SPY over
the same days (from market memory's daily closes). Paper only. History, not a forecast.
"""

from __future__ import annotations

import json
import statistics as st
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MAX_POINTS = 120


def curve(results: list[tuple[float, float]]) -> dict[str, Any]:
    """[(when, return %)] -> the curve and its risk numbers."""
    results = sorted(results)
    if not results:
        return {"n": 0, "points": []}
    cum = peak = dd = 0.0
    pts = []
    for ts, r in results:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        pts.append([ts, round(cum, 2)])
    rets = [r for _, r in results]
    sd = st.pstdev(rets) if len(rets) > 1 else 0.0
    step = max(1, len(pts) // MAX_POINTS)
    thin = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
    return {"n": len(rets), "total": round(cum, 2), "max_dd": round(dd, 2), "win": round(sum(r > 0 for r in rets) / len(rets), 2),
            "avg": round(sum(rets) / len(rets), 2), "sharpe": round(st.mean(rets) / sd, 2) if sd else None,
            "best": round(max(rets), 2), "worst": round(min(rets), 2), "from": results[0][0], "to": results[-1][0],
            "points": thin}  # fmt: skip


def spy_curve(engine: Engine, since: float, now: float) -> dict[str, Any]:
    """SPY's daily closes from market memory over the same days, as a cumulative curve."""
    d0 = datetime.fromtimestamp(since, ET).date().isoformat()
    rows = engine.storage.db.execute(
        "SELECT key, value FROM settings WHERE key >= ? AND key LIKE 'memory:%' ORDER BY key",
        (f"memory:{d0}",),
    ).fetchall()
    days = []
    for k, v in rows:
        chg = (json.loads(v).get("indexes") or {}).get("SPY")
        if chg is not None:
            d = datetime.fromisoformat(k.split(":", 1)[1]).replace(hour=16, tzinfo=ET).timestamp()
            if d <= now:
                days.append((d, float(chg)))
    out = curve(days)
    out.pop("win", None)
    return out


def equity(engine: Engine, device: str, days: float = 90, now: float | None = None) -> dict[str, Any]:
    from .autopilot import rule_label

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    since = now - days * 86400
    closed = [
        c for c in p.get("closed") or [] if c.get("pnl_pct") is not None and (c.get("closed") or 0) >= since
    ]
    mine = curve([(c["closed"], c["pnl_pct"]) for c in closed if not c.get("auto")])
    autos = []
    for rule in p.get("autopilots") or []:
        cv = curve([(c["closed"], c["pnl_pct"]) for c in closed if c.get("auto") == rule["id"]])
        if cv["n"]:
            autos.append({"id": rule["id"], "label": rule.get("label") or rule_label(rule), **cv})
    every = []
    for a in engine.storage.recent_alerts(5000, since=since):
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if row and row.get("sign") and row.get("close") is not None and a.get("kind") != "system":
            every.append((float(a["created"]), row["sign"] * row["close"]))
    starts = [x for x in (mine.get("from"), *(c["from"] for c in autos)) if x]
    first = min(starts) if starts else since
    return {"days": days, "yours": mine, "autopilots": autos, "every_alert": curve(every),
            "spy": spy_curve(engine, first, now)}  # fmt: skip
