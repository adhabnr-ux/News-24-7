"""Playbooks, written by the record: everything learned about one kind of news, on one page.

Each record answers one question about a kind of news: how often it goes its way (by size and by
regime), how fast (the news clock), what happens after the bell when it lands overnight (into the
open), whether to hold it into tomorrow (overnight), whether it keeps going for a month (drift),
how far its winners dip before working (heat, where a stop belongs), whether it's running hot or
cold lately (climate), how it does on Fed and jobs days (macro), and which stocks take it best and
worst. A playbook puts those answers for one kind on one page, and opens with the few rules of
thumb they support, each with its count, written only from records that clear their own bar:

    "Act fast: half its day's move comes within 5 minutes (24)."
    "Overnight, don't chase a big gap: gaps of 5%+ kept going after the open 36% (22)."
    "Stops: 80% of its winners dipped less than 0.9% first; a stop past 1.0% keeps them (31)."

Nothing is written without data behind it; a kind with little history gets a short page. History,
not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8


def _get(engine: Engine, name: str) -> dict[str, Any]:
    return getattr(engine, name, None) or {}


def _checked(line: str, r: dict[str, Any], names: dict[str, str], default: str) -> str:
    """A pick's rule of thumb, with what the out-of-sample check said about it (holdout.py)."""
    from .holdout import words

    w = words(r.get("holdout"), names, default)
    return f"{line[:-1]}; {w}." if w and line else line


def stocks_for(engine: Engine, key: str, days: float = 180, now: float | None = None) -> list[dict[str, Any]]:
    """The stocks with 3+ graded alerts of this kind, best first: how this news did on each."""
    from .catalysts import classify

    now = now or time.time()
    by: dict[str, list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        if not c or c["key"] != key:
            continue
        sym, row = next(iter(o.items()))
        if row.get("sign") and row.get("close") is not None:
            by.setdefault(sym, []).append(row["sign"] * row["close"])
    rows = [{"symbol": s, "n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)}
            for s, xs in by.items() if len(xs) >= 3]  # fmt: skip
    return sorted(rows, key=lambda r: (-r["median"], -r["n"]))


def playbook(engine: Engine, key: str, now: float | None = None) -> dict[str, Any] | None:
    from .catalysts import BAND_LABEL, LABELS

    if key not in LABELS:
        return None
    cat = _get(engine, "catalyst_rates")
    out: dict[str, Any] = {"key": key, "label": LABELS[key], "rules": []}
    rules = out["rules"]
    whole = cat.get(f"{key}|*") or {}
    out["record"] = {h: whole.get(h) for h in ("h1", "close", "d1")} | {"typical": whole.get("typical")}
    out["bands"] = [{"band": b, "label": BAND_LABEL.get(b, b), "close": (cat.get(f"{key}|{b}") or {}).get("close")}
                    for b in ("small", "mid", "large") if (cat.get(f"{key}|{b}") or {}).get("close")]  # fmt: skip
    c = whole.get("close")
    if c and c["n"] >= MIN_N:
        rules.append(
            f"It went its way {round(c['hit'] * 100)}% of the time by the close here (median {c['median']:+.1f}%, {c['n']})."
        )
    reg = _get(engine, "regime_rates")
    out["regimes"] = [
        {"regime": r, **reg[f"{key}|{r}"]} for r in ("risk-on", "mixed", "risk-off") if f"{key}|{r}" in reg
    ]
    ck = _get(engine, "clock_rates").get(key)
    if ck and ck.get("half"):
        out["clock"] = ck
        left = (ck.get("left") or {}).get("m15")
        if ck["half"] in ("m5", "m15"):
            when = "5 minutes" if ck["half"] == "m5" else "15 minutes"
            tail = (
                f"; bought 15 minutes in, {left['median']:+.1f}% more by the close ({left['n']})"
                if left
                else ""
            )
            rules.append(f"Act fast: half its day's move comes within {when} ({ck['n']}){tail}.")
        elif ck["half"] == "late":
            rules.append(f"No rush: it builds through the day, not half done by the hour ({ck['n']}).")
    op = _get(engine, "open_rates")
    gaps = [op[f"{key}|{g}"] for g in ("against", "flat", "small", "mid", "big") if f"{key}|{g}" in op]
    if gaps:
        out["open"] = gaps
        big = op.get(f"{key}|big")
        if big and big["n"] >= MIN_N:
            verb = "chase" if big["after"]["hit"] >= 0.55 else "don't chase"
            rules.append(
                f"Overnight, {verb} a big gap: gaps of 5%+ kept going after the open {round(big['after']['hit'] * 100)}% ({big['n']})."
            )
    on = _get(engine, "overnight_rates")
    nights = [on[f"{key}|{s}"] for s in ("against", "flat", "up", "big") if f"{key}|{s}" in on]
    if nights:
        out["overnight"] = nights
        up = on.get(f"{key}|up") or on.get(f"{key}|big")
        if up and up["n"] >= MIN_N:
            verb = (
                "hold it"
                if up["hit"] >= 0.55 and up["median"] > 0
                else "take it"
                if up["hit"] <= 0.45 or up["median"] < 0
                else "either way"
            )
            rules.append(
                f"Up at the close? {verb[:1].upper()}{verb[1:]}: it went on by the next close {round(up['hit'] * 100)}% (median {up['median']:+.1f}%, {up['n']})."
            )
    dr = _get(engine, "drift_rates").get(key)
    if dr and dr.get("verdict"):
        out["drift"] = dr
        words = {"drifts": "it kept drifting", "gives_back": "it gave it back", "holds": "it held"}
        rules.append(
            f"A month on, {words[dr['verdict']]}: {dr['d5']['median']:+.1f}% at five sessions, {dr['d20']['median']:+.1f}% at twenty ({dr['d20']['n']})."
        )
    ht = _get(engine, "heat_rates").get(key)
    if ht:
        out["heat"] = {k: ht.get(k) for k in ("winners", "dip80", "dip_median", "losers", "run_median")}
        rules.append(
            f"Stops: 80% of its winners dipped less than {ht['dip80']:.1f}% first; a stop just past that keeps them ({ht['winners']})."
        )
    en = _get(engine, "entry_rates").get(key)
    if en and en.get("best"):
        from .entries import STYLES, entry_line

        out["entries"] = {**en, "names": STYLES}
        rules.append(_checked(entry_line(en), en, STYLES, "alert"))
    ex = _get(engine, "exit_rates").get(key)
    if ex and ex.get("best"):
        from .exits_kind import STYLES as EXIT_STYLES
        from .exits_kind import exit_line

        out["exits"] = {**ex, "names": EXIT_STYLES}
        rules.append(_checked(exit_line(ex), ex, EXIT_STYLES, "close"))
    co = _get(engine, "combo_rates").get(key)
    if co and co.get("best"):
        from .combos import DEFAULT, NAMES, combo_line

        out["combos"] = co
        rules.append(_checked(combo_line(co), co, NAMES, DEFAULT))
    rp = _get(engine, "repeat_rates").get(key)
    if rp and rp.get("repeat") and rp.get("first"):
        out["repeat"] = rp
        rules.append(
            f"A second headline on the same stock within a week went its way {round(rp['repeat']['hit'] * 100)}% "
            f"({rp['repeat']['n']}); first headlines {round(rp['first']['hit'] * 100)}% ({rp['first']['n']})."
        )
    gb = _get(engine, "giveback_rates").get(key)
    if gb:
        from .giveback import BUCKETS
        from .giveback import MIN_N as GB_N

        parts = [
            f"{BUCKETS[b]} {gb[b]['avg']:+.1f}% ({gb[b]['n']})"
            for b in ("kept", "half", "most")
            if (gb.get(b) or {}).get("n", 0) >= GB_N
        ]
        if parts:
            out["giveback"] = gb
            rules.append("Overnight, by how much of its run it kept at the close: " + "; ".join(parts) + ".")
    so = _get(engine, "stop_rates").get(key)
    if so and so.get("best"):
        from .stops_kind import STYLES as STOP_STYLES
        from .stops_kind import stop_line

        out["stops"] = {**so, "names": STOP_STYLES}
        rules.append(_checked(stop_line(so), so, STOP_STYLES, "none"))
    cl = _get(engine, "climate_rates").get(key)
    if cl and cl.get("recent"):
        out["climate"] = cl
        if cl["state"] in ("hot", "cold"):
            rules.append(
                f"Lately it's {cl['state']}: {round(cl['recent']['hit'] * 100)}% in the last two weeks ({cl['recent']['n']}) vs {round(cl['before']['hit'] * 100)}% before."
            )
    mc = _get(engine, "macro_rates").get(key)
    if mc and mc.get("macro"):
        out["macro"] = mc
        m, o = mc["macro"], mc.get("other")
        if m["n"] >= MIN_N and o and abs(m["hit"] - o["hit"]) >= 0.15:
            rules.append(
                f"On Fed and jobs days it went its way {round(m['hit'] * 100)}% ({m['n']}) vs {round(o['hit'] * 100)}% on other days."
            )
    stocks = stocks_for(engine, key, now=now)
    out["stocks"] = {"best": stocks[:5], "worst": [s for s in reversed(stocks) if s["median"] < 0][:5]}
    out["sections"] = sum(
        1
        for k in ("clock", "open", "overnight", "drift", "heat", "climate", "macro", "entries", "exits")
        if k in out
    )
    return out


def playbooks(engine: Engine) -> dict[str, Any]:
    """Every kind of news with a graded record, most graded first, with how full its playbook is."""
    from .catalysts import LABELS

    cat = _get(engine, "catalyst_rates")
    rows = []
    for key, label in LABELS.items():
        c = (cat.get(f"{key}|*") or {}).get("close")
        if c:
            rows.append({"key": key, "label": label, "n": c["n"], "hit": c["hit"], "median": c["median"]})
    rows.sort(key=lambda r: -r["n"])
    return {"rows": rows}
