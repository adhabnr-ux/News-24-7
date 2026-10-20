"""The second headline graded: news on a stock that just had news.

A stock that made a move on a headline often gets another within days: a follow-up contract, a
second analyst, the 8-K behind the press release, a partner's version of the same deal. The desk
graded every headline as if it were the first. Now each alert knows whether it's a repeat: the same
stock had a directional alert in the 7 days before it (more than 30 minutes before, so one story
told by several outlets doesn't count). Per kind of news, from the record:

- **Firsts against repeats:** how often each went its way by the close, and the average.
- **Same kind or another:** a second contract win is a different animal from earnings after a
  contract win.
- **After a winner or a loser:** does a repeat on a stock whose first headline worked keep working,
  or is it priced in?

The alert's sheet says it ("🔁 The 2nd headline on ACME in 3 days. Repeat contract-win headlines
went their way 38% here (21); first ones 61%"), and the odds model learns it as an input. History,
not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

WINDOW = 7 * 86400
GAP = 1800  # one story told by several outlets: not a second headline
MIN_FIRST = 8
MIN_REPEAT = 5


def _its_way(a: dict[str, Any]) -> float | None:
    o = (a.get("edge") or {}).get("outcome") or {}
    row = next(iter(o.values()), None) if o else None
    if not row or not row.get("sign") or row.get("close") is None:
        return None
    return row["sign"] * row["close"]


def _sym(a: dict[str, Any]) -> str:
    o = (a.get("edge") or {}).get("outcome") or {}
    return next(iter(o), None) or ((a.get("tickers") or [""])[0])


def previous(engine: Engine, a: dict[str, Any]) -> dict[str, Any] | None:
    """The newest directional alert on the same stock in the 7 days before this one (30+ minutes earlier)."""
    from .dossier import _direction

    sym = _sym(a)
    if not sym:
        return None
    t = float(a.get("created") or time.time())
    for b in engine.storage.alerts_for_ticker(sym, since=t - WINDOW, limit=40):
        if b.get("id") == a.get("id") or b.get("kind") == "system" or not _direction(b):
            continue
        if t - GAP >= float(b["created"]) >= t - WINDOW:
            return b
    return None


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "avg": round(sum(xs) / len(xs), 2),
    }


def repeat_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{kind: {"kind", "label", "first", "repeat", "same", "other", "after_win", "after_loss"}} (each
    {"n", "hit", "avg"}), for kinds with 8+ firsts and 5+ repeats graded."""
    from .catalysts import LABELS, classify
    from .dossier import _direction

    now = now or time.time()
    since = now - days * 86400
    alerts = [
        a for a in engine.storage.recent_alerts(8000, since=since - WINDOW) if a.get("kind") != "system"
    ]
    alerts.sort(key=lambda a: a["created"])
    last: dict[str, dict[str, Any]] = {}
    pools: dict[str, dict[str, list[float]]] = {}
    for a in alerts:
        if not _direction(a):
            continue
        sym = _sym(a)
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        kind = c["key"] if c else None
        prev = last.get(sym)
        if prev and a["created"] - prev["created"] < GAP:
            continue  # the same story from another outlet: neither first nor repeat
        last[sym] = {"created": a["created"], "kind": kind, "way": _its_way(a)}
        x = _its_way(a)
        if x is None or a["created"] < since:
            continue
        rep = prev is not None and a["created"] - prev["created"] <= WINDOW
        for k in ("*", kind) if kind else ("*",):
            g = pools.setdefault(k, {})
            g.setdefault("repeat" if rep else "first", []).append(x)
            if rep:
                g.setdefault("same" if prev["kind"] == kind else "other", []).append(x)
                if prev["way"] is not None:
                    g.setdefault("after_win" if prev["way"] > 0 else "after_loss", []).append(x)
    out = {}
    for k, g in pools.items():
        if len(g.get("first", [])) < MIN_FIRST or len(g.get("repeat", [])) < MIN_REPEAT:
            continue
        out[k] = {"kind": k, "label": "Any news" if k == "*" else LABELS.get(k, k),
                  **{b: _stats(g.get(b, [])) for b in ("first", "repeat", "same", "other", "after_win", "after_loss")}}  # fmt: skip
    return out


def stamp(engine: Engine, a: dict[str, Any]) -> dict[str, Any] | None:
    """What the alert's sheet and the odds need: the headline before it on this stock, and the record."""
    from .catalysts import classify

    prev = previous(engine, a)
    if not prev:
        return None
    c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
    pc = (prev.get("edge") or {}).get("catalyst") or classify(prev, engine.universe)
    kind = c["key"] if c else None
    sym = _sym(a)
    times = sorted(
        float(b["created"])
        for b in engine.storage.alerts_for_ticker(sym, since=float(a["created"]) - WINDOW, limit=40)
        if b.get("id") != a.get("id")
        and b.get("kind") != "system"
        and float(b["created"]) <= float(a["created"]) - GAP
    )
    nth, last = 1, None
    for t0 in times:  # one story from several outlets counts once
        if last is None or t0 - last > GAP:
            nth += 1
        last = t0
    out: dict[str, Any] = {"prev_id": prev["id"], "prev_title": (prev.get("title") or "")[:140], "symbol": sym,
                           "hours": round((float(a["created"]) - float(prev["created"])) / 3600, 1), "nth": nth,
                           "same": bool(kind) and pc is not None and pc["key"] == kind}  # fmt: skip
    rates = getattr(engine, "repeat_rates", None) or {}
    r = rates.get(kind or "") or rates.get("*")
    if r:
        out["rec"] = {
            k: r[k] for k in ("kind", "label", "first", "repeat", "same", "other") if r.get(k) is not None
        }
    out["line"] = line(out)
    return out


def line(rp: dict[str, Any] | None) -> str:
    if not rp:
        return ""
    when = f"{rp['hours']:.0f} hours" if rp["hours"] < 48 else f"{rp['hours'] / 24:.0f} days"
    nth = {2: "2nd", 3: "3rd"}.get(rp["nth"], f"{rp['nth']}th")
    head = f"The {nth} headline on {rp['symbol']} in {when}"
    r = rp.get("rec") or {}
    if r.get("repeat") and r.get("first"):
        what = "news" if r["kind"] == "*" else f"{r['label'].lower()} headlines"
        head += (f". Repeat {what} went their way {round(r['repeat']['hit'] * 100)}% here ({r['repeat']['n']}); "
                 f"first ones {round(r['first']['hit'] * 100)}% ({r['first']['n']})")  # fmt: skip
    return head + "."
