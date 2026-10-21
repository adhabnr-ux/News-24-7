"""The record after real fills: the mirror's measured costs, charged to the playbook.

The broker mirror (mirror.py) measures what real Alpaca paper fills cost against the desk's paper
prices, a round trip at a time, by the stock's dollar volume. Once a band has 10+ measured round
trips, its average cost is a fact about trading it, and the record uses it:

- **The track record after fills:** every playbook call in a measured band is charged that band's
  round-trip cost. Calls in bands not measured yet are counted apart, never charged a guess.
- **Edges smaller than their fills:** when a band's measured cost is at least what the playbook's
  calls make there on average, its paper edge is a fill nobody gets. New alerts in that band are
  sat out with the reason. It's the eighth sit-out rule, graded and muted like the others
  (pbrules.py).

Nothing here exists until the mirror has been on: costs are measured, not assumed. Paper only.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 10


def cost_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{band: {"band", "label", "n", "cost", "edge", "edge_n", "costly"}} for bands with 10+ measured trips."""
    from .liquidity import LABELS
    from .mirror import report
    from .pbcurve import taken
    from .pbrules import free

    now = now or time.time()
    measured = {b["band"]: b for b in report(engine)["bands"] if b["band"] in LABELS and b["n"] >= MIN_N}
    if not measured:
        return {}
    edges: dict[str, list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        b = ((a.get("edge") or {}).get("liq") or {}).get("band")
        if b not in measured:
            continue
        got = taken(free(a))
        if got and got[0] != 0.0:
            edges.setdefault(b, []).append(got[0])
    out = {}
    for b, m in measured.items():
        xs = edges.get(b) or []
        edge = round(sum(xs) / len(xs), 2) if xs else None
        out[b] = {"band": b, "label": LABELS[b], "n": m["n"], "cost": m["round"], "edge": edge, "edge_n": len(xs),
                  "costly": edge is not None and len(xs) >= MIN_N and m["round"] >= edge}  # fmt: skip
    return out


def stamp(engine: Engine, band: str | None) -> dict[str, Any] | None:
    r = (getattr(engine, "cost_rates", None) or {}).get(band or "")
    if not r or not r["costly"]:
        return None
    return {"why": (f"fills in {r['label'].split(' (')[0].lower()} stocks cost {r['cost']:+.2f}% a round trip on the mirror "
                    f"({r['n']}), at least what the playbook makes there ({r['edge']:+.2f}%)")}  # fmt: skip


def after(engine: Engine, rows: list[tuple[float, float, str | None]]) -> dict[str, Any]:
    """rows: (created, the playbook's result, the alert's band). The record charged its measured fills."""
    rates = getattr(engine, "cost_rates", None) or {}
    calls = [(pb, b) for _, pb, b in rows if pb != 0.0]
    costed = [(pb, rates[b]["cost"]) for pb, b in calls if b in rates]
    if not costed:
        return {"costed": 0, "line": ""}
    paper = round(sum(pb for pb, _ in costed), 2)
    net = round(sum(pb - c for pb, c in costed), 2)
    out = {"costed": len(costed), "unmeasured": len(calls) - len(costed), "paper": paper, "net": net}
    out["line"] = (
        f"After real fills (measured on the mirror): on the {len(costed)} calls in measured bands, the playbook's "
        f"{paper:+.1f}% becomes {net:+.1f}%"
        + (
            f"; {out['unmeasured']} more in bands not measured yet, not charged."
            if out["unmeasured"]
            else "."
        )
    )
    return out
