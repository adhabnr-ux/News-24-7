"""Macro days: how news trades on Fed days, jobs-report days and other big scheduled releases.

On the day of an FOMC decision or a payrolls report, the whole market moves on one number and
single-stock news competes with it: a contract win at 13:55 meets a Fed statement at 14:00. The
calendar keeps those days (data/calendar.yaml: the Fed, the big data releases, elections, with
their impact), so every graded alert can be split by whether its session was one of them, and each
kind of news gets a record for both: "contract win news on macro days: 41% went its way by the
close (22) vs 63% on other days (140)".

When today is a macro day, a new alert names it with its kind's macro-day record (when 8+ are
graded), the pre-trade check carries it, and the call counts a poor macro-day record (40% or less)
as a fact against. The Watch tab lists every kind both ways, and the next macro day. History, not a
forecast.
"""

from __future__ import annotations

import statistics as st
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
KINDS = ("fed", "data", "politics")
MIN_N = 8


def macro_days(engine: Engine) -> dict[str, list[str]]:
    """{date: [titles]} for every scheduled release that moves the whole market (impact 3, or 2 for
    the Fed and data), past and future."""
    out: dict[str, list[str]] = {}
    for e in getattr(engine.calendar, "events", []) or []:
        if e.get("kind") not in KINDS:
            continue
        impact = e.get("impact") or 1
        if impact < 3 and not (impact >= 2 and e.get("kind") in ("fed", "data")):
            continue
        d = e["date"].isoformat() if isinstance(e["date"], date) else str(e["date"])
        out.setdefault(d, []).append(str(e.get("title") or ""))
    return out


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def macro_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{kind: {"key", "label", "macro": stats, "other": stats}} for "*" and each kind of news."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    mdays = set(macro_days(engine))
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if not row.get("sign") or row.get("close") is None:
            continue
        side = (
            "macro"
            if datetime.fromtimestamp(float(a["created"]), ET).date().isoformat() in mdays
            else "other"
        )
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for key in ("*", c["key"]) if c else ("*",):
            pools.setdefault(key, {"macro": [], "other": []})[side].append(row["sign"] * row["close"])
    return {k: {"key": k, "label": "Every alert" if k == "*" else LABELS.get(k, k), "macro": _stats(g["macro"]), "other": _stats(g["other"])}
            for k, g in pools.items()}  # fmt: skip


def today(engine: Engine, now: float | None = None) -> list[str]:
    """Today's macro releases, if today is a macro day."""
    d = datetime.fromtimestamp(now or time.time(), ET).date().isoformat()
    return macro_days(engine).get(d, [])


def macro_for(rates: dict[str, Any], kind: str | None, titles: list[str]) -> dict[str, Any] | None:
    """On a macro day: its kind's macro-day record (else every alert's) with 8+ graded."""
    if not titles:
        return None
    for k in (kind, "*"):
        r = rates.get(k or "")
        if r and r.get("macro") and r["macro"]["n"] >= MIN_N:
            return {**r, "today": titles}
    return None


def macro_line(r: dict[str, Any] | None) -> str:
    """'📅 FOMC decision today: contract win news on macro days went its way 41% (22) vs 63% on other days (140)'."""
    if not r:
        return ""
    who = "news" if r["key"] == "*" else f"{r['label'][:1].lower()}{r['label'][1:]} news"
    other = (
        f" vs {round(r['other']['hit'] * 100)}% on other days ({r['other']['n']})" if r.get("other") else ""
    )
    return f"📅 {r['today'][0]} today: {who} on macro days went its way {round(r['macro']['hit'] * 100)}% ({r['macro']['n']}){other}"


def macro_board(engine: Engine, rates: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    d0 = datetime.fromtimestamp(now, ET).date().isoformat()
    upcoming = sorted((d, t) for d, t in macro_days(engine).items() if d >= d0)
    rows = sorted((r for r in rates.values() if r.get("macro")), key=lambda r: -r["macro"]["n"])
    return {"rows": rows, "next": {"date": upcoming[0][0], "titles": upcoming[0][1]} if upcoming else None,
            "today": today(engine, now), "need": MIN_N}  # fmt: skip
