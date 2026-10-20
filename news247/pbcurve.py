"""The playbook's track record, as a curve: every alert it was stamped on, since the first.

Each alert goes out with what the playbook would do with it: its pair (a way in and a way out), or a
pass. Once graded, the alert says what that did, on an alert the pair was never fitted on, and
what the plain way (in at the alert, out at the close) did on the same alert. Added up in order,
one equal-sized paper call per alert, they make two lines:

- **The playbook:** what it took, the way it would have taken it (a pass, or an entry that never
  came, adds 0%);
- **The plain way:** every one of the same alerts, in at the alert, out at the close.

With the totals, the average per alert, how often each went its way, and the playbook's worst
drawdown. This is the case for the whole learning system, or against it, in one chart, and it runs
whether or not anyone follows the playbook.

**In dollars**, the same alerts become an account: your account size (Settings), or $10,000, with a
tenth of the balance in each paper call, compounded, for the playbook and the plain way, month by
month, with the best and worst month and the deepest drawdown in dollars. Paper only. History, not a
forecast.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
FRACTION = 0.10  # the share of the balance in each paper call, in the dollars view


def taken(a: dict[str, Any], counterfactual: bool = False) -> tuple[float, float] | None:
    """(what the playbook made on this graded alert, what the plain way made), or None if not graded.
    ``counterfactual``: as if it hadn't sat out a blind spot (blindspots.py judges slices that way, so
    sitting one out doesn't make it look recovered)."""
    from .autopilot import playbook_in, playbook_out, playbook_pass
    from .combos import DEFAULT, combos

    if counterfactual and "blind" in (a.get("edge") or {}):
        a = {**a, "edge": {k: v for k, v in a["edge"].items() if k != "blind"}}

    if not ((a.get("edge") or {}).get("combo_rec") or {}).get("best"):
        return None
    got = combos(a)
    if DEFAULT not in got:
        return None
    if playbook_pass(a):
        return 0.0, got[DEFAULT]
    pin, pout = playbook_in(a), playbook_out(a)
    return (got.get(f"{pin}|{pout}", 0.0) if pin and pout else got[DEFAULT]), got[DEFAULT]


def _stats(xs: list[float]) -> dict[str, Any]:
    return {
        "total": round(sum(xs), 2),
        "avg": round(sum(xs) / len(xs), 2),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
    }


def dollars(rows: list[tuple[float, float, float]], capital: float, frac: float = FRACTION) -> dict[str, Any]:
    """The same alerts as an account: ``frac`` of the balance in each paper call, compounded, for the
    playbook and the plain way. {"start", "playbook", "plain", "months", "best", "worst", "drawdown"}."""
    eq = {"playbook": capital, "plain": capital}
    peak, dd = capital, 0.0
    months: dict[str, dict[str, float]] = {}
    for t, pb, plain in rows:
        m = datetime.fromtimestamp(t, ET).strftime("%Y-%m")
        mo = months.setdefault(m, {"playbook": 0.0, "plain": 0.0})
        for k, x in (("playbook", pb), ("plain", plain)):
            gain = eq[k] * frac * x / 100
            eq[k] += gain
            mo[k] += gain
        peak = max(peak, eq["playbook"])
        dd = min(dd, eq["playbook"] - peak)
    mrows = [
        {"month": m, "playbook": round(v["playbook"], 2), "plain": round(v["plain"], 2)}
        for m, v in months.items()
    ]
    best = max(mrows, key=lambda r: r["playbook"]) if mrows else None
    worst = min(mrows, key=lambda r: r["playbook"]) if mrows else None
    return {"start": capital, "frac": frac, "playbook": round(eq["playbook"], 2), "plain": round(eq["plain"], 2),
            "months": mrows, "best": best, "worst": worst, "drawdown": round(dd, 2)}  # fmt: skip


def money_line(d: dict[str, Any]) -> str:
    head = f"${d['start']:,.0f} → ${d['playbook']:,.0f} on the playbook (the plain way ${d['plain']:,.0f})"
    if d.get("worst") and len(d["months"]) > 1:
        head += f"; best month {_usd(d['best']['playbook'])}, worst {_usd(d['worst']['playbook'])}"
    return head + f"; its deepest drawdown {_usd(d['drawdown'])}."


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def curve(
    engine: Engine, days: float = 365, now: float | None = None, capital: float | None = None
) -> dict[str, Any]:
    now = now or time.time()
    rows = []
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        got = taken(a)
        if got:
            rows.append((float(a["created"]), *got))
    if not rows:
        return {"n": 0}
    rows.sort()
    pb_cum = plain_cum = peak = dd = 0.0
    points: dict[str, list[Any]] = {}
    for t, pb, plain in rows:
        pb_cum += pb
        plain_cum += plain
        peak = max(peak, pb_cum)
        dd = min(dd, pb_cum - peak)
        points[datetime.fromtimestamp(t, ET).date().isoformat()] = [round(pb_cum, 2), round(plain_cum, 2)]
    pbs, plains = [r[1] for r in rows], [r[2] for r in rows]
    money = dollars(rows, capital) if capital else None
    return {"money": money, "money_line": money_line(money) if money else "","n": len(rows), "since": datetime.fromtimestamp(rows[0][0], ET).date().isoformat(),
            "playbook": _stats(pbs), "plain": _stats(plains), "drawdown": round(dd, 2),
            "taken": sum(1 for x in pbs if x != 0.0), "points": [[d, *v] for d, v in points.items()]}  # fmt: skip


def line(c: dict[str, Any]) -> str:
    if not c.get("n"):
        return ""
    p, q = c["playbook"], c["plain"]
    return (f"Since {c['since']}, on {c['n']} alerts: the playbook {p['total']:+.1f}% in all ({p['avg']:+.2f}% an alert), "
            f"the plain way {q['total']:+.1f}% ({q['avg']:+.2f}%); its worst drawdown {c['drawdown']:.1f}%.")  # fmt: skip
