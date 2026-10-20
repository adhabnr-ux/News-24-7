"""Heat: how far alerts dipped before they worked, and how far losers ran before they failed.

Every minute until its session's close, each alert keeps its best run and its worst dip, its way,
from the alert price (dossier.record_outcomes: edge.exc). Once graded, the winners' dips say where a
stop belongs: if 80% of contract-win winners never dipped more than 0.7% before working, a stop a
little past that keeps four winners in five while cutting the rest early, instead of a stop placed
by feel. The losers' runs say how much a failed call teases first.

Recorded going forward, so a kind of news needs a dozen graded winners before its heat is used.
History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_WINNERS = 12
KEEP = 0.8  # the share of winners a stop from the record should keep
BUFFER = 0.15  # % past that dip, for the noise around it


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def heat_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"contract": {"winners", "dip80", "dip_median", "losers", "run_median"}, "*": {...}}: the
    winners' worst dips (as positive %, how far against) and the losers' best runs first."""
    from .catalysts import classify

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(6000, since=now - days * 86400):
        e = a.get("edge") or {}
        exc, o = e.get("exc") or {}, e.get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not exc or not row or not row.get("sign") or row.get("close") is None:
            continue
        won = row["sign"] * row["close"] > 0
        c = e.get("catalyst") or classify(a, engine.universe)
        for key in ("*", c["key"]) if c else ("*",):
            g = pools.setdefault(key, {"dips": [], "runs": []})
            if won:
                g["dips"].append(max(0.0, -exc.get("mae", 0.0)))
            else:
                g["runs"].append(max(0.0, exc.get("mfe", 0.0)))
    out = {}
    for key, g in pools.items():
        if len(g["dips"]) < MIN_WINNERS:
            continue
        out[key] = {"winners": len(g["dips"]), "dip80": round(_pct(g["dips"], KEEP), 2), "dip_median": round(_pct(g["dips"], 0.5), 2),
                    "losers": len(g["runs"]), "run_median": round(_pct(g["runs"], 0.5), 2) if g["runs"] else None}  # fmt: skip
    return out


def heat_for(rates: dict[str, Any], a: dict[str, Any], universe: Any = None) -> dict[str, Any] | None:
    """The heat to expect for this alert: its kind of news' when it has enough winners, else every alert's."""
    from .catalysts import LABELS, classify

    c = (a.get("edge") or {}).get("catalyst") or classify(a, universe)
    for key in (c["key"], "*") if c else ("*",):
        r = rates.get(key)
        if r:
            return {**r, "from": f"{LABELS.get(key, key).lower()} news" if key != "*" else "every alert"}
    return None
