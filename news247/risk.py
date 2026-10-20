"""Your risk, in one place: what the whole paper book could lose, where it clusters, and what's coming.

Each trade can be sensible while the book isn't. For a phone's open paper calls this reads, from
the live price: how much each would lose from here if its stop were hit (in dollars when the call
has a size, else at the phone's own risk per idea, sized from its original stop), the total as a
share of the account, the calls with no stop at all, the stocks clustered on the same side of the
same sector across holdings and calls, what a 3% fall in each sector the book leans on would cost,
and the reports and events landing on its positions this week. A cap (2-8% of the account) flags
the book when open risk runs past it. Paper only: the system never trades.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
SHOCK = 3.0  # % a sector falls in the scenario


def _account(engine: Engine, device: str, p: dict[str, Any], now: float) -> float:
    acct = float((p.get("prefs") or {}).get("account") or 0)
    if not acct and p.get("holdings"):
        from .dossier import portfolio

        acct = portfolio(engine, device, now).get("value") or 0.0
    return acct


def desk_risk(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    prefs = p.get("prefs") or {}
    account = _account(engine, device, p, now)
    per_idea = float(prefs.get("risk_pct") or 1.0)
    calls, unbounded = [], []
    exposure: dict[str, float] = {}  # sector -> net long dollars (shorts negative)
    sides: dict[tuple[str, str], list[str]] = {}
    for t in p.get("tracked") or []:
        q = engine.price_of(t["symbol"], 6 * 3600, now=now)
        price = q[1] if q else None
        long = t.get("direction", "long") == "long"
        sign = 1 if long else -1
        li = engine.universe.get(t["symbol"])
        sector = (li.sector if li else "") or "Other"
        sides.setdefault((sector, "long" if long else "short"), []).append(t["symbol"])
        if t.get("stop") is None or price is None:
            if t.get("stop") is None:
                unbounded.append(t["symbol"])
            continue
        from_here = max(0.0, sign * (price - t["stop"]) / price * 100)  # % lost from here if stopped
        orig = t.get("risk_stop") or t["stop"]
        dist0 = sign * (t["entry"] - orig) / t["entry"] * 100
        if t.get("shares"):
            value = t["shares"] * price
        elif account and dist0 > 0:
            value = (
                account * per_idea / 100 / (dist0 / 100)
            )  # the size risk-per-idea gives at the original stop
        else:
            value = None
        usd = round(value * from_here / 100, 2) if value is not None else None
        if value is not None:
            exposure[sector] = exposure.get(sector, 0.0) + sign * value
        calls.append({"id": t["id"], "symbol": t["symbol"], "direction": "long" if long else "short", "auto": bool(t.get("auto")),
                      "price": price, "stop": t["stop"], "from_here": round(from_here, 2), "risk_usd": usd,
                      "sized": "shares" if t.get("shares") else "risk per idea" if value is not None else None})  # fmt: skip
    for sym, h in (p.get("holdings") or {}).items():
        q = engine.price_of(sym, 6 * 3600, now=now)
        if not q or not h.get("shares"):
            continue
        li = engine.universe.get(sym)
        sector = (li.sector if li else "") or "Other"
        exposure[sector] = exposure.get(sector, 0.0) + h["shares"] * q[1]
        sides.setdefault((sector, "long"), []).append(sym)
    total = round(sum(c["risk_usd"] or 0 for c in calls), 2)
    pct = round(total / account * 100, 2) if account else None
    cap = float(prefs.get("risk_cap") or 0)
    clusters = sorted(({"sector": s, "side": side, "symbols": sorted(set(v))} for (s, side), v in sides.items() if len(set(v)) >= 2),
                      key=lambda c: -len(c["symbols"]))  # fmt: skip
    shocks = sorted(({"sector": s, "net": round(v, 2), "cost": round(-abs(v) * SHOCK / 100 if v > 0 else abs(v) * SHOCK / 100, 2)}
                     for s, v in exposure.items() if abs(v) >= 1), key=lambda r: r["cost"])[:4]  # fmt: skip
    events = _events(engine, device, p, now)
    return {"account": round(account, 2) if account else None, "per_idea": per_idea, "calls": calls, "total_usd": total,
            "total_pct": pct, "unbounded": unbounded, "clusters": clusters, "shock": SHOCK, "shocks": shocks, "events": events,
            "cap": cap or None, "over": bool(cap and pct is not None and pct > cap)}  # fmt: skip


def _events(engine: Engine, device: str, p: dict[str, Any], now: float) -> list[dict[str, Any]]:
    from .plans import event_risk

    out = [{"symbol": e["symbol"], "title": e["title"], "date": e["date"], "in_days": e["in_days"], "on": "holding"}
           for e in event_risk(engine, device, now, days=7)["events"]]  # fmt: skip
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    held = {e["symbol"] for e in out}
    if book is not None:
        today = datetime.fromtimestamp(now, ET).date()
        for sym in sorted({t["symbol"] for t in p.get("tracked") or []} - held):
            e = book.lookup(sym, today)
            if e and 0 <= (e.get("in_days") if e.get("in_days") is not None else 99) <= 7:
                out.append(
                    {
                        "symbol": sym,
                        "title": f"{sym} reports",
                        "date": e["date"],
                        "in_days": e["in_days"],
                        "on": "open call",
                    }
                )
    return sorted(out, key=lambda e: e["date"])
