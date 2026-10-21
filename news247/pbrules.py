"""What each of the playbook's rules adds: every sit-out rule graded on the alerts it flagged.

The playbook sits alerts out for six reasons, each from its own corner of the record:

- **the circuit breaker** (the whole playbook in a record drawdown);
- **kinds that don't pay** (no way in and out made money on the kind here);
- **kinds drifting** from their promise;
- **blind spots** (a slice of its record that lost in both halves);
- **thin stocks** (lost even 5 minutes late);
- **stocks that fight their news.**

Each rule is a claim: the alerts it flags would lose. This grades the claim. For every graded alert
a rule flagged, what the playbook's own pair would have made with no sit-out rule at all; summed per
rule, with the older and the newer half apart:

- **It earns its keep** when the alerts it flagged lost on average: what it saved is the playbook's.
- **It costs** when the alerts it flagged made money in both halves of its record, on 20+: it keeps
  sitting out winners. A rule that costs is **muted**: new alerts still carry its flag, but the
  playbook takes them. Muted rules go on being graded in the shadow, and come back on their own
  when the alerts they flag stop making money.

Each alert records the rules muted when it went out, so its track record stays what the playbook
actually did. The same both-halves rule as the blind spots: one good stretch doesn't make a verdict.
History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 20


def free(a: dict[str, Any]) -> dict[str, Any]:
    """The alert with no sit-out rule: what the playbook's own pair would have done with it."""
    e = dict(a.get("edge") or {})
    for k in ("breaker", "blind", "pb_muted"):
        e.pop(k, None)
    if (e.get("liq") or {}).get("pass"):
        e.pop("liq")
    if (e.get("persona") or {}).get("trait") == "fights":
        e.pop("persona")
    if e.get("combo_rec"):
        e["combo_rec"] = {k: v for k, v in e["combo_rec"].items() if k not in ("pass", "drift")}
    return {**a, "edge": e}


def _half(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def rule_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{rule: {"rule", "label", "n", "avg", "total", "older", "newer", "verdict"}} for every rule that
    flagged a graded alert, and "muted": [the rules that cost]."""
    from .autopilot import PASS_RULES, pass_rules
    from .pbcurve import taken

    now = now or time.time()
    pools: dict[str, list[tuple[float, float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        flagged = pass_rules(a)
        if not flagged:
            continue
        got = taken(free(a))
        if got is None:
            continue
        for rule, _ in flagged:
            pools.setdefault(rule, []).append((float(a["created"]), got[0]))
    out: dict[str, Any] = {}
    for rule, rows in pools.items():
        rows.sort()
        xs = [x for _, x in rows]
        mid = len(xs) // 2
        older, newer = _half(xs[:mid]), _half(xs[mid:])
        avg = round(sum(xs) / len(xs), 2)
        if len(xs) >= MIN_N and older is not None and newer is not None and older > 0 and newer > 0:
            verdict = "costs"
        elif avg < 0:
            verdict = "earns"
        else:
            verdict = "unclear"
        out[rule] = {"rule": rule, "label": PASS_RULES[rule], "n": len(xs), "avg": avg, "total": round(sum(xs), 2),
                     "older": older, "newer": newer, "verdict": verdict}  # fmt: skip
    out["muted"] = sorted(r for r, v in out.items() if v["verdict"] == "costs")
    return out


def line(r: dict[str, Any]) -> str:
    """One rule's verdict in words."""
    head = f"{r['label']}: the {r['n']} alerts it flagged would have made {r['avg']:+.2f}% on average ({r['total']:+.1f}% in all)"
    if r["verdict"] == "earns":
        return head + f": it saved {-r['total']:.1f} points."
    if r["verdict"] == "costs":
        return (
            head
            + f", in both halves ({r['older']:+.2f}%, then {r['newer']:+.2f}%): it keeps sitting out winners, so it's muted."
        )
    if r["n"] < MIN_N:
        return head + f": too few to judge ({MIN_N} needed)."
    return head + ": not a cost in both halves, so it stays."


def board(rates: dict[str, Any]) -> dict[str, Any]:
    from .autopilot import PASS_RULES

    rows = [{**rates[k], "line": line(rates[k])} for k in PASS_RULES if k in rates]
    return {"rows": rows, "muted": rates.get("muted") or [], "need": MIN_N}
