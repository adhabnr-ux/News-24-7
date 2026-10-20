"""Skill or the market? Each paper call's return split into what the market gave and the rest.

A long call that made 2% on a day SPY rose 1.5% mostly rode the market. Each call notes SPY when it
opens and when it closes (desk.py: mkt_entry, mkt_exit), so its return splits into the market's
part (SPY's move, in the call's direction) and what the stock did beyond it. Summed over the
phone's closed calls, the split answers whether its record is an edge or a tide. Every graded alert
gets the same split from its own SPY reading (dossier.excess). Recorded going forward: calls opened
before this note no market reading and are counted apart. History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine


def call_split(c: dict[str, Any]) -> dict[str, Any] | None:
    """{"pnl", "market", "beyond"} for a closed call with both market readings, else None."""
    if c.get("pnl_pct") is None or not c.get("mkt_entry") or not c.get("mkt_exit"):
        return None
    sign = 1 if c.get("direction", "long") == "long" else -1
    market = round(sign * (c["mkt_exit"] / c["mkt_entry"] - 1) * 100, 2)
    return {"pnl": c["pnl_pct"], "market": market, "beyond": round(c["pnl_pct"] - market, 2)}


def _sum(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    beyond = [r["beyond"] for r in rows]
    return {"n": len(rows), "total": round(sum(r["pnl"] for r in rows), 2), "market": round(sum(r["market"] for r in rows), 2),
            "beyond": round(sum(beyond), 2), "avg_beyond": round(sum(beyond) / len(beyond), 2),
            "hit_beyond": round(sum(b > 0 for b in beyond) / len(beyond), 2)}  # fmt: skip


def attribution(engine: Engine, device: str, days: float = 90, now: float | None = None) -> dict[str, Any]:
    from .dossier import excess

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    closed = [
        c
        for c in p.get("closed") or []
        if (c.get("closed") or 0) >= now - days * 86400 and c.get("pnl_pct") is not None
    ]
    mine, autos, unread = [], [], 0
    recent = []
    for c in closed:
        sp = call_split(c)
        if sp is None:
            unread += 1
            continue
        (autos if c.get("auto") else mine).append(sp)
        if not c.get("auto") and len(recent) < 10:
            recent.append(
                {"symbol": c["symbol"], "direction": c.get("direction"), "closed": c.get("closed"), **sp}
            )
    alerts = []
    for a in engine.storage.recent_alerts(5000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        x = excess(a)
        if row and row.get("sign") and row.get("close") is not None and x is not None:
            raw = row["sign"] * row["close"]
            alerts.append({"pnl": raw, "market": round(raw - x, 2), "beyond": x})
    return {"days": days, "yours": _sum(mine), "autopilots": _sum(autos), "every_alert": _sum(alerts),
            "no_reading": unread, "recent": recent}  # fmt: skip
