"""What's at stake: each upcoming binary event with how both of its outcomes have traded here.

An FDA decision, a trial readout, a report: the stock moves on which way it goes, and the two
branches are rarely the same size. For every scheduled catalyst in the next two weeks (captured
from the headlines, see analysis/catalyst_dates.py) and every report on a phone's own stocks, this
lays out both branches from the graded record of that kind of news at the stock's size: "an
approval: +14% median by the close (9); a rejection or a negative panel: −38% (5)", beside what
options price for it and the phone's own exposure. The record is this server's alerts of that kind,
each graded from its own price. History, not a forecast of the outcome or its size.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
BRANCHES = {
    "pdufa": (("fda_approval", "an approval"), ("fda_negative", "a rejection or delay")),
    "adcom": (("fda_approval", "a positive vote"), ("fda_negative", "a negative vote")),
    "readout": (("trial_win", "the trial works"), ("trial_fail", "the trial fails")),
    "conference": (("trial_win", "good data"), ("trial_fail", "bad data")),
    "earnings": (("earnings_beat", "a beat"), ("earnings_miss", "a miss"), ("guidance_raise", "guidance up"), ("guidance_cut", "guidance down")),
}  # fmt: skip
MIN_N = 4


def _branch(rates: dict[str, Any], key: str, label: str, band: str) -> dict[str, Any]:
    """A branch's record at the stock's size, else at any size; signed as the stock moved (a
    rejection's record is its way for a short, so it's flipped to show the stock's move)."""
    from .catalysts import LABELS

    for b in (band, "*"):
        r = rates.get(f"{key}|{b}") or {}
        c = r.get("close")
        if c and c["n"] >= MIN_N:
            # records are its way; a bad-news kind's way is down: show the stock's own move
            sign = -1 if key in ("fda_negative", "trial_fail", "earnings_miss", "guidance_cut") else 1
            d1 = r.get("d1")
            return {"key": key, "label": label, "kind": LABELS.get(key, key), "n": c["n"], "size": r.get("band_label"),
                    "close": round(sign * c["median"], 2), "d1": round(sign * d1["median"], 2) if d1 else None,
                    "typical": r.get("typical")}  # fmt: skip
    return {"key": key, "label": label, "kind": LABELS.get(key, key), "n": 0}


def event_stakes(
    engine: Engine, device: str = "", now: float | None = None, days: int = 14
) -> dict[str, Any]:
    from .catalysts import size_band

    now = now or time.time()
    today = datetime.fromtimestamp(now, ET).date()
    end = today + timedelta(days=days)
    rates = getattr(engine, "catalyst_rates", None) or {}
    p = engine.desk.profiles.get(device) or {}
    holdings = p.get("holdings") or {}
    mine = set(p.get("watchlist") or []) | set(holdings)
    events: list[dict[str, Any]] = []
    for c in engine.storage.catalysts(today.isoformat()):
        if c.get("date", "") <= end.isoformat() and c.get("symbol") and c.get("event") in BRANCHES:
            events.append(
                {
                    "symbol": c["symbol"],
                    "date": c["date"],
                    "when": c.get("time") or "",
                    "event": c["event"],
                    "title": c.get("title") or "",
                }
            )
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    if book is not None:
        for sym in sorted(mine):
            e = book.lookup(sym, today)
            if e and 0 <= (e.get("in_days") if e.get("in_days") is not None else 99) <= days:
                events.append(
                    {
                        "symbol": sym,
                        "date": e["date"],
                        "when": e.get("when") or "",
                        "event": "earnings",
                        "title": f"{sym} reports",
                    }
                )
    rows = []
    for ev in events:
        sym = ev["symbol"]
        li = engine.universe.get(sym)
        band = size_band(li.market_cap if li else None)
        q = engine.price_of(sym, 86400, now=now)
        cached = (getattr(engine, "implied_cache", {}) or {}).get(sym)
        im = (cached[1] if cached else {}).get("rows") or []
        imp = next((r for r in im if r.get("earnings")), None) or (im[0] if im else None)
        h = holdings.get(sym) or {}
        value = round(h["shares"] * q[1], 2) if h.get("shares") and q else None
        try:
            in_days = (date.fromisoformat(ev["date"]) - today).days
        except ValueError:
            continue
        rows.append({**ev, "in_days": in_days, "mine": sym in mine, "price": q[1] if q else None,
                     "branches": [_branch(rates, k, label, band) for k, label in BRANCHES[ev["event"]]],
                     "implied_pct": imp["move_pct"] if imp else None, "value": value,
                     "tracked": sum(1 for t in p.get("tracked") or [] if t["symbol"] == sym)})  # fmt: skip
    rows.sort(key=lambda r: (r["date"], not r["mine"], r["symbol"]))
    return {"rows": rows, "days": days}
