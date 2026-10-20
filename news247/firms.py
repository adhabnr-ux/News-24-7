"""Which analysts move stocks: each firm's upgrades, downgrades and price-target changes, graded.

When an alert names a firm and an analyst action ("Goldman Sachs upgrades Acme to buy", "Acme price
target raised to $150 at Morgan Stanley"), its grade (did the stock go the call's way by the close,
and the next day) counts toward that firm's record for that action, here, on this server's own
alerts. A new alert from the firm then carries the record ("Goldman Sachs upgrades here: 71% went
their way by the close, 14 graded"), so does a big target push, and the Watch tab ranks the firms
whose calls followed through and the ones the market faded.

Only alerts Foretape sent are graded (each from its own reference price), so a firm needs a few
before it is judged. History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

ACTIONS = {
    "upgrade": "upgrades",
    "downgrade": "downgrades",
    "raise": "target raises",
    "cut": "target cuts",
    "initiate": "initiations",
}
MIN_ACTION = 4  # graded calls of one kind before a firm's record for it is used
MIN_FIRM = 6  # graded calls of any kind before its overall record stands in


def read_call(alert: dict[str, Any]) -> dict[str, str] | None:
    """{firm, action, label} when an alert is an analyst's call by a named firm, else None."""
    from .catalysts import classify
    from .targets import _CANON, _FIRM_RE, parse_target

    title = str(alert.get("title") or "")
    m = _FIRM_RE.search(title)
    if not m:
        return None
    firm = _CANON.get(m.group(1).lower(), m.group(1))
    c = (alert.get("edge") or {}).get("catalyst") or classify(alert)
    action = c["key"] if c and c["key"] in ("upgrade", "downgrade") else None
    if action is None:
        pt = parse_target(title)
        if pt and pt["firm"] and pt["action"] in ("raise", "cut", "initiate"):
            action = pt["action"]
    if action is None:
        return None
    return {"firm": firm, "action": action, "label": f"{firm} {ACTIONS[action]}"}


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2),
            "avg": round(sum(xs) / len(xs), 2)}  # fmt: skip


def firm_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"Goldman Sachs|upgrade": {"close": stats, "d1": stats}, "Goldman Sachs|*": ...} from graded alerts."""
    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(6000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign"):
            continue
        call = (a.get("edge") or {}).get("firm") or read_call(a)
        if not call:
            continue
        for key in (f"{call['firm']}|{call['action']}", f"{call['firm']}|*"):
            pool = pools.setdefault(key, {"close": [], "d1": []})
            for h in ("close", "d1"):
                if row.get(h) is not None:
                    pool[h].append(row["sign"] * row[h])
    return {k: {"close": _stats(v["close"]), "d1": _stats(v["d1"])} for k, v in pools.items() if v["close"]}


def firm_base(rates: dict[str, Any], call: dict[str, str] | None) -> dict[str, Any] | None:
    """The firm's record for this kind of call (4+ graded), else its record on every call (6+)."""
    if not call:
        return None
    r = rates.get(f"{call['firm']}|{call['action']}")
    if r and r["close"]["n"] >= MIN_ACTION:
        return {**r, "firm": call["firm"], "action": call["action"], "label": call["label"]}
    r = rates.get(f"{call['firm']}|*")
    if r and r["close"]["n"] >= MIN_FIRM:
        return {**r, "firm": call["firm"], "action": "*", "label": f"Every {call['firm']} call"}
    return None


def firm_line(fb: dict[str, Any] | None) -> str:
    """For a push: "Goldman Sachs upgrades here: 71% went their way by the close (median +1.2%, 14)"."""
    if not fb:
        return ""
    c = fb["close"]
    return f"{fb['label']} here: {round(c['hit'] * 100)}% went their way by the close (median {c['median']:+.1f}%, {c['n']})"


def scorecard(rates: dict[str, Any], min_n: int = 5) -> dict[str, Any]:
    """Every firm with ``min_n``+ graded calls: its record overall and per kind of call, ranked by
    how its calls followed through (average move its way, shrunk toward zero when there are few)."""
    firms: dict[str, dict[str, Any]] = {}
    for key, r in rates.items():
        firm, action = key.split("|", 1)
        f = firms.setdefault(firm, {"firm": firm, "all": None, "actions": []})
        if action == "*":
            f["all"] = r
        else:
            f["actions"].append({"action": action, "label": ACTIONS[action], **r})
    rows = [f for f in firms.values() if f["all"] and f["all"]["close"]["n"] >= min_n]
    for f in rows:
        c = f["all"]["close"]
        f["score"] = round(c["avg"] * c["n"] / (c["n"] + 10), 3)
        f["actions"].sort(key=lambda x: -x["close"]["n"])
    rows.sort(key=lambda f: -f["score"])
    return {
        "best": [f for f in rows if f["score"] > 0][:10],
        "worst": [f for f in reversed(rows) if f["score"] < 0][:10],
        "firms": len(rows),
        "thin": sum(1 for f in firms.values() if f["all"] and f["all"]["close"]["n"] < min_n),
        "min_n": min_n,
    }
