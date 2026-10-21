"""The tide rule: kinds of news whose playbook result is all market get sat out.

The market's part of the playbook (pbmarket.py) splits every call's result into what SPY gave over
the same window and what's beyond it. Per kind of news, that can show a kind that makes money only
because the market rose under it: on 20+ calls with a market reading it made money, yet beyond the
market it made nothing, or lost, in both the older and the newer half of its record. That kind's
edge is the market's. Holding SPY would have done as well, with no news risk.

Such a kind is a **tide kind**. New alerts of it go out with the reason, and the playbook sits them
out. It's the seventh sit-out rule, so it's graded like the others (pbrules.py): if the alerts it
sits out keep making money in both halves, the rule is muted. Judged on what the playbook's own
pair would have made with no sit-out rule, so sitting a kind out never makes it look cured.
History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 20


def tide_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "n", "avg", "beyond", "older", "newer", "tide"}} for kinds with 20+ calls
    with a market reading."""
    from .combos import label_of
    from .pbmarket import split
    from .pbrules import free

    now = now or time.time()
    pools: dict[str, list[tuple[float, float, float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        kind = ((a.get("edge") or {}).get("combo_rec") or {}).get("kind")
        if not kind:
            continue
        sp = split(free(a))
        if not sp or sp["pb_mkt"] is None or sp["pb"] == 0.0:
            continue  # no reading, or no call
        pools.setdefault(kind, []).append((float(a["created"]), sp["pb"], sp["pb"] - sp["pb_mkt"]))
    out = {}
    for kind, rows in pools.items():
        if len(rows) < MIN_N:
            continue
        rows.sort()
        mid = len(rows) // 2
        beyond = [b for _, _, b in rows]
        older, newer = sum(beyond[:mid]) / mid, sum(beyond[mid:]) / (len(beyond) - mid)
        avg = sum(x for _, x, _ in rows) / len(rows)
        out[kind] = {"kind": kind, "label": label_of(kind), "n": len(rows), "avg": round(avg, 2),
                     "beyond": round(sum(beyond) / len(beyond), 2), "older": round(older, 2), "newer": round(newer, 2),
                     "tide": avg > 0 and older <= 0 and newer <= 0}  # fmt: skip
    return out


def stamp(engine: Engine, kind: str | None) -> dict[str, Any] | None:
    """The tide stamp for a new alert of a tide kind (autopilot.pass_rules reads it)."""
    r = (getattr(engine, "tide_rates", None) or {}).get(kind or "")
    if not r or not r["tide"]:
        return None
    return {"why": (f"its kind rides the market here: {r['avg']:+.2f}% a call, but {r['beyond']:+.2f}% beyond SPY "
                    f"over the same windows ({r['n']}), in both halves of its record")}  # fmt: skip
