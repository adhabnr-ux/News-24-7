"""How to exit each kind of news: every exit, graded side by side on the same alerts.

Run 58 graded every way in; this grades every way out, from the alert price, its way:
- **An hour in:** out after the first hour.
- **At the close:** out at the session's close.
- **The next day:** held to the next session's close.
- **Five sessions on:** held a week.
- **The plan's bracket:** the plan's stop and target; out at whichever the stock reached first,
  else at the close. When both were reached the same day, the stop is assumed first, the
  conservative read.

Per kind of news, the best exit is the one with the best average (8+ graded), and the alert's sheet,
its kind's playbook and a Watch-tab board say which: "Exit contract win news here: the next day
(+1.2% on average, 61%); at the close +0.8%". Together with the entries, each kind of news gets a
way in and a way out from its own record. History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
STYLES = {"h1": "An hour in", "close": "At the close", "d1": "The next day", "d5": "Five sessions on",
          "bracket": "The plan's bracket"}  # fmt: skip


def exits(a: dict[str, Any]) -> dict[str, float]:
    """{style: move from the alert price to that exit, its way, %} for the exits this alert had."""
    e = a.get("edge") or {}
    o = e.get("outcome") or {}
    if not o:
        return {}
    sym, row = next(iter(o.items()))
    sign = row.get("sign")
    if not sign:
        return {}
    out = {h: sign * row[h] for h in ("h1", "close", "d1", "d5") if row.get(h) is not None}
    plan = e.get("plan") or {}
    ref = (e.get("refs") or {}).get(sym)
    exc = e.get("exc") or {}
    if (
        ref
        and plan.get("stop")
        and plan.get("target")
        and exc.get("mae") is not None
        and row.get("close") is not None
    ):
        stop = sign * (plan["stop"] / ref - 1) * 100
        target = sign * (plan["target"] / ref - 1) * 100
        if exc["mae"] <= stop:
            out["bracket"] = stop  # the stop was reached (first, when both were)
        elif exc.get("mfe") is not None and exc["mfe"] >= target:
            out["bracket"] = target
        else:
            out["bracket"] = sign * row["close"]
    return {k: round(v, 2) for k, v in out.items()}


def exit_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "n", "styles": {style: {"n", "hit", "median", "avg"}}, "best"}}."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, int] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        got = exits(a)
        if "close" not in got:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            counts[kind] = counts.get(kind, 0) + 1
            for style, x in got.items():
                pools.setdefault(kind, {}).setdefault(style, []).append(x)
    out = {}
    for kind, styles in pools.items():
        if counts[kind] < MIN_N:
            continue
        rows = {s: {"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2),
                    "avg": round(sum(xs) / len(xs), 2)} for s, xs in styles.items()}  # fmt: skip
        ok = {s: r for s, r in rows.items() if r["n"] >= MIN_N}
        best = max(ok, key=lambda s: ok[s]["avg"]) if ok else None
        out[kind] = {"kind": kind, "label": "Any news" if kind == "*" else LABELS.get(kind, kind), "n": counts[kind],
                     "styles": rows, "best": best}  # fmt: skip
    return out


def exit_line(r: dict[str, Any] | None) -> str:
    if not r or not r.get("best"):
        return ""
    b, s = r["best"], r["styles"]
    head = f"Exit {r['label'].lower()} news here: {STYLES[b].lower()} ({s[b]['avg']:+.1f}% on average, {round(s[b]['hit'] * 100)}% its way, {s[b]['n']})"
    if b != "close" and "close" in s:
        head += f"; at the close {s['close']['avg']:+.1f}%"
    return head + "."


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = sorted(rates.values(), key=lambda r: (r["kind"] != "*", -r["n"]))[:14]
    return {"rows": rows, "styles": STYLES, "need": MIN_N}
