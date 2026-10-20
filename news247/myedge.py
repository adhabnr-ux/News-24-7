"""Your edge by kind of news: where your calls beat the alerts they came from, and where they trail.

Every paper call opened from an alert has a twin: the alert itself, graded from its own price to
its session's close. The gap between them is what your choices added: when you got in, and when and
how you got out. Pooled per kind of news (contract wins, FDA approvals, downgrades...), it says
where to trust your own read and where to just follow the alert: "On contract win news you beat the
alerts by +1.1% a call (9); on earnings you trail them by -0.8% (6)".

The gap splits in two:

- **Entry:** your entry price against the alert's, the call's way (+ = you got a better price than
  the alert's, by waiting for a dip; − = you paid up chasing it).
- **The rest:** everything after the entry: how long you held and where you got out.

Paper only; history, not a forecast. A kind needs 4 calls before it gets a verdict.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 4
MARGIN = 0.5  # % a call before a kind is said to be yours or the alerts'


def my_edge(engine: Engine, device: str, days: float = 180, now: float | None = None) -> dict[str, Any]:
    from .catalysts import LABELS, classify

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    pools: dict[str, list[dict[str, float]]] = {}
    total: list[dict[str, float]] = []
    for c in p.get("closed") or []:
        if not c.get("alert_id") or c.get("pnl_pct") is None or not c.get("entry"):
            continue
        if (c.get("closed") or 0) < now - days * 86400:
            continue
        a = engine.storage.get_alert(c["alert_id"])
        e = (a or {}).get("edge") or {}
        sym = c["symbol"]
        row = (e.get("outcome") or {}).get(sym) or {}
        ref = (e.get("refs") or {}).get(sym)
        if not a or not ref or row.get("close") is None:
            continue
        sign = 1 if c.get("direction", "long") == "long" else -1
        alert_ret = sign * row["close"]  # the alert, its price to its close, the call's way
        entry = sign * (ref / c["entry"] - 1) * 100  # + = a better price than the alert's
        x = {"you": c["pnl_pct"], "alert": alert_ret, "gap": c["pnl_pct"] - alert_ret, "entry": entry,
             "rest": c["pnl_pct"] - alert_ret - entry}  # fmt: skip
        cat = e.get("catalyst") or classify(a, engine.universe)
        pools.setdefault(cat["key"] if cat else "other", []).append(x)
        total.append(x)

    def summ(xs: list[dict[str, float]]) -> dict[str, Any]:
        n = len(xs)
        avg = {k: round(sum(x[k] for x in xs) / n, 2) for k in ("you", "alert", "gap", "entry", "rest")}
        return {"n": n, **avg, "beat": round(sum(x["gap"] > 0 for x in xs) / n, 2)}

    kinds = []
    for key, xs in pools.items():
        s = {
            "key": key,
            "label": LABELS.get(key, "Other news") if key != "other" else "Other news",
            **summ(xs),
        }
        s["verdict"] = (
            None
            if s["n"] < MIN_N
            else "yours"
            if s["gap"] >= MARGIN
            else "alerts"
            if s["gap"] <= -MARGIN
            else "even"
        )
        kinds.append(s)
    kinds.sort(key=lambda s: -s["gap"])
    out: dict[str, Any] = {
        "days": days,
        "all": summ(total) if total else None,
        "kinds": kinds,
        "need": MIN_N,
        "lines": [],
    }
    for s in kinds:
        if s["verdict"] == "yours":
            out["lines"].append(
                f"On {s['label'].lower()} news you beat the alerts by {s['gap']:+.1f}% a call ({s['n']})"
            )
        elif s["verdict"] == "alerts":
            out["lines"].append(
                f"On {s['label'].lower()} news you trail the alerts by {s['gap']:+.1f}% a call ({s['n']}): just following them did better"
            )
    a = out["all"]
    if a and a["n"] >= MIN_N and abs(a["entry"]) >= 0.3:
        out["lines"].append(
            f"Your entries: {'a better' if a['entry'] > 0 else 'a worse'} price than the alert's by {abs(a['entry']):.1f}% a call on average "
            f"({'waiting paid' if a['entry'] > 0 else 'chasing cost'}; {a['n']})"
        )
    return out
