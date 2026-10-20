"""The playbook's blind spots: the slices of its own track record where it loses.

A playbook picked per kind of news can still lose in places the kind doesn't describe: late-day
alerts, Friday news, shorts, small caps, MEDIUM alerts. Its track record (pbcurve.py: every stamped,
graded alert, taken the playbook's way) is sliced every hour by:

- **time of day:** before the open, the first hour, midday, the last hour, after the close;
- **day of the week;**
- **direction:** long or short;
- **company size;**
- **severity;**
- **the market:** the regime (risk-on, risk-off, mixed) when the alert went out;
- **today's tape:** cold, normal or hot (tapeday.py), for alerts after it was known that day;
- **macro days:** a Fed or jobs day, or an ordinary one.

A slice is a **blind spot** when the playbook lost there on 20+ alerts, lost in both the older and the
newer half of them (so one bad stretch doesn't make a rule), and makes money overall. The playbook
sits blind spots out: the autopilot passes, the board says why, and the Watch tab lists them with
their numbers. A slice that recovers drops off on its own. History, not a forecast.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 20
HOURS = ((0, 9.5, "before the open"), (9.5, 10.5, "the first hour"), (10.5, 15, "midday"), (15, 16, "the last hour"),
         (16, 24, "after the close"))  # fmt: skip
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DIMS = {
    "hour": "Time of day",
    "day": "Day of the week",
    "dir": "Direction",
    "band": "Company size",
    "sev": "Severity",
    "regime": "The market",
    "tape": "Today's tape",
    "macro": "Macro day",
}
TAPE = {"cold": "a cold tape", "normal": "a normal tape", "hot": "a hot tape"}


def slices(a: dict[str, Any], tape: str | None = None) -> dict[str, str]:
    """{dimension: value} for an alert, from what was known when it went out (``tape``: the day's tape
    state, when it was already known at the alert)."""
    from .dossier import _direction

    dt = datetime.fromtimestamp(float(a.get("created") or 0), ET)
    hh = dt.hour + dt.minute / 60
    out = {"hour": next(lbl for lo, hi, lbl in HOURS if lo <= hh < hi), "day": DAYS[dt.weekday()]}
    d = _direction(a)
    if d in ("up", "down"):
        out["dir"] = "long" if d == "up" else "short"
    band = ((a.get("edge") or {}).get("catalyst") or {}).get("band")
    if band:
        out["band"] = band
    if a.get("severity"):
        out["sev"] = str(a["severity"])
    e = a.get("edge") or {}
    if e.get("regime") in ("risk-on", "risk-off", "mixed"):
        out["regime"] = e["regime"]
    if "macro" in e or "event_night" in e:  # stamped in the era that notes macro days
        out["macro"] = "a Fed or jobs day" if e.get("macro") else "an ordinary day"
    if tape in TAPE:
        out["tape"] = TAPE[tape]
    return out


def tape_known(engine: Engine, since: float, until: float) -> dict[str, tuple[str, float]]:
    """{date: (state, from when it was known)}: each session's tape, known once its first alerts' hour
    was in (tapeday.py), so an alert only gets the state it could have known."""
    from .tapeday import FIRST, _days, state_of

    out = {}
    for d, xs in _days(engine, since, until).items():
        st = state_of(xs)
        if st:
            out[d] = (st, float(xs[FIRST - 1]["created"]) + 3600)
    return out


def _avg(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 2)


def blind_spots(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"overall": {...}, "spots": [{"dim", "value", "label", "n", "avg", "older", "newer"}]}."""
    from .pbcurve import taken

    now = now or time.time()
    rows: list[tuple[float, float, dict[str, str]]] = []
    tapes = tape_known(engine, now - days * 86400, now)
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        got = taken(a, counterfactual=True)
        if got:
            t = float(a["created"])
            st, known = tapes.get(datetime.fromtimestamp(t, ET).date().isoformat(), (None, 0.0))
            rows.append((t, got[0], slices(a, st if st and t >= known else None)))
    if not rows:
        return {"overall": None, "spots": []}
    rows.sort(key=lambda r: r[0])
    overall = _avg([r[1] for r in rows])
    pools: dict[tuple[str, str], list[float]] = {}
    for _, x, sl in rows:
        for dim, val in sl.items():
            pools.setdefault((dim, val), []).append(x)
    spots = []
    if overall > 0:
        for (dim, val), xs in pools.items():
            if len(xs) < MIN_N:
                continue
            half = len(xs) // 2
            older, newer = _avg(xs[:half]), _avg(xs[half:])
            if _avg(xs) < 0 and older < 0 and newer < 0:
                spots.append({"dim": dim, "value": val, "label": f"{DIMS[dim]}: {val}", "n": len(xs), "avg": _avg(xs),
                              "older": older, "newer": newer})  # fmt: skip
    spots.sort(key=lambda s: s["avg"])
    return {"overall": {"n": len(rows), "avg": overall}, "spots": spots}


def hit(rates: dict[str, Any], a: dict[str, Any], tape: str | None = None) -> dict[str, Any] | None:
    """The blind spot this alert falls in, if any (``tape``: today's tape state, once known)."""
    sl = slices(a, tape)
    return next((s for s in rates.get("spots") or [] if sl.get(s["dim"]) == s["value"]), None)


def why(s: dict[str, Any]) -> str:
    return (f"a blind spot of the playbook ({s['label'].lower()}: {s['avg']:+.2f}% an alert on {s['n']}, "
            f"losing in both halves of its record)")  # fmt: skip
