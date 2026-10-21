"""Conviction sizing: the playbook's paper calls sized by how sure its record is.

Every pair the playbook trades carries its average and an 80% range for it (combos.py: bootstrapped
from the alerts behind it). Some are sure: the whole range sits above zero and above the plain way.
Some are a coin flip with a good average. Equal-sized calls treat them the same. This sizes them:

- **Full size** when the pair is proven (its range sits above the plain way's average) and the
  range's low end is above zero;
- **Half size** otherwise: an unproven pair, or a range that reaches below zero.

The playbook autopilot opens its calls at that size, the push says which, and the track record
grades it: full-size calls against half-size ones, and the account sized against equal-sized on the
same alerts. Fixed rule, stated as a policy: two sizes, no fitting. Paper only.
"""

from __future__ import annotations

from typing import Any

FULL, HALF = 1.0, 0.5


def size(edge: dict[str, Any]) -> tuple[float, str]:
    """(the share of a full paper call, why) for an alert's pair; (1.0, "") with no pair to read."""
    cr = edge.get("combo_rec") or {}
    if not cr.get("best"):
        return FULL, ""
    s = (cr.get("styles") or {}).get(cr["best"]) or {}
    lo = s.get("lo")
    if lo is None:
        return FULL, ""  # stamped before ranges were kept: unsized, as it was
    if cr.get("proven") and lo > 0:
        return FULL, f"full size: its range clears zero ({lo:+.1f}% to {s['hi']:+.1f}%)"
    if not cr.get("proven"):
        return HALF, "half size: unproven (the plain way's average sits inside its range)"
    return HALF, f"half size: its range reaches below zero ({lo:+.1f}% to {s['hi']:+.1f}%)"


def label(why: str) -> str:
    """ "full", "half", or "" (unsized) from :func:`size`'s reason."""
    return why.split(" ", 1)[0] if why else ""


def grade(rows: list[tuple[float, float, float, float, str]]) -> dict[str, Any]:
    """rows: (created, the playbook's result, the plain way's, its size, "full"/"half"/"") on the curve's
    alerts. {"full": {...}, "half": {...}, "equal", "sized", "line"}; a pass (0%) is no call of any size."""
    pools: dict[str, list[float]] = {"full": [], "half": []}
    for _, pb, _, _, lbl in rows:
        if pb != 0.0 and lbl:
            pools[lbl].append(pb)
    out: dict[str, Any] = {
        k: ({"n": len(xs), "avg": round(sum(xs) / len(xs), 2)} if xs else {"n": 0}) for k, xs in pools.items()
    }
    out["equal"] = round(sum(r[1] for r in rows), 2)
    out["sized"] = round(sum(r[1] * r[3] for r in rows), 2)
    f, h = out["full"], out["half"]
    if f["n"] and h["n"]:
        verdict = "it helps" if f["avg"] > h["avg"] else "it doesn't help yet"
        out["line"] = (
            f"Sized by conviction: full-size calls made {f['avg']:+.2f}% on average ({f['n']}), half-size "
            f"{h['avg']:+.2f}% ({h['n']}): {verdict}."
        )
    else:
        out["line"] = ""
    return out
