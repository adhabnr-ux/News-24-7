"""Options against the news: are options pricing the move this kind of news usually makes?

When news lands, the stock's options are priced for an ordinary day. If this kind of news here
usually moves the stock much more than that, options are cheap for it (a long option, or a
straddle, is paying for less movement than the record says comes); if it moves less, they're rich.

At each alert on a stock whose chain was read in the last ten minutes, the two numbers are set side
by side in the same units, the typical size of a day's move either way (the median absolute move):

- **Options:** 30-day implied volatility as a typical day (IV30 / √252 × 0.674, the median absolute
  move of a normal day at that volatility); without IV30, the nearest straddle spread over its
  trading days (straddle / √days × 0.845, since a straddle prices the mean absolute move).
- **The news:** its kind's typical move by the close, either way, at the stock's size (catalysts.py).

The ratio reads **cheap** at 1.5× or more, **rich** at 0.8× or less, else fair. Both rest on
approximations (normal moves, a move from the alert to the close against a full day), so each
read is graded: did the stock's move by the close (either way) beat what options priced? A stock
with no fresh chain read gets one fetched (the sheet shows it; the push has already gone). History,
not a forecast; options are priced by people who may know more.
"""

from __future__ import annotations

import math
import statistics as st
import time
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

CHEAP, RICH = 1.5, 0.8
MIN_N = 8
MEDIAN_NORMAL = 0.674  # median |N(0,1)|
MEDIAN_OVER_MEAN = 0.845  # median |N| / mean |N| (a straddle prices the mean)


def implied_day(implied: dict[str, Any] | None) -> tuple[float, str] | None:
    """(the typical day's move either way that options price, in %, how it was read)."""
    if not implied or not implied.get("available"):
        return None
    iv = implied.get("iv30")
    if iv:
        return round(iv / math.sqrt(252) * MEDIAN_NORMAL, 2), f"IV30 {iv:.0f}%"
    for r in implied.get("rows") or []:
        dte = r.get("dte") or 0
        if dte >= 1 and r.get("move_pct"):
            days = max(1.0, dte * 5 / 7)
            return round(r["move_pct"] / math.sqrt(days) * MEDIAN_OVER_MEAN, 2), f"the {r['expiry']} straddle"
    return None


def read(implied: dict[str, Any] | None, cat_base: dict[str, Any] | None) -> dict[str, Any] | None:
    """{"implied", "typical", "ratio", "verdict", "from", "label"} or None without both numbers."""
    day = implied_day(implied)
    typical = (cat_base or {}).get("typical")
    if not day or not typical or day[0] <= 0:
        return None
    ratio = round(typical / day[0], 2)
    verdict = "cheap" if ratio >= CHEAP else "rich" if ratio <= RICH else "fair"
    return {"implied": day[0], "typical": typical, "ratio": ratio, "verdict": verdict, "from": day[1],
            "label": (cat_base or {}).get("label") or "this kind of news"}  # fmt: skip


def vol_line(v: dict[str, Any] | None, rec: dict[str, Any] | None = None) -> str:
    """'🎯 Options price ±1.3% a typical day (IV30 31%); contract win news here moves ±4.0% by the close: cheap for it'."""
    if not v or v["verdict"] == "fair":
        return ""
    tail = ""
    if rec and rec.get("n", 0) >= MIN_N:
        tail = f" (on {v['verdict']} reads here the move beat what options priced {round(rec['beat'] * 100)}% of the time, {rec['n']})"
    word = "cheap for it" if v["verdict"] == "cheap" else "rich for it"
    who = v["label"][:1].lower() + v["label"][1:]
    return f"🎯 Options price ±{v['implied']:.1f}% a typical day ({v['from']}); {who} news here moves ±{v['typical']:.1f}% by the close: {word}{tail}"


def vol_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{verdict: {"n", "beat", "ratio"}}: how often the move by the close (either way) beat what options
    priced for a day, and the median of the two's ratio."""
    now = now or time.time()
    pools: dict[str, list[tuple[float, float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        v, o = e.get("vol") or {}, e.get("outcome") or {}
        if not v or not o:
            continue
        row = next(iter(o.values()))
        if row.get("close") is None:
            continue
        pools.setdefault(v["verdict"], []).append((abs(row["close"]), v["implied"]))
    return {k: {"verdict": k, "n": len(xs), "beat": round(sum(r > i for r, i in xs) / len(xs), 2),
                "ratio": round(st.median(r / i for r, i in xs), 2)} for k, xs in pools.items()}  # fmt: skip


async def stamp_later(engine: Engine, alert_id: str, sym: str, cat_base: dict[str, Any] | None) -> None:
    """Read the chain now (it wasn't fresh at the alert) and stamp the read on the stored alert."""
    from .personal import implied_move

    implied = await implied_move(engine, sym)
    v = read(implied, cat_base)
    if v:
        engine.storage.update_alert_edge(alert_id, {"vol": v})


def board(engine: Engine, rates: dict[str, Any]) -> dict[str, Any]:
    """The record by verdict, and today's alerts with a read."""
    t0 = time.time() - 86400
    today = []
    for a in engine.storage.recent_alerts(300, since=t0):
        v = (a.get("edge") or {}).get("vol")
        if v and v["verdict"] != "fair":
            today.append(
                {"id": a["id"], "symbol": (a.get("tickers") or [""])[0], "title": a.get("title", ""), **v}
            )
    return {"rates": [rates[k] for k in ("cheap", "fair", "rich") if k in rates], "today": today[:10], "need": MIN_N,
            "date": date.today().isoformat()}  # fmt: skip
