"""The whole playbook, backtested together: every way in paired with every way out, on the same alerts.

Runs 58-61 picked the best way in, the best way out and the best stop for each kind of news one at a
time. But a way in and a way out aren't independent: a stock bought on its second leg has already
run, and its next-day hold may be a different trade from the one bought at the alert. So every pair
is now replayed on each graded alert, from its own entry price to its own exit:

- in at the alert: out an hour in, at the close, the next day, five sessions on, or at the plan's
  bracket;
- in at the open, at the patient entry, on the second leg or when the call turns: out at the close,
  the next day or five sessions on;
- in on volume confirmation (15 minutes in): out an hour in, at the close, the next day or five
  sessions on.

Per kind of news, the best pair is the one with the best average (8+ alerts had it), checked out of
sample like every pick (holdout.py) against the plain way (in at the alert, out at the close). The
pair is what the playbook autopilot trades and what it promises, so its promise is the exact
backtest of what it does. The alert's sheet, its playbook and the Watch tab's board show it.
History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

from .entries import STYLES as IN
from .entries import _from, entry_moves
from .exits_kind import STYLES as OUT
from .exits_kind import exits

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
DEFAULT = "alert|close"
PAIRS = {"alert": ("h1", "close", "d1", "d5", "bracket"), "open": ("close", "d1", "d5"), "patient": ("close", "d1", "d5"),
         "leg2": ("close", "d1", "d5"), "turn": ("close", "d1", "d5"), "vol15": ("h1", "close", "d1", "d5")}  # fmt: skip
NAMES = {f"{i}|{o}": f"{IN[i]}, out {OUT[o].lower()}" for i, outs in PAIRS.items() for o in outs}


def combos(a: dict[str, Any]) -> dict[str, float]:
    """{"in|out": move from that entry to that exit, its way, %} for the pairs this alert had."""
    got = entry_moves(a)
    if not got:
        return {}
    sign, row, moves = got
    ex = exits(a)
    out = {}
    for i, mv in moves.items():
        for o in PAIRS[i]:
            if o == "bracket":
                if "bracket" in ex:
                    out[f"{i}|{o}"] = ex["bracket"]
            elif row.get(o) is not None:
                out[f"{i}|{o}"] = round(_from(sign, row[o], mv), 2)
    return out


def combo_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "n", "styles": {"in|out": {"n", "hit", "median", "avg"}}, "best", "holdout"}}."""
    from .catalysts import LABELS, classify
    from .holdout import check

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    dated: dict[str, dict[str, list[tuple[float, float]]]] = {}
    counts: dict[str, int] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        got = combos(a)
        if DEFAULT not in got:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            counts[kind] = counts.get(kind, 0) + 1
            for pair, x in got.items():
                pools.setdefault(kind, {}).setdefault(pair, []).append(x)
                dated.setdefault(kind, {}).setdefault(pair, []).append((a["created"], x))
    out = {}
    for kind, pairs in pools.items():
        if counts[kind] < MIN_N:
            continue
        rows = {p: {"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2),
                    "avg": round(sum(xs) / len(xs), 2)} for p, xs in pairs.items()}  # fmt: skip
        ok = {p: r for p, r in rows.items() if r["n"] >= MIN_N}
        best = max(ok, key=lambda p: (ok[p]["avg"], p == DEFAULT)) if ok else None
        out[kind] = {"kind": kind, "label": "Any news" if kind == "*" else LABELS.get(kind, kind), "n": counts[kind],
                     "styles": rows, "best": best, "holdout": check(dated[kind], DEFAULT, best)}  # fmt: skip
    return out


def combo_line(r: dict[str, Any] | None) -> str:
    if not r or not r.get("best"):
        return ""
    b, s = r["best"], r["styles"]
    head = (f"The playbook for {r['label'].lower()} news here, in and out together: {NAMES[b].lower()} "
            f"({s[b]['avg']:+.1f}% on average, {round(s[b]['hit'] * 100)}% its way, {s[b]['n']})")  # fmt: skip
    if b != DEFAULT and DEFAULT in s:
        head += f"; the plain way (at the alert, out at the close) {s[DEFAULT]['avg']:+.1f}%"
    return head + "."


def board(rates: dict[str, Any]) -> dict[str, Any]:
    from .holdout import words

    rows = sorted(rates.values(), key=lambda r: (r["kind"] != "*", -r["n"]))[:14]
    rows = [{**r, "check": words(r.get("holdout"), NAMES, DEFAULT),
             "top": sorted(((p, x) for p, x in r["styles"].items() if x["n"] >= MIN_N), key=lambda t: -t[1]["avg"])[:3]}
            for r in rows]  # fmt: skip
    return {"rows": rows, "names": NAMES, "need": MIN_N, "default": DEFAULT}
