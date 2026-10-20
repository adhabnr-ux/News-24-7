"""Your reaction time, priced: how long you take to act on a push, and what the wait costs you.

Every paper call opened from an alert records when it was opened. The alert records when it went
out and its price then. So each call says how long its phone took to act and what that cost: the
entry against the alert price, its way (negative: you paid up). Pooled per phone:
- the median delay and the median cost;
- the cost by speed (under 5 minutes, 5-15, 15-60, an hour+), so the price of every extra minute
  shows;
- per kind of news, against how fast that news usually plays out (the news clock): "contract wins
  make half their move within 15 minutes here; you take 22 minutes on average: you start 1.1% late".

The fix is a tap away: alert pushes carry a **Track** button where the phone shows them (Android,
desktop), which opens the paper call from the lock screen with the alert's plan, without opening
the app. Calls opened that way are marked, and the record shows whether they did better. Paper
only; history, not a forecast.
"""

from __future__ import annotations

import statistics as st
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 5
SPEEDS = (("fast", "under 5 minutes", 0, 300), ("soon", "5-15 minutes", 300, 900), ("late", "15-60 minutes", 900, 3600),
          ("slow", "an hour or more", 3600, float("inf")))  # fmt: skip


def speed(delay: float) -> str:
    return next(k for k, _, lo, hi in SPEEDS if lo <= delay < hi)


def calls(engine: Engine, device: str) -> list[dict[str, Any]]:
    """Each paper call opened from an alert: {symbol, kind, delay, cost, via}."""
    from .catalysts import classify

    p = engine.desk.profiles.get(device) or {}
    out = []
    for t in [*(p.get("tracked") or []), *(p.get("closed") or [])]:
        if not t.get("alert_id") or not t.get("entry") or not t.get("ts") or t.get("auto"):
            continue  # autopilot calls react by themselves
        a = engine.storage.get_alert(t["alert_id"])
        if not a:
            continue
        e = a.get("edge") or {}
        plan = e.get("plan") or {}
        # the price when the alert went out: its reference, else its plan's entry (the live price then)
        ref = (e.get("refs") or {}).get(t["symbol"]) or (
            plan.get("entry") if plan.get("symbol") == t["symbol"] else None
        )
        if not ref:
            continue
        sign = 1 if t.get("direction", "long") == "long" else -1
        c = e.get("catalyst") or classify(a, engine.universe)
        out.append({"symbol": t["symbol"], "kind": c["key"] if c else None, "label": c["label"] if c else None,
                    "delay": max(0.0, float(t["ts"]) - float(a["created"])),
                    "cost": round(sign * (ref / float(t["entry"]) - 1) * 100, 2), "via": t.get("via") or ""})  # fmt: skip
    return out


def reaction(engine: Engine, device: str) -> dict[str, Any]:
    """This phone's delay and its cost, by speed and by kind of news, against the news clock."""
    from .newsclock import WHEN

    cs = calls(engine, device)
    out: dict[str, Any] = {"n": len(cs), "need": MIN_N}
    if len(cs) < MIN_N:
        return out
    out["delay"] = round(st.median(c["delay"] for c in cs))
    out["cost"] = round(st.median(c["cost"] for c in cs), 2)
    by_speed = []
    for k, label, _, _ in SPEEDS:
        xs = [c["cost"] for c in cs if speed(c["delay"]) == k]
        if xs:
            by_speed.append({"speed": k, "label": label, "n": len(xs), "cost": round(st.median(xs), 2)})
    out["speeds"] = by_speed
    clocks = getattr(engine, "clock_rates", None) or {}
    kinds = []
    for kind in sorted({c["kind"] for c in cs if c["kind"]}):
        ks = [c for c in cs if c["kind"] == kind]
        if len(ks) < 3:
            continue
        ck = clocks.get(kind) or {}
        row = {"kind": kind, "label": ks[0]["label"], "n": len(ks), "delay": round(st.median(c["delay"] for c in ks)),
               "cost": round(st.median(c["cost"] for c in ks), 2), "half": ck.get("half"),
               "half_label": WHEN.get(ck.get("half") or "", "")}  # fmt: skip
        kinds.append(row)
    out["kinds"] = sorted(kinds, key=lambda r: r["cost"])
    lock = [c["cost"] for c in cs if c["via"] == "lockscreen"]
    app = [c["cost"] for c in cs if c["via"] != "lockscreen"]
    if lock and app:
        out["lockscreen"] = {
            "n": len(lock),
            "cost": round(st.median(lock), 2),
            "app_cost": round(st.median(app), 2),
        }
    return out


def line(r: dict[str, Any]) -> str:
    if r["n"] < MIN_N:
        return ""
    m = r["delay"] / 60
    when = f"{m:.0f} minutes" if m < 90 else f"{m / 60:.1f} hours"
    return f"You act {when} after a push at the median, at {r['cost']:+.2f}% against the alert price ({r['n']} calls)."
