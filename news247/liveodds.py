"""Live odds: once an alert is an hour old, how it has done so far says a lot about how it ends.

Every graded alert has a move an hour later and a move at the close, both in its direction. Pooled,
they answer "of the alerts that were up 1%+ its way after an hour, how many finished the day its
way?", per kind of news when it has enough history and over every alert otherwise. For an alert
between an hour old and its close, its move so far picks the row: "1 hour in, +1.2%: alerts like
this finished their way 88% of the time (34)", or "−1.0%: only 22% (41)". A trader's hold-or-cut
question with a number on it. History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

BINS = (
    (-1e9, -1.0, "down 1%+ against it"),
    (-1.0, -0.25, "a little against it"),
    (-0.25, 0.25, "flat"),
    (0.25, 1.0, "a little its way"),
    (1.0, 1e9, "up 1%+ its way"),
)
MIN_BIN = 8  # graded alerts in a row before it is used


def _bin(x: float) -> int:
    return next(i for i, (lo, hi, _) in enumerate(BINS) if lo <= x < hi)


def h1_table(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"*": [row per bin], "contract": [...]}: for each move-after-an-hour band, how many went on to
    finish the day its way, and the median close."""
    from .catalysts import classify

    now = now or time.time()
    pools: dict[str, list[list[float]]] = {}
    for a in engine.storage.recent_alerts(6000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("h1") is None or row.get("close") is None:
            continue
        h1, close = row["sign"] * row["h1"], row["sign"] * row["close"]
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for key in ("*", c["key"]) if c else ("*",):
            pools.setdefault(key, [[] for _ in BINS])[_bin(h1)].append(close)
    out: dict[str, Any] = {}
    for key, bins in pools.items():
        out[key] = [
            {"label": BINS[i][2], "n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)}
            if xs else {"label": BINS[i][2], "n": 0}
            for i, xs in enumerate(bins)
        ]  # fmt: skip
    return out


def live_odds(engine: Engine, a: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """For an alert between an hour old and its session's close: its move so far, and how alerts
    that had done the same after an hour finished the day. None outside that window or unread."""
    from .catalysts import LABELS, classify
    from .dossier import _direction, _since, session_closes

    now = now or time.time()
    created = float(a.get("created") or 0)
    if now - created < 3600:
        return None
    close_ts = session_closes(created, 1, getattr(engine.calendar, "holidays", set()))[0]
    if now >= close_ts:
        return None
    d = _direction(a)
    sym = (((a.get("edge") or {}).get("play") or {}).get("direct") or a.get("tickers") or [""])[0]
    if not d or not sym:
        return None
    since = _since(a, sym, lambda s: engine.price_of(s, 1800, now=now))
    if since is None:
        return None
    its_way = round(since if d == "up" else -since, 2)
    table = getattr(engine, "h1_rates", None) or {}
    c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
    i = _bin(its_way)
    for key in (c["key"], "*") if c else ("*",):
        row = (table.get(key) or [None] * len(BINS))[i]
        if row and row.get("n", 0) >= MIN_BIN:
            return {"symbol": sym, "since": its_way, "hours": round((now - created) / 3600, 1), "p": row["hit"], "n": row["n"],
                    "median": row["median"], "band": row["label"],
                    "from": f"{LABELS.get(key, key).lower()} news" if key != "*" else "every alert"}  # fmt: skip
    return None


def live_line(lo: dict[str, Any] | None) -> str:
    """ "2.1 hours in, +1.2% its way: alerts like this finished their way 88% of the time (34)"."""
    if not lo:
        return ""
    return (f"{lo['hours']:g} hours in, {lo['since']:+.1f}% its way: {lo['from']} that was {lo['band']} after an hour "
            f"finished its way {round(lo['p'] * 100)}% of the time ({lo['n']})")  # fmt: skip
