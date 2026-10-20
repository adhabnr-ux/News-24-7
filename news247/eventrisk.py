"""Event risk on overnight holds: don't carry a news trade into the stock's own earnings by accident.

The playbook holds some kinds of news into the next day or the week because, on average, they keep
going. That average was earned on ordinary nights. A night the stock reports earnings (after
today's close, or before tomorrow's open), or has a scheduled binary event (an FDA date, a
readout), is a coin toss of a different size, whatever the news was.

So every alert now notes whether the night after its session is an **event night** for its stock
(Nasdaq's earnings calendar; the desk's own captured binary events). Those holds are graded
separately (close to the next close, its way). Until there are 8+ graded, and as long as they don't
pay, the playbook doesn't hold through one:

- the playbook autopilot exits such calls at 15:50 ET, with the reason;
- the playbook today board and *Coach my calls* say so before the bell;
- with 8+ graded event nights that paid on average, the hold stands, and the board says that too.

That's a stated policy until the record has its say, not a finding. History, not a forecast.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 8


def _next_session(d: date, holidays: set[str]) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5 or n.isoformat() in holidays:
        n += timedelta(days=1)
    return n


def tonight(engine: Engine, sym: str, ts: float) -> dict[str, Any] | None:
    """The event between the close of the session at ``ts`` and the next close, for ``sym``: earnings
    after today's close or before (or on) the next session, or a binary event dated the next session."""
    from .desk import session_edge

    close = session_edge("close", ts - 31, engine.calendar)
    day = datetime.fromtimestamp(close, ET).date()
    nxt = _next_session(day, set(getattr(engine.calendar, "holidays", set()) or ()))
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    if book is not None:
        e = book.lookup(sym, day)
        if e:
            when = e.get("when") or ""
            if (e["date"] == day.isoformat() and when == "after the close") or (
                e["date"] == nxt.isoformat() and when != "after the close"
            ):
                return {"what": "earnings", "date": e["date"], "when": when or "time not given",
                        "line": f"{sym} reports earnings {when or 'on ' + e['date']}"}  # fmt: skip
    for ev in engine.storage.catalysts(nxt.isoformat()):
        if ev.get("symbol") == sym and ev.get("date") == nxt.isoformat():
            return {"what": "event", "date": ev["date"], "when": "", "title": (ev.get("title") or "")[:100],
                    "line": f"{sym} has a scheduled event on {ev['date']}: {(ev.get('title') or 'a binary event')[:80]}"}  # fmt: skip
    return None


def night_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"event": {"n", "avg", "hit", "abs"}, "normal": {...}}: overnight moves, close to next close, its way."""
    now = now or time.time()
    pools: dict[str, list[float]] = {"event": [], "normal": []}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        o = e.get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if (
            not row
            or not row.get("sign")
            or row.get("close") is None
            or row.get("d1") is None
            or "event_night" not in e
        ):
            continue
        night = row["sign"] * ((1 + row["d1"] / 100) / (1 + row["close"] / 100) - 1) * 100
        pools["event" if e["event_night"] else "normal"].append(night)
    out = {}
    for k, xs in pools.items():
        if xs:
            out[k] = {"n": len(xs), "avg": round(sum(xs) / len(xs), 2), "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                      "abs": round(sum(abs(x) for x in xs) / len(xs), 2)}  # fmt: skip
    return out


def verdict(engine: Engine, a: dict[str, Any], sym: str) -> dict[str, Any] | None:
    """{"exit", "line", "event"} for holding this alert's call through tonight; None on an ordinary night."""
    ev = (a.get("edge") or {}).get("event_night")
    if ev is None:  # stamped before this record existed: look it up now
        ev = tonight(engine, sym, float(a["created"]))
    if not ev:
        return None
    rec = (getattr(engine, "night_rates", None) or {}).get("event")
    if rec and rec["n"] >= MIN_N and rec["avg"] > 0:
        return {
            "exit": False,
            "event": ev,
            "line": f"{ev['line']}; holds through event nights paid here ({rec['avg']:+.1f}% on average, {rec['n']}): the hold stands.",
        }
    why = (f"holds through event nights went {rec['avg']:+.1f}% here ({rec['n']})" if rec and rec["n"] >= MIN_N
           else "the playbook doesn't hold through one until its record says it pays")  # fmt: skip
    return {"exit": True, "event": ev, "line": f"{ev['line']}: {why}. Out at the close instead."}
