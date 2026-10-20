"""Holds that gave back their run: when the playbook's overnight hold should end at the close instead.

The playbook holds some kinds of news into the next day or the week, because on average they keep
going. But a stock that ran hard after its news and gave most of it back by the close is a
different animal from one that closed near its best. Every graded alert kept its best run, its way,
until its session's close (heat.py: edge.exc.mfe) and its close. So each day is sorted by how much
of its run it gave back by the bell:

- **Kept it:** closed with two-thirds or more of its best run.
- **Gave back half:** closed with a third to two-thirds of it.
- **Gave back most:** closed with less than a third of it, or against the call.

(A day without a run worth the name, under 0.5%, isn't sorted.) The record then says, per kind of
news, how each kind of day went overnight: the move from that close to the next session's close,
its way. When the playbook's way out is the next day or five sessions on, and the day's give-back
says the night lost on average (8+ graded), the playbook exits at the close instead:

- the playbook today board says so before the bell;
- the playbook autopilot closes such calls at 15:50 ET, with the reason;
- the alert's playbook says how each kind of day went.

History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_RUN = 0.5  # %: a smaller best run has nothing to give back
MIN_N = 8
BUCKETS = {"kept": "kept its run", "half": "gave back half its run", "most": "gave back most of its run"}


def bucket(mfe: float | None, its: float | None) -> str | None:
    """How much of its best run (``mfe``, its way %) a stock that's at ``its`` (its way %) has kept."""
    if mfe is None or its is None or mfe < MIN_RUN:
        return None
    kept = its / mfe
    return "kept" if kept >= 2 / 3 else "half" if kept >= 1 / 3 else "most"


def giveback_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{kind: {bucket: {"n", "hit", "avg"}}}: each kind of day's overnight move, close to next close, its way."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        e = a.get("edge") or {}
        o = e.get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("close") is None or row.get("d1") is None:
            continue
        sign = row["sign"]
        b = bucket((e.get("exc") or {}).get("mfe"), sign * row["close"])
        if not b:
            continue
        night = sign * ((1 + row["d1"] / 100) / (1 + row["close"] / 100) - 1) * 100
        c = e.get("catalyst") or classify(a, engine.universe)
        for k in ("*", c["key"]) if c else ("*",):
            pools.setdefault(k, {}).setdefault(b, []).append(night)
    out: dict[str, Any] = {}
    for k, g in pools.items():
        rows = {b: {"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "avg": round(sum(xs) / len(xs), 2)}
                for b, xs in g.items()}  # fmt: skip
        if any(r["n"] >= MIN_N for r in rows.values()):
            out[k] = {"kind": k, "label": "Any news" if k == "*" else LABELS.get(k, k), **rows}
    return out


def verdict(rates: dict[str, Any], kind: str | None, b: str | None) -> dict[str, Any] | None:
    """{"exit": bool, "bucket", "rec", "line"} for a day like this on this kind of news (else any news)."""
    if not b:
        return None
    r = (rates.get(kind or "") or {}).get(b)
    if not r or r["n"] < MIN_N:
        r = (rates.get("*") or {}).get(b)
    if not r or r["n"] < MIN_N:
        return None
    ex = r["avg"] < 0
    line = (f"It {BUCKETS[b]} today; days like it went {r['avg']:+.1f}% overnight on average here "
            f"({round(r['hit'] * 100)}% its way, {r['n']})")  # fmt: skip
    return {
        "exit": ex,
        "bucket": b,
        "rec": r,
        "line": line + (": out at the close instead." if ex else ": the hold stands."),
    }


def live(engine: Engine, a: dict[str, Any], live_move: float | None) -> dict[str, Any] | None:
    """Today's give-back verdict for an alert still in its session (``live_move``: raw % from the alert price)."""
    from .dossier import _direction

    e = a.get("edge") or {}
    d = _direction(a)
    if live_move is None or d not in ("up", "down"):
        return None
    its = (1 if d == "up" else -1) * live_move
    mfe = (e.get("exc") or {}).get("mfe")
    mfe = max(mfe, its) if mfe is not None else None
    kind = ((e.get("combo_rec") or {}).get("kind")) or ((e.get("catalyst") or {}).get("key"))
    return verdict(getattr(engine, "giveback_rates", None) or {}, kind, bucket(mfe, its))
