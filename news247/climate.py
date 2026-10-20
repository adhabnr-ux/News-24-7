"""News climate: which kinds of news are working lately, against their own long-run record.

What works rotates. Contract wins can run for months and then stop; downgrades can bite for a few
weeks and then get shrugged off. Every record here is pooled over months, so it's slow to notice.
The climate compares each kind of news' last two weeks (by the close, its way) with its record from
before them, and flags the gap only when it's too big for chance:

- **Hot:** went its way more often lately, by 15 points or more, at a z-score of 1.64 or more (a
  one-sided test at 5% on the difference of two proportions).
- **Cold:** less often lately, by the same bar.
- **Normal:** anything else, including too few lately (8 needed) or before (20 needed).

A hot or cold kind is named on its new alerts (the push, the sheet, the pre-trade check) and counts
in the call (thecall.py): a cold kind is a fact against, a hot one a fact for. The Watch tab lists
every kind's climate. History, not a forecast: a hot streak can end on the next alert.
"""

from __future__ import annotations

import math
import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

RECENT_DAYS = 14
MIN_RECENT, MIN_BEFORE = 8, 20
GAP, Z = 0.15, 1.64


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def z_score(h1: float, n1: int, h2: float, n2: int) -> float:
    """Two-proportion z for hit rate h1 of n1 against h2 of n2 (pooled standard error)."""
    if not n1 or not n2:
        return 0.0
    p = (h1 * n1 + h2 * n2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (h1 - h2) / se if se else 0.0


def climate_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"key", "label", "recent", "before", "z", "state"}} for "*" and each kind of news."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    cut = now - RECENT_DAYS * 86400
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if not row.get("sign") or row.get("close") is None:
            continue
        x = row["sign"] * row["close"]
        side = "recent" if float(a.get("created") or 0) >= cut else "before"
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for key in ("*", c["key"]) if c else ("*",):
            pools.setdefault(key, {"recent": [], "before": []})[side].append(x)
    out = {}
    for key, g in pools.items():
        rec, bef = _stats(g["recent"]), _stats(g["before"])
        r = {"key": key, "label": "Every alert" if key == "*" else LABELS.get(key, key), "recent": rec, "before": bef,
             "z": None, "state": "normal"}  # fmt: skip
        if rec and bef and rec["n"] >= MIN_RECENT and bef["n"] >= MIN_BEFORE:
            z = z_score(
                sum(x > 0 for x in g["recent"]) / rec["n"],
                rec["n"],
                sum(x > 0 for x in g["before"]) / bef["n"],
                bef["n"],
            )
            r["z"] = round(z, 2)
            gap = rec["hit"] - bef["hit"]
            if gap >= GAP and z >= Z:
                r["state"] = "hot"
            elif gap <= -GAP and z <= -Z:
                r["state"] = "cold"
        out[key] = r
    return out


def climate_for(rates: dict[str, Any], kind: str | None) -> dict[str, Any] | None:
    """Its kind's climate when hot or cold (else None: normal news isn't news)."""
    r = rates.get(kind or "") if kind else None
    return r if r and r["state"] != "normal" else None


def climate_line(r: dict[str, Any] | None) -> str:
    """'🔥 Hot lately: contract win news went its way 85% in the last two weeks (13) vs 52% before (60)'."""
    if not r or r.get("state") not in ("hot", "cold"):
        return ""
    icon, word = ("🔥", "Hot") if r["state"] == "hot" else ("🧊", "Cold")
    who = "news" if r["key"] == "*" else f"{r['label'][:1].lower()}{r['label'][1:]} news"
    return (f"{icon} {word} lately: {who} went its way {round(r['recent']['hit'] * 100)}% in the last two weeks "
            f"({r['recent']['n']}) vs {round(r['before']['hit'] * 100)}% before ({r['before']['n']})")  # fmt: skip


def climate_board(rates: dict[str, Any]) -> dict[str, Any]:
    order = {"hot": 0, "cold": 1, "normal": 2}
    rows = sorted(
        (r for r in rates.values() if r["recent"]),
        key=lambda r: (order[r["state"]], -(r["recent"] or {}).get("n", 0)),
    )
    return {"rows": rows, "days": RECENT_DAYS, "min_recent": MIN_RECENT, "min_before": MIN_BEFORE}
