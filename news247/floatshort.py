"""Float and short interest, graded: how news runs on a stock with few shares to trade, or many short.

The same contract win means one thing for a stock with 8 million shares in the float and another for
one with 800 million: a small float has to reprice on few shares, and a crowded short has to buy
back. Neither was on the alerts. Now every news alert's stock carries its float and short interest
(the share of the float sold short), read from Yahoo's key statistics and kept a week per stock.

Graded by the close, by float band (under 10M, 10-50M, 50-200M, 200M+ shares) and by short band
(under 5%, 5-15%, 15-30%, 30%+), for every kind of news and for each kind, each record gives how
often the news went its way, the median move, and the median best run before the close (how far
the winners ran). An alert carries its band's record, the most specific with 8+ graded. A strong
or weak one counts in the call ("tiny-float news went its way 71%").

When a stock's statistics can't be read, nothing is stamped: no guess.
"""

from __future__ import annotations

import json
import math
import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

QS = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
TTL = 7 * 86400
MIN_N = 8
FLOAT_BANDS = (("tiny", "a float under 10M shares", 0, 10e6), ("small", "a 10-50M share float", 10e6, 50e6),
               ("mid", "a 50-200M share float", 50e6, 200e6), ("large", "a 200M+ share float", 200e6, math.inf))  # fmt: skip
SHORT_BANDS = (("low", "under 5% short", 0, 0.05), ("some", "5-15% short", 0.05, 0.15),
               ("high", "15-30% short", 0.15, 0.30), ("crowded", "30%+ short", 0.30, math.inf))  # fmt: skip
F_LABEL = {k: lbl for k, lbl, _, _ in FLOAT_BANDS}
S_LABEL = {k: lbl for k, lbl, _, _ in SHORT_BANDS}


def fband(shares: float | None) -> str | None:
    if not shares or shares <= 0:
        return None
    return next(k for k, _, lo, hi in FLOAT_BANDS if lo <= shares < hi)


def sband(short: float | None) -> str | None:
    if short is None or short < 0:
        return None
    return next(k for k, _, lo, hi in SHORT_BANDS if lo <= short < hi)


# --------------------------------------------------------------------------- reading


def parse(doc: Any) -> dict[str, Any] | None:
    """Yahoo quoteSummary (defaultKeyStatistics) -> {"float", "short", "out"}, or None without a float."""
    res = (((doc or {}).get("quoteSummary") or {}).get("result") or [None])[0] or {}
    ks = res.get("defaultKeyStatistics") or {}

    def raw(k: str) -> float | None:
        v = ks.get(k)
        v = v.get("raw") if isinstance(v, dict) else v
        return float(v) if isinstance(v, int | float) and v == v else None

    fl = raw("floatShares")
    if not fl or fl <= 0:
        return None
    short = raw("shortPercentOfFloat")
    if short is None and raw("sharesShort") is not None:
        short = raw("sharesShort") / fl  # type: ignore[operator]
    out = raw("sharesOutstanding")
    return {
        "float": int(fl),
        "short": round(short, 4) if short is not None else None,
        "out": int(out) if out else None,
    }


def cached(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any] | None:
    """The stock's statistics if read within a week."""
    now = now or time.time()
    raw = engine.storage.get_setting(f"float:{sym}")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if now - d.get("ts", 0) < TTL and d.get("float") else None


async def fetch(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any] | None:
    """Read the stock's statistics from Yahoo and keep them a week. None when they can't be read."""
    now = now or time.time()
    feed = getattr(engine, "smallcap_feed", None)
    if feed is None or not hasattr(feed, "yahoo_json"):
        return None
    try:
        doc = await feed.yahoo_json(QS.format(sym=sym), {"modules": "defaultKeyStatistics"})
    except Exception:  # noqa: BLE001 - no statistics: nothing is stamped
        return None
    d = parse(doc)
    if d:
        d["ts"] = now
        engine.storage.set_setting(f"float:{sym}", json.dumps(d))
    return d


def stamp_of(d: dict[str, Any] | None) -> dict[str, Any] | None:
    """What an alert carries: {"float", "short", "fband", "sband"}."""
    if not d or not d.get("float"):
        return None
    return {
        "float": d["float"],
        "short": d.get("short"),
        "fband": fband(d["float"]),
        "sband": sband(d.get("short")),
    }


async def stamp_later(engine: Engine, alert_id: str, sym: str, kind: str | None) -> None:
    """For a stock not read this week: read it, then stamp the alert (its sheet and record get it)."""
    d = stamp_of(await fetch(engine, sym))
    if d:
        patch: dict[str, Any] = {"float": d}
        base = float_for(getattr(engine, "float_rates", None) or {}, kind, d)
        if base:
            patch["float_base"] = base
        engine.storage.update_alert_edge(alert_id, patch)


# --------------------------------------------------------------------------- the record


def float_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"f:band|kind" and "s:band|kind": {"key", "label", "n", "hit", "median", "run"}}, by the close."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, list[tuple[float, float | None]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        fl = e.get("float") or {}
        o = e.get("outcome") or {}
        if not fl or not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if not row.get("sign") or row.get("close") is None:
            continue
        its = row["sign"] * row["close"]
        run = (e.get("exc") or {}).get("mfe")
        c = e.get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            for axis, band in (("f", fl.get("fband")), ("s", fl.get("sband"))):
                if band:
                    pools.setdefault(f"{axis}:{band}|{kind}", []).append((its, run))
    out = {}
    for key, xs in pools.items():
        if len(xs) < MIN_N:
            continue
        axis_band, kind = key.split("|")
        axis, band = axis_band.split(":")
        who = "News" if kind == "*" else LABELS.get(kind, kind)
        lbl = f"{who} on {F_LABEL[band]}" if axis == "f" else f"{who} on a stock {S_LABEL[band]}"
        runs = [r for _, r in xs if r is not None]
        out[key] = {"key": key, "axis": axis, "band": band, "kind": kind, "label": lbl, "n": len(xs),
                    "hit": round(sum(x > 0 for x, _ in xs) / len(xs), 2), "median": round(st.median(x for x, _ in xs), 2),
                    "run": round(st.median(runs), 2) if len(runs) >= MIN_N else None}  # fmt: skip
    return out


def float_for(rates: dict[str, Any], kind: str | None, fl: dict[str, Any] | None) -> dict[str, Any] | None:
    """The record an alert carries: its kind on its float band, else any news on it; then the same for
    its short band. The float comes first: it moves news more often than the short does."""
    if not fl:
        return None
    for axis, band in (("f", fl.get("fband")), ("s", fl.get("sband"))):
        if not band:
            continue
        for k in (f"{axis}:{band}|{kind}" if kind else None, f"{axis}:{band}|*"):
            if k and k in rates:
                return rates[k]
    return None


def float_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    run = f", the winners' median run {r['run']:+.1f}%" if r.get("run") is not None else ""
    return f"{r['label']}: went its way {round(r['hit'] * 100)}% by the close (median {r['median']:+.1f}%{run}, {r['n']})"


def board(rates: dict[str, Any]) -> dict[str, Any]:
    """Every news alert by float band and by short band (any kind), for the Watch tab."""
    rows = {"float": [rates[f"f:{b}|*"] for b, *_ in FLOAT_BANDS if f"f:{b}|*" in rates],
            "short": [rates[f"s:{b}|*"] for b, *_ in SHORT_BANDS if f"s:{b}|*" in rates]}  # fmt: skip
    kinds = sorted((r for r in rates.values() if r["kind"] != "*"), key=lambda r: -r["n"])[:12]
    return {**rows, "kinds": kinds, "need": MIN_N}


def fmt_shares(n: float) -> str:
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K"
