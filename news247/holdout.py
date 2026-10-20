"""Picks checked out of sample: is the record's best way in, out or stop real, or the luck of the draw?

Each "best" is the style with the best average among several (six entries, five exits, seven stops)
on as few as eight alerts. The best of several noisy averages is high partly by chance, so a pick can
look good and mean nothing. The remedy is the oldest one in testing: choose on the past, check on
what came after.

Per kind of news, the alerts are split by time: the older two-thirds pick a style (the best average
there, 5+), and the newest third grades it against the default (at the alert, at the close, no stop):

- **Held:** the older alerts picked the same style as the full record, and on the newest third it did
  at least as well as the default (4+ each).
- **Didn't hold:** the older alerts picked something else, or the pick trailed the default on the
  newest third. The desk then uses the default for that kind (sheet, game plan, playbook autopilot)
  and says why.
- **Unverified:** too few new alerts to tell; the pick stands.

History, not a forecast; a third of a small sample is smaller still, and counts are shown.
"""

from __future__ import annotations

from typing import Any

SPLIT = 2 / 3
MIN_TRAIN = 5
MIN_TEST = 4


def _avg(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 2)


def check(
    dated: dict[str, list[tuple[float, float]]], default: str, best: str | None
) -> dict[str, Any] | None:
    """{"pick", "since", "n", "avg", "base", "base_n", "held"} for one kind's styles, each a list of
    (alert time, result); None when there's too little to split."""
    ts = sorted({t for xs in dated.values() for t, _ in xs})
    if not best or len(ts) < MIN_TRAIN + MIN_TEST:
        return None
    cut = ts[int(len(ts) * SPLIT)]
    train = {s: [x for t, x in xs if t < cut] for s, xs in dated.items()}
    test = {s: [x for t, x in xs if t >= cut] for s, xs in dated.items()}
    ok = {s: xs for s, xs in train.items() if len(xs) >= MIN_TRAIN}
    if not ok:
        return None
    pick = max(ok, key=lambda s: (_avg(ok[s]), s == default))
    tp, td = test.get(pick) or [], test.get(default) or []
    out: dict[str, Any] = {"pick": pick, "best": best, "since": cut, "n": len(tp), "avg": _avg(tp) if tp else None,
                           "base": _avg(td) if td else None, "base_n": len(td)}  # fmt: skip
    if best == default:
        out["held"] = True  # the default needs no defending
    elif len(tp) < MIN_TEST or len(td) < MIN_TEST:
        out["held"] = None
    else:
        out["held"] = pick == best and out["avg"] >= out["base"]
    return out


def use(r: dict[str, Any] | None, default: str) -> str | None:
    """The style to act on: the record's best, unless it didn't hold out of sample."""
    if not r or not r.get("best"):
        return None
    return default if (r.get("holdout") or {}).get("held") is False else r["best"]


def words(h: dict[str, Any] | None, names: dict[str, str], default: str) -> str:
    """ "checked on the newest third: held (+0.9% vs +0.2% at the default, 6)"."""
    if not h:
        return ""
    if h["held"] is None:
        return f"too few new alerts to check it yet ({h['n']})"
    if h["best"] == default:
        return ""
    d = names[default].lower()
    if h["held"]:
        return f"checked on the newest third: held ({h['avg']:+.1f}% vs {h['base']:+.1f}% {d}, {h['n']})"
    if h["pick"] != h["best"]:
        if h["pick"] == default:
            return f"on the older alerts nothing beat {d}: not stable, so {d}"
        return f"the older alerts picked {names[h['pick']].lower()} instead: not stable, so {d}"
    return f"on the newest third it made {h['avg']:+.1f}% vs {h['base']:+.1f}% {d} ({h['n']}): didn't hold, so {d}"


def stamp(
    r: dict[str, Any], kind: str, default: str, names: dict[str, str], extra: tuple[str, ...] = ()
) -> dict[str, Any]:
    """An alert's copy of its kind's pick: the style to act on, and the out-of-sample verdict behind it."""
    h = r.get("holdout")
    best = use(r, default) or default
    keep = (r["best"], default, *extra)
    out: dict[str, Any] = {"kind": kind, "label": r["label"], "best": best, "n": r["n"],
                           "styles": {s: r["styles"][s] for s in keep if s in r["styles"]}}  # fmt: skip
    if "width" in r:
        out["width"] = r["width"] if best == r["best"] else None
    if h:
        out["held"] = h["held"]
        out["check"] = words(h, names, default)
    if best != r["best"]:
        out["pick"] = r["best"]
    return out
