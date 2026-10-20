"""The stall rule: a time stop for news that hasn't moved 30 minutes in.

A call that's gone nowhere half an hour after its news ties up money and attention, and the market
may already have said what it thinks. The desk records where every alert stood 30 minutes in (the
news clock's m30 checkpoint) and at the close, so each one is sorted by how its first half hour went,
its way:

- **Working:** 0.3% or more its way;
- **Stalled:** within 0.3% either side;
- **Against:** 0.3% or more against it.

The record then says, per kind of news, what each did for the rest of the day (from the
30-minute price to the close, its way). When stalled ones lost the rest of the day on average (8+
graded), the stall rule is a time stop:

- the playbook autopilot closes a call it entered at the alert that's still stalled 30 minutes in;
- the playbook today board flags it, with the record;
- the kind's playbook lists how working, stalled and against did.

History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

BAND = 0.3  # %: within this either side of the alert price, 30 minutes in, it stalled
MIN_N = 8
STATES = {"working": "working", "stalled": "stalled", "against": "going against it"}


def state_of(its: float | None) -> str | None:
    if its is None:
        return None
    return "working" if its >= BAND else "against" if its <= -BAND else "stalled"


def stall_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{kind: {state: {"n", "hit", "avg"}}}: the rest of the day, from 30 minutes in to the close, its way."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        e = a.get("edge") or {}
        o = e.get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("m30") is None or row.get("close") is None:
            continue
        sign = row["sign"]
        st = state_of(sign * row["m30"])
        rest = sign * ((1 + row["close"] / 100) / (1 + row["m30"] / 100) - 1) * 100
        c = e.get("catalyst") or classify(a, engine.universe)
        for k in ("*", c["key"]) if c else ("*",):
            pools.setdefault(k, {}).setdefault(st, []).append(rest)  # type: ignore[arg-type]
    out: dict[str, Any] = {}
    for k, g in pools.items():
        rows = {s: {"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "avg": round(sum(xs) / len(xs), 2)}
                for s, xs in g.items()}  # fmt: skip
        if any(r["n"] >= MIN_N for r in rows.values()):
            out[k] = {"kind": k, "label": "Any news" if k == "*" else LABELS.get(k, k), **rows}
    return out


def verdict(rates: dict[str, Any], kind: str | None, st: str | None) -> dict[str, Any] | None:
    """{"exit": bool, "state", "rec", "line"} for an alert in this state 30 minutes in."""
    if not st:
        return None
    r = (rates.get(kind or "") or {}).get(st)
    if not r or r["n"] < MIN_N:
        r = (rates.get("*") or {}).get(st)
    if not r or r["n"] < MIN_N:
        return None
    ex = st == "stalled" and r["avg"] < 0
    line = (f"{STATES[st].capitalize()} 30 minutes in; alerts like it went {r['avg']:+.1f}% the rest of the day here "
            f"({round(r['hit'] * 100)}% its way, {r['n']})")  # fmt: skip
    return {"exit": ex, "state": st, "rec": r, "line": line + (": the time stop says out." if ex else ".")}


def live(engine: Engine, a: dict[str, Any], live_move: float | None) -> dict[str, Any] | None:
    """The stall verdict for an alert 30-60 minutes old (``live_move``: raw % from the alert price)."""
    from .dossier import _direction

    d = _direction(a)
    if live_move is None or d not in ("up", "down"):
        return None
    e = a.get("edge") or {}
    kind = ((e.get("combo_rec") or {}).get("kind")) or ((e.get("catalyst") or {}).get("key"))
    return verdict(
        getattr(engine, "stall_rates", None) or {}, kind, state_of((1 if d == "up" else -1) * live_move)
    )
