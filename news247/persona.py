"""Stock personality: how each stock has taken its own news here.

Records so far pool alerts by kind of news. Stocks differ: one runs with every headline for days,
another pops in the first hour and gives it all back by the close, a third barely notices. Every
graded alert on a stock (an hour later, at the close, the next day, five days on, each its way from
its own reference price) pools into that stock's profile, and the shape of the medians names it:

- **Barely reacts:** its news moves (the larger of the first hour and the close) are under 40% of
  a typical alert's.
- **Fights its news:** most of its alerts went the other way by the close.
- **Fades by the close:** the first hour's move is mostly gone by the close.
- **Fades over days:** what it did on the day is mostly gone five sessions on.
- **Keeps drifting:** five days on, it's gone well past where it closed on the day.
- **Runs with its news:** most of its alerts went their way by the close, and held.
- **Mixed:** none of the above stands out.

A stock needs 6 graded alerts before it has a profile. The stock's page, the push, the sheet and the
pre-trade check carry it; Watch lists the phone's stocks. History, not a forecast: a handful of
alerts on one stock is a small sample, and the counts are always shown.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 6  # graded closes on a stock before it has a profile
MIN_LATER = 4  # graded day-5 (or hour) moves before a trait reads them
HORIZONS = ("h1", "close", "d1", "d5", "d20")
TRAITS = {
    "quiet": ("Barely reacts", "its news moves are small next to a typical alert's"),
    "fights": ("Fights its news", "most of its alerts went the other way by the close"),
    "fades_day": ("Fades by the close", "its first-hour moves mostly didn't last the day"),
    "fades_days": ("Fades over days", "what it did on the day was mostly gone five sessions on"),
    "drifts": ("Keeps drifting", "its news moves kept going for days after the close"),
    "runs": ("Runs with its news", "most of its alerts went their way by the close, and held"),
    "mixed": ("Mixed", "no one pattern stands out yet"),
}
TONE = {"quiet": "info", "fights": "bad", "fades_day": "warn", "fades_days": "warn", "drifts": "good",
        "runs": "good", "mixed": "info"}  # fmt: skip


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def _graded(a: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """(its stock, its outcome row) for a graded, directional alert."""
    o = (a.get("edge") or {}).get("outcome") or {}
    if not o or a.get("kind") == "system":
        return None
    sym, row = next(iter(o.items()))
    if not row.get("sign") or row.get("close") is None:
        return None
    return sym, row


def trait(p: dict[str, Any], typical: float | None) -> str:
    """The name for a profile's shape (one of TRAITS), from its medians, its way."""
    c, h1, d5 = p.get("close"), p.get("h1"), p.get("d5")
    if not c or c["n"] < MIN_N:
        return "mixed"
    if typical and p.get("size") is not None and p["size"] < 0.4 * typical:
        return "quiet"
    if c["hit"] <= 0.35 and c["median"] < 0:
        return "fights"
    if h1 and h1["n"] >= MIN_LATER and h1["median"] >= 0.3 and c["median"] < 0.5 * h1["median"]:
        return "fades_day"
    if d5 and d5["n"] >= MIN_LATER:
        peak = max(c["median"], h1["median"] if h1 and h1["n"] >= MIN_LATER else 0.0)
        if peak >= 0.3 and d5["median"] < 0.4 * peak:
            return "fades_days"
        if d5["median"] > 0 and d5["median"] >= c["median"] + max(0.5, 0.5 * abs(c["median"])):
            return "drifts"
    if c["hit"] >= 0.6 and c["median"] > 0:
        return "runs"
    return "mixed"


def persona_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{symbol: {"symbol", "n", "h1", "close", "d1", "d5", "size", "ratio", "trait"}, "_typical": x}
    for every stock with MIN_N graded closes; ``size`` is its median news move (the larger of the
    first hour and the close, either way) and ``_typical`` the same over every graded alert."""
    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    sizes_all: list[float] = []
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        g = _graded(a)
        if g is None:
            continue
        sym, row = g
        pool = pools.setdefault(sym, {h: [] for h in (*HORIZONS, "size")})
        for h in HORIZONS:
            if row.get(h) is not None:
                pool[h].append(row["sign"] * row[h])
        # how big its news moves are: the larger of the first hour and the close, either way
        size = max(abs(row["close"]), abs(row.get("h1") or 0.0))
        pool["size"].append(size)
        sizes_all.append(size)
    typical = round(st.median(sizes_all), 2) if sizes_all else None
    out: dict[str, Any] = {"_typical": typical}
    for sym, pool in pools.items():
        if len(pool["close"]) < MIN_N:
            continue
        p: dict[str, Any] = {"symbol": sym, "n": len(pool["close"])}
        p.update({h: _stats(pool[h]) for h in HORIZONS})
        p["size"] = round(st.median(pool["size"]), 2)
        p["ratio"] = round(p["size"] / typical, 1) if typical else None
        p["trait"] = trait(p, typical)
        out[sym] = p
    return out


def persona_for(rates: dict[str, Any], sym: str) -> dict[str, Any] | None:
    """A stock's profile with its trait's words, or None without enough graded."""
    p = rates.get((sym or "").upper())
    if not p:
        return None
    label, why = TRAITS[p["trait"]]
    return {**p, "label": label, "why": why, "tone": TONE[p["trait"]]}


def stamp(rates: dict[str, Any], sym: str) -> dict[str, Any] | None:
    """The compact profile an alert carries (edge.persona): enough for the push and the sheet."""
    p = persona_for(rates, sym)
    if not p:
        return None
    path = {
        h: p[h]["median"]
        for h in HORIZONS
        if p.get(h) and p[h]["n"] >= (MIN_N if h == "close" else MIN_LATER)
    }
    return {"symbol": p["symbol"], "trait": p["trait"], "label": p["label"], "n": p["n"],
            "hit": p["close"]["hit"], "path": path, "ratio": p.get("ratio")}  # fmt: skip


WORDS = {
    "h1": "in an hour",
    "close": "by the close",
    "d1": "the next day",
    "d5": "five days on",
    "d20": "a month on",
}


def persona_line(p: dict[str, Any] | None) -> str:
    """'ACME and its news: fades by the close (+1.4% in an hour, +0.2% by the close; median, its way, 9 graded)'."""
    if not p:
        return ""
    path = p.get("path") or {}
    bits = [f"{path[h]:+.1f}% {WORDS[h]}" for h in ("h1", "close", "d5") if h in path]
    tail = f" ({', '.join(bits)}; median, its way, {p['n']} graded)" if bits else f" ({p['n']} graded)"
    return f"{p['symbol']} and its news: {p['label'].lower()}{tail}"


def persona(engine: Engine, sym: str, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """The full profile for a stock's page: its trait, its path, its record by kind of news and
    against the same kind of news elsewhere, and its latest graded alerts."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    sym = (sym or "").upper()
    rates = getattr(engine, "persona_rates", None) or {}
    cat_rates = getattr(engine, "catalyst_rates", None) or {}
    kinds: dict[str, list[float]] = {}
    vs: list[float] = []
    recent = []
    n = 0
    for a in engine.storage.alerts_for_ticker(sym, since=now - days * 86400, limit=400):
        g = _graded(a)
        if g is None or g[0] != sym:
            continue
        _, row = g
        n += 1
        its = row["sign"] * row["close"]
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        key = c["key"] if c else "other"
        kinds.setdefault(key, []).append(its)
        if c:
            base = cat_rates.get(f"{c['key']}|{c.get('band') or '*'}") or cat_rates.get(f"{c['key']}|*") or {}
            if (base.get("close") or {}).get("n", 0) >= 8:
                vs.append(its - base["close"]["median"])
        if len(recent) < 8:
            recent.append({"id": a["id"], "title": a.get("title", ""), "created": a.get("created"),
                           **{h: round(row["sign"] * row[h], 2) for h in HORIZONS if row.get(h) is not None}})  # fmt: skip
    out: dict[str, Any] = {"symbol": sym, "graded": n, "need": MIN_N, "profile": persona_for(rates, sym),
                           "typical": rates.get("_typical"), "recent": recent}  # fmt: skip
    out["kinds"] = sorted(
        ({"key": k, "label": LABELS.get(k, "Other news" if k == "other" else k), **(_stats(xs) or {})}
         for k, xs in kinds.items()),
        key=lambda r: -r["n"],
    )  # fmt: skip
    if len(vs) >= 4:
        out["vs_kind"] = {"n": len(vs), "median": round(st.median(vs), 2)}
    return out


def board(engine: Engine, device: str = "") -> dict[str, Any]:
    """The phone's own stocks (watched, held, with an open call) that have a profile, and the
    clearest runners and faders among every other stock here."""
    rates = getattr(engine, "persona_rates", None) or {}
    p = engine.desk.profiles.get(device) or {}
    mine = (set(p.get("watchlist") or []) | set(p.get("holdings") or {})
            | {t["symbol"] for t in p.get("tracked") or []})  # fmt: skip
    rows = [persona_for(rates, s) for s in sorted(mine)]
    everyone = [persona_for(rates, s) for s in rates if not s.startswith("_") and s not in mine]

    def pick(traits: tuple[str, ...]) -> list[dict[str, Any]]:
        xs = [x for x in everyone if x and x["trait"] in traits]
        return sorted(xs, key=lambda x: -x["n"])[:6]

    return {"mine": [r for r in rows if r], "unknown": sorted(s for s in mine if not rates.get(s)),
            "runners": pick(("runs", "drifts")), "faders": pick(("fades_day", "fades_days", "fights")),
            "typical": rates.get("_typical"), "need": MIN_N}  # fmt: skip
