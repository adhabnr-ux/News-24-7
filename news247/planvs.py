"""Your exits against the plan's: would following each alert's plan have done better than you did?

Every alert carries a plan when it goes out (plans.py: a stop where the news is proven wrong, a
target at its kind's typical move), and every alert records its best run and worst dip before its
session's close (dossier.record_outcomes: edge.exc, its way from the alert price) and its close.
For a paper call opened from an alert, at about the alert's price (within 0.5%, within 15 minutes),
that's enough to replay the plan's exits exactly:

- the dip reached the plan's stop: **stopped** at the stop;
- otherwise the run reached the target: **target** at the target;
- otherwise: out **at the close**.

When both the stop and the target were reached in the session, which came first isn't recorded, so
the call counts as **stopped** (the conservative reading) and is flagged. Calls held past their
session can't be replayed (only the closes are known after that) and are left out. The result: per
call, your return beside the plan's, and over every replayed call how often and by how much the
plan would have done better. Paper only; history, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MAX_GAP_PCT = 0.5  # the call's entry within this of the alert price
MAX_LAG_S = 900  # opened within 15 minutes of the alert
MIN_N = 5


def replay(c: dict[str, Any], a: dict[str, Any], holidays: set[str] | None = None) -> dict[str, Any] | None:
    """The plan's exits on one closed call: {"plan", "yours", "how", "ambiguous"} in %, its way; None
    when the call can't be replayed exactly."""
    from .dossier import session_closes

    e = a.get("edge") or {}
    plan, exc = e.get("plan") or {}, e.get("exc") or {}
    sym = c.get("symbol")
    ref = (e.get("refs") or {}).get(sym)
    row = (e.get("outcome") or {}).get(sym) or {}
    if not plan or not exc or not ref or plan.get("symbol") != sym or row.get("close") is None:
        return None
    sign = 1 if c.get("direction", "long") == "long" else -1
    if sign != (1 if plan.get("dir") == "up" else -1) or c.get("pnl_pct") is None or not c.get("entry"):
        return None
    if abs(c["entry"] / ref - 1) * 100 > MAX_GAP_PCT or not (
        0 <= c.get("ts", 0) - float(a["created"]) <= MAX_LAG_S
    ):
        return None
    close_ts = session_closes(float(a["created"]), 1, holidays)[0]
    if c.get("closed", 0) > close_ts + 1800:
        return None  # held past its session: the path after the close isn't recorded
    stop_pct = sign * (plan["stop"] / ref - 1) * 100  # negative: how far against, its way
    target_pct = sign * (plan["target"] / ref - 1) * 100
    hit_stop = exc.get("mae", 0.0) <= stop_pct
    hit_target = exc.get("mfe", 0.0) >= target_pct
    if hit_stop:
        how, out = "stopped", stop_pct
    elif hit_target:
        how, out = "target", target_pct
    else:
        how, out = "close", sign * row["close"]  # the raw close move, the call's way
    # from the call's own entry, not the alert's price
    plan_ret = ((1 + sign * out / 100) * ref / c["entry"] - 1) * 100 * sign
    return {"plan": round(plan_ret, 2), "yours": round(c["pnl_pct"], 2), "how": how,
            "ambiguous": hit_stop and hit_target}  # fmt: skip


def plan_vs(engine: Engine, device: str, days: float = 120, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    holidays = getattr(engine.calendar, "holidays", set())
    rows, skipped = [], 0
    for c in p.get("closed") or []:
        if not c.get("alert_id") or (c.get("closed") or 0) < now - days * 86400:
            continue
        a = engine.storage.get_alert(c["alert_id"])
        r = replay(c, a, holidays) if a else None
        if r is None:
            skipped += 1
            continue
        rows.append({"id": c["id"], "symbol": c["symbol"], "direction": c.get("direction", "long"),
                     "closed": c.get("closed"), **r, "diff": round(r["plan"] - r["yours"], 2)})  # fmt: skip
    out: dict[str, Any] = {"days": days, "n": len(rows), "skipped": skipped, "need": MIN_N, "rows": rows[:20]}
    if rows:
        out.update(
            yours=round(sum(r["yours"] for r in rows) / len(rows), 2),
            plan=round(sum(r["plan"] for r in rows) / len(rows), 2),
            plan_better=round(sum(r["diff"] > 0.05 for r in rows) / len(rows), 2),
            ambiguous=sum(r["ambiguous"] for r in rows),
            how={k: sum(r["how"] == k for r in rows) for k in ("stopped", "target", "close")},
        )
        if len(rows) >= MIN_N:
            gap = out["plan"] - out["yours"]
            out["verdict"] = "plan" if gap >= 0.25 else "yours" if gap <= -0.25 else "even"
            out["line"] = {
                "plan": f"The plan's exits would have done better: {out['plan']:+.2f}% a call vs your {out['yours']:+.2f}% ({len(rows)})",
                "yours": f"Your exits beat the plan's: {out['yours']:+.2f}% a call vs {out['plan']:+.2f}% ({len(rows)})",
                "even": f"Your exits and the plan's come out about even: {out['yours']:+.2f}% vs {out['plan']:+.2f}% a call ({len(rows)})",
            }[out["verdict"]]
    return out
