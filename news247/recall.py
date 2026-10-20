"""What we caught, what we missed: every big move of the session, and whether an alert got there.

Every other record here grades the alerts that went out. This grades the other direction: of the
stocks that actually moved, how many did an alert catch? Each session, every company worth $300M+
that moved 5%+ on the whole-market tape is checked against the alerts on it since the previous
close:

- **Caught:** an alert on it (any lane: a headline, the radar, options, insiders, a confluence) and
  how far ahead of its extreme (the session's high for a rise, low for a drop) the first one came.
- **Missed, headline under the line:** a headline naming it was read but scored under the alert line
  (the score is kept: the threshold may be too high for that kind of news).
- **Missed, no headline:** nothing named it in the feeds (a source gap, or a move with no news).

A snapshot is kept for each session at 16:05-17:30 ET; the board reads the last 20 sessions (catch
rate by company size, lead time, why the misses missed, the latest misses) and, during the session,
today's movers so far. Nothing is estimated: a move is the tape's, a catch is an alert on file.
"""

from __future__ import annotations

import json
import statistics as st
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_MOVE = 5.0
MIN_CAP = 300e6
KEEP_DAYS = 60


def movers(engine: Engine, now: float | None = None, max_age_s: float = 1800) -> list[dict[str, Any]]:
    """Today's movers: each $300M+ common stock 5%+ on the day, with whether an alert caught it."""
    now = now or time.time()
    since = _session_start(engine, now)
    out = []
    for sym, q in list(engine.live_quotes.quotes.items()):
        if now - q.ts > max_age_s or q.change_pct is None or abs(q.change_pct) < MIN_MOVE:
            continue
        li = engine.universe.get(sym)
        if li is None or not li.common or (li.market_cap or 0) < MIN_CAP:
            continue
        out.append(_check(engine, sym, q.change_pct, li, since, now))
    out.sort(key=lambda r: -abs(r["chg"]))
    return out


def _session_start(engine: Engine, now: float) -> float:
    """The close before today's session: news since then counts for today's move (after 16:00 too,
    when the latest close is today's own)."""
    from .edge import Calendar

    bell = datetime.fromtimestamp(now, ET).replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
    return Calendar.last_close(min(now - 60, bell), engine.calendar.holidays)


def _check(engine: Engine, sym: str, chg: float, li: Any, since: float, now: float) -> dict[str, Any]:
    alerts = [
        a for a in engine.storage.alerts_for_ticker(sym, since=since, limit=40) if a.get("kind") != "system"
    ]
    path = engine.live_quotes.path(sym, now)
    extreme_ts = (path.high_ts if chg > 0 else path.low_ts) if path is not None else None
    row: dict[str, Any] = {"symbol": sym, "name": li.name, "chg": round(chg, 2), "band": li.band, "sector": li.sector,
                           "caught": bool(alerts), "extreme_ts": extreme_ts}  # fmt: skip
    if alerts:
        first = min(alerts, key=lambda a: a["created"])
        row.update(first_alert=first["created"], alert_id=first["id"], alert_title=first.get("title", ""),
                   alert_kind=first.get("kind"))  # fmt: skip
        if extreme_ts:
            row["lead_min"] = round((extreme_ts - first["created"]) / 60, 1)  # + = before the extreme
        row["why"] = "alerted"
        return row
    from .uptime import asleep_at

    if asleep_at(engine, extreme_ts or now):  # the desk was down when it moved: a sleep, not a blind spot
        row["why"] = "asleep"
        return row
    items = engine.storage.items_for_ticker(sym, since=since, limit=40)
    best = max(items, key=lambda it: it.get("score") or 0, default=None)
    if best is not None:
        row.update(why="under the line", top_score=round(best.get("score") or 0, 1), top_title=best.get("title", ""),
                   top_source=best.get("source", ""))  # fmt: skip
    else:
        row["why"] = "no headline"
    return row


def snapshot(engine: Engine, now: float | None = None) -> int:
    """16:05-17:30 ET on a market day: keep the session's movers (once). Returns movers kept."""
    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    day = dt.date().isoformat()
    if (
        dt.weekday() >= 5
        or day in engine.calendar.holidays
        or not ((16, 5) <= (dt.hour, dt.minute) < (17, 30))
    ):
        return 0
    key = f"recall:{day}"
    if engine.storage.get_setting(key):
        return 0
    rows = movers(engine, now, max_age_s=3 * 3600)
    engine.storage.set_setting(key, json.dumps({"day": day, "rows": rows}))
    old = (dt - timedelta(days=KEEP_DAYS)).date().isoformat()
    engine.storage.db.execute(
        "DELETE FROM settings WHERE key LIKE 'recall:%' AND key < ?", (f"recall:{old}",)
    )
    return len(rows)


def _rate(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return {
        "n": len(rows),
        "caught": sum(r["caught"] for r in rows),
        "rate": round(sum(r["caught"] for r in rows) / len(rows), 2),
    }


def recall_report(engine: Engine, days: int = 20, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    rows_db = engine.storage.db.execute(
        "SELECT value FROM settings WHERE key LIKE 'recall:%' ORDER BY key DESC LIMIT ?", (days,)
    ).fetchall()
    sessions = [json.loads(v) for (v,) in rows_db]
    rows = [r for s in sessions for r in s["rows"]]
    leads = [r["lead_min"] for r in rows if r.get("lead_min") is not None]
    missed = [r for r in rows if not r["caught"]]
    under = [
        r["top_score"] for r in missed if r.get("why") == "under the line" and r.get("top_score") is not None
    ]
    out: dict[str, Any] = {"sessions": len(sessions), "all": _rate(rows),
                           "bands": [{"band": b, **(_rate([r for r in rows if r["band"] == b]) or {})} for b in sorted({r["band"] for r in rows})],
                           "lead": {"n": len(leads), "median": round(st.median(leads), 1),
                                    "before": round(sum(x > 0 for x in leads) / len(leads), 2)} if leads else None,
                           "why": {k: sum(r.get("why") == k for r in missed) for k in ("under the line", "no headline", "asleep")},
                           "under_score": round(st.median(under), 1) if under else None,
                           "missed": [{**r, "day": s["day"]} for s in sessions for r in s["rows"] if not r["caught"]][:15],
                           "today": movers(engine, now)}  # fmt: skip
    t = out["today"]
    out["today_rate"] = _rate(t)
    return out
