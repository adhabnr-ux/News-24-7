"""Into the open: news that lands outside the session, and what the stock did after the bell.

Most big catalysts land before 9:30 or after 16:00. By the open the stock has already gapped, and
the question is whether to buy the open or wait for the fade. Each alert from outside the session
is now also stamped at the session's first print after 9:30 (dossier.record_outcomes: "open"),
beside its close, so it splits into two moves, both its way: **the gap** (from its price when the
news landed to the open) and **after the open** (from the open to the close).

Pooled per kind of news and gap size (against it, flat, under 2%, 2-5%, 5%+ its way), the record
answers it: "contract wins that gapped 5%+ kept going after the open 36% of the time (median −1.2%
open to close, 22); 41% gave the whole gap back by the close". The most specific record with 6+
graded is used, else the same gap at any kind of news, else the kind at any gap. Before the open the
gap is read live (the stock's pre-market move since the alert), so the 9:20 open push, the gap board,
the sheet and the pre-trade check carry the row it falls in. History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 6
GAPS = (("against", "gapped against it"), ("flat", "barely gapped"), ("small", "gapped under 2% its way"),
        ("mid", "gapped 2-5% its way"), ("big", "gapped 5%+ its way"))  # fmt: skip
GAP_LABEL = dict(GAPS)


def gap_bin(g: float) -> str:
    return "against" if g <= -0.5 else "flat" if g < 0.5 else "small" if g < 2 else "mid" if g < 5 else "big"


def split(row: dict[str, Any]) -> tuple[float, float, float] | None:
    """(gap, after the open, the whole move to the close), each its way, from an outcome row."""
    if not row.get("sign") or row.get("open") is None or row.get("close") is None:
        return None
    s = row["sign"]
    gap = s * row["open"]
    after = s * ((1 + row["close"] / 100) / (1 + row["open"] / 100) - 1) * 100
    return round(gap, 2), round(after, 2), round(s * row["close"], 2)


def open_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"kind|gap": {"key", "kind", "gap", "label", "n", "gap_median", "after": stats, "filled"}} for
    every kind of news ("*" for any) and gap size ("*" for any)."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        sp = split(next(iter(o.values())))
        if sp is None:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        b = gap_bin(sp[0])
        for kind in ("*", c["key"]) if c else ("*",):
            for g in (b, "*"):
                pools.setdefault((kind, g), []).append(sp)
    out = {}
    for (kind, g), xs in pools.items():
        if len(xs) < MIN_N:
            continue
        afters = [x[1] for x in xs]
        r = {"key": f"{kind}|{g}", "kind": kind, "gap": g, "n": len(xs),
             "label": ("News" if kind == "*" else LABELS.get(kind, kind)) + (f" that {GAP_LABEL[g]}" if g != "*" else ", any gap"),
             "gap_median": round(st.median(x[0] for x in xs), 2),
             "after": {"n": len(xs), "hit": round(sum(x > 0 for x in afters) / len(xs), 2), "median": round(st.median(afters), 2)}}  # fmt: skip
        gapped = [x for x in xs if x[0] >= 2]
        if len(gapped) >= MIN_N:  # gave the whole gap back: at or past the news price by the close
            r["filled"] = round(sum(x[2] <= 0 for x in gapped) / len(gapped), 2)
        out[r["key"]] = r
    return out


def open_for(rates: dict[str, Any], kind: str | None, gap: float) -> dict[str, Any] | None:
    """The most specific record for this kind of news at this gap."""
    b = gap_bin(gap)
    for k in (f"{kind}|{b}" if kind else None, f"*|{b}", f"{kind}|*" if kind else None):
        if k and k in rates:
            return rates[k]
    return None


def open_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    a = r["after"]
    filled = f"; {round(r['filled'] * 100)}% gave the whole gap back" if r.get("filled") is not None else ""
    return f"{r['label']}: kept going after the open {round(a['hit'] * 100)}% (median {a['median']:+.1f}% open to close, {a['n']}){filled}"


def outside(ts: float, holidays: set[str] | None = None) -> bool:
    """News from outside the regular session (before 9:30, after 16:00, weekends and holidays)."""
    dt = datetime.fromtimestamp(ts, ET)
    if dt.weekday() >= 5 or dt.date().isoformat() in (holidays or set()):
        return True
    return not ((9, 30) <= (dt.hour, dt.minute) < (16, 0))


def premarket(engine: Engine, now: float | None = None, device: str = "") -> dict[str, Any]:
    """Before the open (and in its first half hour): each alert since the last close on a stock that
    has gapped, its gap so far, and the record for that gap. With a device, its stocks first."""
    from .catalysts import classify
    from .dossier import _direction, _since

    now = now or time.time()
    rates = getattr(engine, "open_rates", None) or {}
    dt = datetime.fromtimestamp(now, ET)
    holidays = getattr(engine.calendar, "holidays", set())
    p = engine.desk.profiles.get(device) or {}
    mine = (
        set(p.get("watchlist") or [])
        | set(p.get("holdings") or {})
        | {t["symbol"] for t in p.get("tracked") or []}
    )
    since = now - 20 * 3600
    rows, seen = [], set()
    for a in engine.storage.recent_alerts(400, since=since):
        if a.get("kind") == "system" or not a.get("tickers") or not outside(float(a["created"]), holidays):
            continue
        sym = a["tickers"][0]
        sign = {"up": 1, "down": -1}.get(_direction(a), 0)
        if not sign or sym in seen:
            continue
        mv = _since(a, sym, lambda x: engine.price_of(x, 1800, now=now))
        if mv is None:
            continue
        seen.add(sym)
        gap = round(sign * mv, 2)
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        r = open_for(rates, c["key"] if c else None, gap)
        rows.append({"alert_id": a["id"], "symbol": sym, "title": a.get("title", ""), "created": a["created"],
                     "direction": _direction(a), "gap": gap, "bin": gap_bin(gap), "bin_label": GAP_LABEL[gap_bin(gap)],
                     "kind": c["label"] if c else None, "mine": sym in mine, "record": r, "line": open_line(r)})  # fmt: skip
    rows.sort(key=lambda r: (not r["mine"], -abs(r["gap"])))
    phase = (
        "before" if (dt.hour, dt.minute) < (9, 30) else "open" if (dt.hour, dt.minute) < (10, 0) else "after"
    )
    return {"rows": rows[:20], "phase": phase, "need": MIN_N}


def open_board(rates: dict[str, Any]) -> dict[str, Any]:
    """The record by gap size for any news, then each kind with enough graded at some gap."""
    order = [g for g, _ in GAPS]
    anyk = [rates[f"*|{g}"] for g in order if f"*|{g}" in rates]
    kinds: dict[str, list[dict[str, Any]]] = {}
    for r in rates.values():
        if r["kind"] != "*" and r["gap"] != "*":
            kinds.setdefault(r["kind"], []).append(r)
    rows = [
        {
            "kind": k,
            "label": rs[0]["label"].split(" that ")[0],
            "gaps": sorted(rs, key=lambda r: order.index(r["gap"])),
        }
        for k, rs in sorted(kinds.items(), key=lambda kv: -sum(r["n"] for r in kv[1]))
    ]
    return {"any": anyk, "kinds": rows, "all": rates.get("*|*"), "need": MIN_N}
