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

Each kind's best pair and the plain way also get an 80% range for their average (a seeded
bootstrap: the same record gives the same range). A best pair whose range still covers the plain
way's average is marked **unproven**: it may be the better way, but the record can't yet tell it
from luck.

Paper fills at the alert price are instant; a person's aren't. So a best pair that enters at the
alert is also graded **5 minutes late**, from where the stock stood 5 minutes after the alert (the
news clock's m5 checkpoint) to the same exit. When its edge is gone by then (positive at the alert,
0% or less 5 minutes late), the kind is marked **too fast to trade by hand**: the paper autopilot
can take it, a person reading the push mostly can't.

History, not a forecast.
"""

from __future__ import annotations

import random
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
RESAMPLES = 400
DEFAULT = "alert|close"
PAIRS = {"alert": ("h1", "close", "d1", "d5", "bracket"), "open": ("close", "d1", "d5"), "patient": ("close", "d1", "d5"),
         "leg2": ("close", "d1", "d5"), "turn": ("close", "d1", "d5"), "vol15": ("h1", "close", "d1", "d5")}  # fmt: skip
NAMES = {f"{i}|{o}": f"{IN[i]}, out {OUT[o].lower()}" for i, outs in PAIRS.items() for o in outs}
# alerts with no kind of news read get a playbook by what raised them
KIND_LABELS = {"news": "Other news", "options": "Options flow", "confluence": "Confluence", "price": "Price moves",
               "chatter": "Chatter", "trend": "Trends", "narrative": "Narratives", "attention": "Attention"}  # fmt: skip


def key_of(a: dict[str, Any], catalyst: dict[str, Any] | None) -> str:
    """The playbook's key for an alert: its kind of news, else what raised it ("kind:options")."""
    return catalyst["key"] if catalyst else f"kind:{a.get('kind') or 'news'}"


def label_of(key: str) -> str:
    from .catalysts import LABELS

    if key == "*":
        return "Any news"
    if key.startswith("kind:"):
        return KIND_LABELS.get(key[5:], key[5:].capitalize())
    return LABELS.get(key, key)


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


def late(a: dict[str, Any], minutes: str = "m5") -> dict[str, float]:
    """{out: move from where the stock stood ``minutes`` after the alert to each way out, its way, %}:
    the alert pairs as someone 5 minutes late would have traded them (no bracket: its levels were set
    from the alert price)."""
    got = entry_moves(a)
    if not got:
        return {}
    sign, row, _ = got
    if row.get(minutes) is None:
        return {}
    return {
        o: round(_from(sign, row[o], row[minutes]), 2)
        for o in PAIRS["alert"]
        if o != "bracket" and row.get(o) is not None
    }


def combo_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "n", "styles": {"in|out": {"n", "hit", "median", "avg"}}, "best", "holdout"}}."""
    from .catalysts import classify
    from .holdout import check

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    dated: dict[str, dict[str, list[tuple[float, float]]]] = {}
    lates: dict[str, dict[str, list[float]]] = {}  # the alert pairs, 5 minutes late
    counts: dict[str, int] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        got = combos(a)
        if DEFAULT not in got:
            continue
        lt = late(a)
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for kind in ("*", key_of(a, c)):
            counts[kind] = counts.get(kind, 0) + 1
            for pair, x in got.items():
                pools.setdefault(kind, {}).setdefault(pair, []).append(x)
                dated.setdefault(kind, {}).setdefault(pair, []).append((a["created"], x))
            for o, x in lt.items():
                lates.setdefault(kind, {}).setdefault(o, []).append(x)
    out = {}
    for kind, pairs in pools.items():
        if counts[kind] < MIN_N:
            continue
        rows = {p: {"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2),
                    "avg": round(sum(xs) / len(xs), 2)} for p, xs in pairs.items()}  # fmt: skip
        ok = {p: r for p, r in rows.items() if r["n"] >= MIN_N}
        best = max(ok, key=lambda p: (ok[p]["avg"], p == DEFAULT)) if ok else None
        for p in {best, DEFAULT} - {None}:  # how sure each average is (bootstrap, 80%)
            if p in rows and rows[p]["n"] >= MIN_N:
                rows[p]["lo"], rows[p]["hi"] = ranged(pairs[p], f"{kind}|{p}")
        proven = best == DEFAULT or (
            best is not None
            and "lo" in rows.get(best, {})
            and DEFAULT in rows
            and rows[best]["lo"] > rows[DEFAULT]["avg"]
        )
        out[kind] = {"kind": kind, "label": label_of(kind), "n": counts[kind],
                     "styles": rows, "best": best, "proven": proven, "holdout": check(dated[kind], DEFAULT, best)}  # fmt: skip
        if best and best.startswith("alert|") and best != "alert|bracket":
            xs = (lates.get(kind) or {}).get(best.split("|")[1]) or []
            if len(xs) >= MIN_N:  # the same pair, 5 minutes late: does the edge survive a human's delay?
                avg = round(sum(xs) / len(xs), 2)
                out[kind]["late"] = {"n": len(xs), "avg": avg, "fast": rows[best]["avg"] > 0 >= avg}
    return out


def ranged(xs: list[float], seed: str, draws: int = RESAMPLES) -> tuple[float, float]:
    """The 80% bootstrap range of the average of ``xs`` (seeded: the same record gives the same range)."""
    rng = random.Random(seed)
    m = len(xs)
    draws = draws if m <= 500 else max(100, draws * 500 // m)  # big pools: fewer draws, as tight anyway
    means = sorted(sum(rng.choice(xs) for _ in range(m)) / m for _ in range(draws))
    return round(means[int(draws * 0.1)], 2), round(means[int(draws * 0.9) - 1], 2)


def combo_line(r: dict[str, Any] | None) -> str:
    if not r or not r.get("best"):
        return ""
    b, s = r["best"], r["styles"]
    head = (f"The playbook for {r['label'].lower()} news here, in and out together: {NAMES[b].lower()} "
            f"({s[b]['avg']:+.1f}% on average, {round(s[b]['hit'] * 100)}% its way, {s[b]['n']})")  # fmt: skip
    if "lo" in s[b]:
        head += f", likely {s[b]['lo']:+.1f}% to {s[b]['hi']:+.1f}%"
    if b != DEFAULT and DEFAULT in s:
        head += f"; the plain way (at the alert, out at the close) {s[DEFAULT]['avg']:+.1f}%"
        if not r.get("proven"):
            head += ", inside that range: unproven"
    lt = r.get("late")
    if lt:
        head += f"; 5 minutes late {lt['avg']:+.1f}% ({lt['n']})" + (
            ": too fast to trade by hand" if lt["fast"] else ""
        )
    return head + "."


def board(rates: dict[str, Any]) -> dict[str, Any]:
    from .holdout import words

    rows = sorted(rates.values(), key=lambda r: (r["kind"] != "*", -r["n"]))[:14]
    rows = [{**r, "check": words(r.get("holdout"), NAMES, DEFAULT),
             "top": sorted(((p, x) for p, x in r["styles"].items() if x["n"] >= MIN_N), key=lambda t: -t[1]["avg"])[:3]}
            for r in rows]  # fmt: skip
    return {"rows": rows, "names": NAMES, "need": MIN_N, "default": DEFAULT}
