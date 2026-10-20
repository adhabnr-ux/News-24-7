"""The promise monitor: is each kind's playbook keeping its word on the alerts that came after it?

Every alert goes out stamped with its kind's pair and what that pair had made on average (combos.py).
Once the alert is graded, the desk knows what the pair actually made on it, an alert the pair was
never fitted on. Pooled per kind, the newest 15 of those are the playbook's live record:

- **Keeping it:** the live average is at or above the bottom of the promised range (or, without a
  range, within a point of the promise).
- **Drifting:** below it, and losing. The market may have changed under the record. The playbook
  sits that kind out (the autopilot passes, the board says why) until its live alerts recover, and
  the Watch tab shows promised against live per kind.

An alert whose pair's way in never came counts as 0% (the playbook stood aside). The monitor runs on
every stamped alert, whether or not anyone follows the playbook. History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

LIVE_N = 15
SLACK = 1.0  # %: without a range, how far under its promise a kind's live average may run


def live_record(engine: Engine, days: float = 120, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "n", "live", "promised", "lo", "verdict", "last"}} from graded, stamped alerts."""
    from .combos import combos

    now = now or time.time()
    pools: dict[str, list[dict[str, Any]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        cr = (a.get("edge") or {}).get("combo_rec") or {}
        if not cr.get("best") or cr.get("pass") or not cr.get("kind"):
            continue
        got = combos(a)
        if "alert|close" not in got:
            continue  # not graded yet
        st = (cr.get("styles") or {}).get(cr["best"]) or {}
        if st.get("avg") is None:
            continue
        pools.setdefault(cr["kind"], []).append({"t": float(a["created"]), "x": got.get(cr["best"], 0.0), "promise": st["avg"],
                                                 "lo": st.get("lo"), "label": cr.get("label") or cr["kind"]})  # fmt: skip
    out = {}
    for k, xs in pools.items():
        xs.sort(key=lambda r: r["t"])
        last = xs[-LIVE_N:]
        live = round(sum(r["x"] for r in last) / len(last), 2)
        promised = round(sum(r["promise"] for r in last) / len(last), 2)
        los = [r["lo"] for r in last if r["lo"] is not None]
        lo = round(sum(los) / len(los), 2) if los else None
        floor = lo if lo is not None else promised - SLACK
        verdict = None
        if len(last) >= LIVE_N:
            verdict = "drifting" if live < floor and live < 0 else "keeping"
        out[k] = {
            "kind": k,
            "label": last[-1]["label"],
            "n": len(last),
            "live": live,
            "promised": promised,
            "lo": lo,
            "verdict": verdict,
        }
    return out


def line(r: dict[str, Any]) -> str:
    head = f"Its last {r['n']} live alerts made {r['live']:+.1f}% on its pair against {r['promised']:+.1f}% promised"
    if r.get("lo") is not None:
        head += f" (the range's bottom {r['lo']:+.1f}%)"
    if r.get("verdict") == "drifting":
        return head + ": drifting, so the playbook sits it out until it recovers."
    if r.get("verdict") == "keeping":
        return head + ": keeping its word."
    return head + f" ({LIVE_N} needed for a verdict)."


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = sorted(rates.values(), key=lambda r: (r.get("verdict") != "drifting", -r["n"]))
    return {"rows": [{**r, "line": line(r)} for r in rows[:16]], "need": LIVE_N}
