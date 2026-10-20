"""Where to put the stop for each kind of news: every stop, priced side by side on the same alerts.

Run 58 graded every way in and run 59 every way out; this prices every stop between them. Each graded
alert kept its worst dip, its way, until its session's close (heat.py: edge.exc), and its move to
the close. That's enough to replay any stop on it, from the alert price:

- **No stop:** out at the close, whatever happened in between.
- **1%, 2%, 3%, 5%:** out at the stop if the stock dipped that far against the call, else at the close.
- **The record's stop:** just past where 80% of its kind's winners bottomed (heat.py), the stop the
  playbook already suggests.
- **The plan's stop:** the alert's own plan.

A stop that was hit is assumed to fill at its price (no slippage), and before any gain that day.
Per kind of news the best stop is the one with the best average (8+ graded), and two numbers price
the trade-off every stop makes:

- **Stopped:** how often it cut the call;
- **Shaken out:** of those, how many finished the day its way anyway (the stop cost a winner).

So "Stop contract win news here at 3% (+0.9% on average, 14); no stop +0.6%; a 1% stop shook out
41% of the calls it cut" replaces a stop placed by feel. The alert's sheet, its kind's playbook, the
morning game plan and the playbook autopilot use it. History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

from .heat import BUFFER

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
WIDTHS = {"w1": 1.0, "w2": 2.0, "w3": 3.0, "w5": 5.0}
STYLES = {"none": "No stop", "w1": "A 1% stop", "w2": "A 2% stop", "w3": "A 3% stop", "w5": "A 5% stop",
          "heat": "The record's stop", "plan": "The plan's stop"}  # fmt: skip


def stop_widths(a: dict[str, Any], heat: dict[str, Any] | None = None) -> dict[str, float]:
    """{style: stop distance from the alert price, % (positive)} for the stops this alert can replay."""
    out = dict(WIDTHS)
    if heat and heat.get("dip80") is not None:
        out["heat"] = round(heat["dip80"] + BUFFER, 2)
    e = a.get("edge") or {}
    plan, o = e.get("plan") or {}, e.get("outcome") or {}
    if plan.get("stop") and o:
        sym, row = next(iter(o.items()))
        ref = (e.get("refs") or {}).get(sym) or plan.get("entry")
        if ref and row.get("sign"):
            w = -row["sign"] * (plan["stop"] / ref - 1) * 100
            if w > 0:
                out["plan"] = round(w, 2)
    return out


def replay(a: dict[str, Any], heat: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """{style: {"r": result %, its way, "stopped": bool, "won": finished the day its way}} per stop."""
    e = a.get("edge") or {}
    o, exc = e.get("outcome") or {}, e.get("exc") or {}
    if not o or exc.get("mae") is None:
        return {}
    _, row = next(iter(o.items()))
    if not row.get("sign") or row.get("close") is None:
        return {}
    close = row["sign"] * row["close"]
    won = close > 0
    out = {"none": {"r": round(close, 2), "stopped": False, "won": won}}
    for style, w in stop_widths(a, heat).items():
        hit = exc["mae"] <= -w
        out[style] = {"r": round(-w if hit else close, 2), "stopped": hit, "won": won}
    return out


def stop_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "n", "styles": {style: {"n", "avg", "median", "hit", "stopped", "shaken"}},
    "best", "width"}}: every stop per kind of news, side by side."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    heats = getattr(engine, "heat_rates", None) or {}
    pools: dict[str, dict[str, list[dict[str, Any]]]] = {}
    counts: dict[str, int] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        kinds = ("*", c["key"]) if c else ("*",)
        for kind in kinds:
            got = replay(a, heats.get(kind))
            if not got:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            for style, x in got.items():
                pools.setdefault(kind, {}).setdefault(style, []).append(x)
    out = {}
    for kind, styles in pools.items():
        if counts[kind] < MIN_N:
            continue
        rows = {}
        for s, xs in styles.items():
            rs = [x["r"] for x in xs]
            cut = [x for x in xs if x["stopped"]]
            rows[s] = {"n": len(xs), "avg": round(sum(rs) / len(rs), 2), "median": round(st.median(rs), 2),
                       "hit": round(sum(r > 0 for r in rs) / len(rs), 2), "stopped": round(len(cut) / len(xs), 2),
                       "shaken": round(sum(x["won"] for x in cut) / len(cut), 2) if cut else None}  # fmt: skip
        ok = {s: r for s, r in rows.items() if r["n"] >= MIN_N}
        best = max(ok, key=lambda s: (ok[s]["avg"], s == "none")) if ok else None
        h = heats.get(kind) or {}
        width = WIDTHS.get(best or "") or (
            round(h["dip80"] + BUFFER, 2) if best == "heat" and h.get("dip80") is not None else None
        )
        out[kind] = {"kind": kind, "label": "Any news" if kind == "*" else LABELS.get(kind, kind), "n": counts[kind],
                     "styles": rows, "best": best, "width": width}  # fmt: skip
    return out


def stop_line(r: dict[str, Any] | None) -> str:
    if not r or not r.get("best"):
        return ""
    b, s = r["best"], r["styles"]
    where = (
        "with no stop (out at the close)"
        if b == "none"
        else f"at {r['width']:.1f}%"
        if r.get("width")
        else "at the plan's stop"
    )
    head = f"Stop {r['label'].lower()} news here {where} ({s[b]['avg']:+.1f}% on average, {s[b]['n']})"
    if b != "none" and "none" in s:
        head += f"; no stop {s['none']['avg']:+.1f}%"
    tight = s.get("w1")
    if tight and tight.get("shaken") is not None and b != "w1" and tight["stopped"] >= 0.1:
        head += f"; a 1% stop shook out {round(tight['shaken'] * 100)}% of the calls it cut"
    return head + "."


def stop_price(entry: float, direction: str, width: float) -> float:
    """The stop price ``width`` % against a call entered at ``entry``."""
    sign = 1 if direction == "long" else -1
    return round(entry * (1 - sign * width / 100), 2)


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = sorted(rates.values(), key=lambda r: (r["kind"] != "*", -r["n"]))[:14]
    return {"rows": rows, "styles": STYLES, "need": MIN_N}
