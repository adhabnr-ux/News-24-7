"""How to enter each kind of news: every entry style, graded side by side on the same alerts.

The desk now knows several ways into a news move, each with its own record in its own corner:
- **At the alert:** the price when it went out.
- **At the open:** news from outside the session, entered at the first print.
- **At the patient entry:** a limit at the winners' median dip; filled when the alert's worst dip
  reached it.
- **On the second leg:** the breakout to a new extreme an hour or more in.
- **On volume confirmation:** 15 minutes in, entered only when the stock trades 5x+ its normal volume.
- **When the call turns to act:** during the day, as the live facts change the verdict.

The conditional styles (all but the first) only enter some alerts, so each is graded on the alerts
it entered: how often it went its way by the close, the median and the average move from its own
entry, and how many of the kind's alerts it entered. Per kind of news, the best style is the one
with the best average from its entry (8+ entries). That turns "contract wins work" into "contract
wins work best on the second leg". History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
STYLES = {"alert": "At the alert", "open": "At the open", "patient": "At the patient entry", "leg2": "On the second leg",
          "vol15": "On volume confirmation", "turn": "When the call turns to act"}  # fmt: skip


def _from(sign: int, close: float, entry_move: float) -> float:
    """The move from an entry (its own move from the alert price, raw %) to the close, its way."""
    return sign * ((1 + close / 100) / (1 + entry_move / 100) - 1) * 100


def entry_moves(a: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, float]] | None:
    """(sign, outcome row, {style: the entry's own move from the alert price, raw %}) for the styles
    this alert had; None when it isn't graded."""
    e = a.get("edge") or {}
    o = e.get("outcome") or {}
    refs = e.get("refs") or {}
    if not o:
        return None
    sym, row = next(iter(o.items()))
    sign, close = row.get("sign"), row.get("close")
    if not sign or close is None:
        return None
    mv = {"alert": 0.0}
    if row.get("open") is not None:
        mv["open"] = row["open"]
    pt = (e.get("plan") or {}).get("patient") or {}
    mae = (e.get("exc") or {}).get("mae")
    if pt.get("dip") and mae is not None and mae <= -pt["dip"]:
        mv["patient"] = -sign * pt["dip"]
    ref = refs.get(sym)
    if ref:
        for style, px in (("leg2", (e.get("leg2") or {}).get("px")),
                          ("turn", (e.get("call_now") or {}).get("px") if (e.get("call_now") or {}).get("verb") == "act" else None)):  # fmt: skip
            if px:
                mv[style] = (px / ref - 1) * 100
    r15 = (e.get("rvol") or {}).get("r15")
    if r15 is not None and r15 >= 5 and row.get("m15") is not None:
        mv["vol15"] = row["m15"]
    return sign, row, mv


def entries(a: dict[str, Any]) -> dict[str, float]:
    """{style: move from that entry to the close, its way, %} for the styles this alert had."""
    got = entry_moves(a)
    if not got:
        return {}
    sign, row, mv = got
    return {k: round(_from(sign, row["close"], m), 2) for k, m in mv.items()}


def entry_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "n", "styles": {style: {"n", "share", "hit", "median", "avg"}}, "best"}}
    for every kind of news ("*" for any) with 8+ graded alerts."""
    from .catalysts import LABELS, classify
    from .holdout import check

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    dated: dict[str, dict[str, list[tuple[float, float]]]] = {}  # for the out-of-sample check
    counts: dict[str, int] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        got = entries(a)
        if not got:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            counts[kind] = counts.get(kind, 0) + 1
            for style, x in got.items():
                pools.setdefault(kind, {}).setdefault(style, []).append(x)
                dated.setdefault(kind, {}).setdefault(style, []).append((a["created"], x))
    out = {}
    for kind, styles in pools.items():
        n = counts[kind]
        if n < MIN_N:
            continue
        rows = {}
        for style, xs in styles.items():
            rows[style] = {"n": len(xs), "share": round(len(xs) / n, 2), "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                           "median": round(st.median(xs), 2), "avg": round(sum(xs) / len(xs), 2)}  # fmt: skip
        ok = {s: r for s, r in rows.items() if r["n"] >= MIN_N}
        best = max(ok, key=lambda s: ok[s]["avg"]) if ok else None
        out[kind] = {"kind": kind, "label": "Any news" if kind == "*" else LABELS.get(kind, kind), "n": n,
                     "styles": rows, "best": best, "holdout": check(dated[kind], "alert", best)}  # fmt: skip
    return out


def entry_line(r: dict[str, Any] | None) -> str:
    """ "How to enter contract win news here: on the second leg (+1.4% on average from the entry, 70%,
    12 of 40 alerts had one); at the alert +0.9%."."""
    if not r or not r.get("best"):
        return ""
    b, s = r["best"], r["styles"]
    head = (f"How to enter {r['label'].lower()} news here: {STYLES[b].lower()} ({s[b]['avg']:+.1f}% on average from the entry, "
            f"{round(s[b]['hit'] * 100)}% its way, {s[b]['n']} of {r['n']} alerts)")  # fmt: skip
    if b != "alert" and "alert" in s:
        head += f"; at the alert {s['alert']['avg']:+.1f}%"
    return head + "."


def board(rates: dict[str, Any]) -> dict[str, Any]:
    from .holdout import words

    rows = sorted(rates.values(), key=lambda r: (r["kind"] != "*", -r["n"]))[:14]
    rows = [{**r, "check": words(r.get("holdout"), STYLES, "alert")} for r in rows]
    return {"rows": rows, "styles": STYLES, "need": MIN_N}
