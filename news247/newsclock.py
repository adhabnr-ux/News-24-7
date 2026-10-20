"""The news clock: how fast each kind of news plays out here, and what's left for a late entry.

Every alert's price is now also stamped 5, 15 and 30 minutes in (dossier.record_outcomes: m5, m15,
m30), beside the hour and the close. Pooled per kind of news, the medians draw its clock:

- **How fast:** the first checkpoint by which the typical alert had made half its day's move (its
  way, the close's median). *Fast news* is half done within 15 minutes: a late entry is chasing.
  *Slow news* hasn't made half by the hour: it builds through the day, and there's no rush.
- **What's left:** for an entry at 5, 15 or 30 minutes in, the move from there to the close: how
  often it went on the alert's way, and the median.
- **Live, between 5 minutes and an hour in:** the alert's move so far picks a row (already ran
  0.5%+, flat, or against it by 0.5%+), so "15 minutes in, +0.8% its way" gets the record of alerts
  that stood the same at the same point. After an hour the live odds take over (liveodds.py).

Only news that lands in the session is clocked (news from outside it is read at the open,
opening.py), and only alerts priced at a checkpoint count, so the close they're measured against is
their own. Recorded going forward: a kind needs 8 alerts graded at a checkpoint and the close before
its clock is read. History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
STEPS = (("m5", 5), ("m15", 15), ("m30", 30), ("h1", 60))
BINS = (("ran", "already 0.5%+ its way"), ("flat", "within 0.5% of its price"),
        ("against", "0.5%+ against it"))  # fmt: skip


def _bin(x: float) -> str:
    return "ran" if x >= 0.5 else "against" if x <= -0.5 else "flat"


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def clock_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"key", "n", "path": {step: median}, "close", "half", "left": {step: stats},
    "after": {step: {bin: stats}}}} for "*" and each kind of news (catalysts.classify)."""
    from .catalysts import LABELS, classify
    from .opening import outside

    now = now or time.time()
    holidays = getattr(engine.calendar, "holidays", set())
    pools: dict[str, dict[str, Any]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if (
            not row.get("sign")
            or row.get("close") is None
            or not any(row.get(k) is not None for k, _ in STEPS)
        ):
            continue
        if outside(float(a.get("created") or 0), holidays):
            continue  # news from outside the session is read at the open instead (opening.py)
        s = row["sign"]
        close = s * row["close"]
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for key in ("*", c["key"]) if c else ("*",):
            g = pools.setdefault(key, {"close": [], "steps": {k: [] for k, _ in STEPS}})
            g["close"].append(close)
            for k, _ in STEPS:
                if row.get(k) is not None:
                    g["steps"][k].append((s * row[k], close))
    out = {}
    for key, g in pools.items():
        if len(g["close"]) < MIN_N:
            continue
        close_med = round(st.median(g["close"]), 2)
        r: dict[str, Any] = {"key": key, "label": "Every alert" if key == "*" else LABELS.get(key, key),
                             "n": len(g["close"]), "close": close_med, "path": {}, "left": {}, "after": {}}  # fmt: skip
        for k, _ in STEPS:
            pairs = g["steps"][k]
            if len(pairs) < MIN_N:
                continue
            r["path"][k] = round(st.median(m for m, _ in pairs), 2)
            r["left"][k] = _stats([cl - m for m, cl in pairs])
            by: dict[str, list[float]] = {}
            for m, cl in pairs:
                by.setdefault(_bin(m), []).append(cl - m)
            r["after"][k] = {b: _stats(xs) for b, xs in by.items() if len(xs) >= MIN_N}
        r["half"] = _half(r)
        if r["path"]:
            out[key] = r
    return out


def _half(r: dict[str, Any]) -> str | None:
    """The first step by which the typical alert had half its day's move: "m5".."h1", "late" when
    not even by the hour, None when the kind doesn't go its way by the close (no clock to read)."""
    if r["close"] < 0.2:
        return None
    for k, _ in STEPS:
        if k in r["path"] and r["path"][k] >= 0.5 * r["close"]:
            return k
    return "late" if "h1" in r["path"] else None


WHEN = {"m5": "5 minutes", "m15": "15 minutes", "m30": "30 minutes", "h1": "an hour"}


def clock_for(rates: dict[str, Any], a: dict[str, Any], universe: Any = None) -> dict[str, Any] | None:
    """This alert's kind's clock when it has one, else every alert's."""
    from .catalysts import classify

    c = (a.get("edge") or {}).get("catalyst") or classify(a, universe)
    for key in (c["key"], "*") if c else ("*",):
        r = rates.get(key)
        if r and r.get("half"):
            return r
    return None


def stamp(r: dict[str, Any] | None) -> dict[str, Any] | None:
    """The compact clock an alert carries (edge.clock)."""
    if not r:
        return None
    return {"key": r["key"], "label": r["label"], "half": r["half"], "close": r["close"], "n": r["n"],
            "path": r["path"], "left": r["left"]}  # fmt: skip


def clock_line(c: dict[str, Any] | None) -> str:
    """The push's line, only when the speed changes what to do: fast news (half done within 15
    minutes) or slow news (not half done by the hour)."""
    if not c or not c.get("half"):
        return ""
    who = "news like this" if c["key"] == "*" else f"{c['label']} news"
    if c["half"] in ("m5", "m15"):
        left = (c.get("left") or {}).get("m15")
        tail = (
            f"; bought 15 minutes in, {round(left['hit'] * 100)}% went on its way (median {left['median']:+.1f}% more, {left['n']})"
            if left
            else ""
        )
        return f"⏱ Fast: {who} made half its day's move within {WHEN[c['half']]} here{tail}"
    if c["half"] == "late":
        left = (c.get("left") or {}).get("m30")
        tail = f"; 30 minutes in, {left['median']:+.1f}% more to come (median, {left['n']})" if left else ""
        return f"⏱ Slow: {who} built through the day here, no rush{tail}"
    return ""


def early(engine: Engine, a: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """For an alert 5 minutes to an hour old: its move so far, at the last checkpoint passed, and how
    alerts of its kind that stood the same there finished: {"step", "minutes", "since", "bin", ...stats}."""
    from .dossier import _direction, _since

    now = now or time.time()
    age = now - float(a.get("created") or now)
    if not (300 <= age < 3600) or not a.get("tickers"):
        return None
    sign = {"up": 1, "down": -1}.get(_direction(a), 0)
    since = _since(a, a["tickers"][0], lambda sym: engine.price_of(sym, 900, now=now))
    if not sign or since is None:
        return None
    its = sign * since
    step = "m30" if age >= 1800 else "m15" if age >= 900 else "m5"
    rates = getattr(engine, "clock_rates", None) or {}
    from .catalysts import classify

    c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
    b = _bin(its)
    for key in (c["key"], "*") if c else ("*",):
        x = ((rates.get(key) or {}).get("after") or {}).get(step, {}).get(b)
        if x:
            label = (rates[key]["label"] + " news") if key != "*" else "alerts like this"
            return {"step": step, "minutes": dict(STEPS)[step], "since": round(its, 2), "bin": b,
                    "bin_label": dict(BINS)[b], "label": label, **x}  # fmt: skip
    return None


def early_line(e: dict[str, Any] | None) -> str:
    if not e:
        return ""
    return (f"{e['minutes']} minutes in, {e['since']:+.1f}% its way: {e['label']} "
            f"that were {e['bin_label']} by then went on its way {round(e['hit'] * 100)}% of the time "
            f"(median {e['median']:+.1f}% more by the close, {e['n']})")  # fmt: skip


def clock_board(rates: dict[str, Any]) -> dict[str, Any]:
    """Every kind with a clock, fastest first."""
    order = {"m5": 0, "m15": 1, "m30": 2, "h1": 3, "late": 4}
    rows = sorted((r for r in rates.values() if r.get("half")), key=lambda r: (order[r["half"]], -r["n"]))
    return {"rows": rows, "need": MIN_N, "when": WHEN}
