"""The tilt guard: your calls after a losing streak, graded on your own record.

After two or three losers in a row, traders tend to press: bigger, faster, on worse setups, to win
it back. Whether you do is in your own record. Every paper call you opened yourself (not the
autopilots') is sorted by how many losers in a row you'd closed that day before you opened it:

- **None;**
- **One;**
- **Two or more.**

When your calls opened after two or more losses the same day lost on average (5+) and did worse than
your calls with none, that's your tilt. While you're on a streak of 2+ losers today, the pre-trade
check turns red the moment you go to open another: "You've closed 2 losers in a row today; your calls
opened after 2+ losses the same day made -0.8% (7), against +0.6% otherwise." On a streak without
that record it's a caution. Paper only; it never blocks the call.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 5
BUCKETS = {"0": "none", "1": "one", "2+": "two or more"}


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).date().isoformat()


def _streak(closed: list[dict[str, Any]], at: float) -> int:
    """Losers in a row closed the same day before ``at`` (newest first, until a winner)."""
    day = _day(at)
    n = 0
    for c in sorted((c for c in closed if (c.get("closed") or 0) < at and _day(c["closed"]) == day),
                    key=lambda c: -c["closed"]):  # fmt: skip
        if (c.get("pnl_pct") or 0) < 0:
            n += 1
        else:
            break
    return n


def record(p: dict[str, Any]) -> dict[str, Any]:
    """{bucket: {"n", "avg", "hit"}} for this phone's own closed calls, and whether it tilts."""
    mine = [
        c
        for c in p.get("closed") or []
        if not c.get("auto") and c.get("pnl_pct") is not None and c.get("closed")
    ]
    pools: dict[str, list[float]] = {}
    for c in mine:
        s = _streak(mine, float(c.get("ts") or c["closed"]))
        pools.setdefault("2+" if s >= 2 else str(s), []).append(float(c["pnl_pct"]))
    out: dict[str, Any] = {
        b: {
            "n": len(xs),
            "avg": round(sum(xs) / len(xs), 2),
            "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        }
        for b, xs in pools.items()
    }
    after, calm = out.get("2+"), out.get("0")
    out["tilts"] = bool(
        after and after["n"] >= MIN_N and after["avg"] < 0 and (not calm or after["avg"] < calm["avg"])
    )
    return out


def now_streak(p: dict[str, Any], now: float | None = None) -> int:
    mine = [
        c
        for c in p.get("closed") or []
        if not c.get("auto") and c.get("pnl_pct") is not None and c.get("closed")
    ]
    return _streak(mine, now or time.time())


def check(engine: Engine, device: str, now: float | None = None) -> dict[str, str] | None:
    """The pre-trade line, while this phone is on a streak of 2+ losers today."""
    p = engine.desk.profiles.get(device) or {}
    s = now_streak(p, now)
    if s < 2:
        return None
    r = record(p)
    head = f"You've closed {s} losers in a row today"
    after, calm = r.get("2+"), r.get("0")
    if r["tilts"]:
        vs = f", against {calm['avg']:+.1f}% otherwise" if calm else ""
        return {
            "tone": "bad",
            "text": f"{head}; your calls opened after 2+ losses the same day made {after['avg']:+.1f}% ({after['n']}){vs}",
        }
    if after and after["n"] >= MIN_N:
        return {
            "tone": "info",
            "text": f"{head}; your calls after 2+ losses have held up ({after['avg']:+.1f}%, {after['n']})",
        }
    return {
        "tone": "warn",
        "text": f"{head}: a moment to step back (your record after streaks is too thin to say more)",
    }
