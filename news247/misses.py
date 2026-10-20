"""Why it missed: a post-mortem for every graded alert that didn't go its way by the close.

A miss is either the signal failing or the signal getting swamped, and the two call for opposite
responses (drop the setup, or keep it and mind the tape). Each alert notes, when it goes out, the
market (SPY, see dossier.excess) and its sector: the sector's ETF when one is quoted (Semiconductors
read through SMH, Technology through XLK...), else the live tape's cap-weighted sector move. Both
are graded over the same window as the stock. A miss is then read in order:

- **Swamped by its sector:** the sector went 0.5%+ against the call and the stock did about as well
  as its sector or better (the news held up; the group didn't).
- **Swamped by the market:** the same, for SPY.
- **New news:** a later headline on the stock before the close.
- **Faded on its own:** none of the above: the market and the sector let it, and it still went
  the other way. That's the signal failing.

The record shows the mix per kind of news, so "contract wins miss 40% of the time" can be read as
"...and most of those were the sector". History, not a forecast.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
SWAMP = 0.5  # % against the call before the market or the sector counts as swamping it
CAUSES = {
    "sector": "Swamped by its sector",
    "market": "Swamped by the market",
    "news": "New news",
    "faded": "Faded on its own",
}
SECTOR_ETF = {
    "Technology": "XLK",
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Healthcare": "XLV",
    "Financials": "XLF",
    "Financial Services": "XLF",
    "Finance": "XLF",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Discretionary": "XLY",
    "Consumer Cyclical": "XLY",
    "Consumer Staples": "XLP",
    "Consumer Defensive": "XLP",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
}
INDUSTRY_ETF = {
    "Semiconductors": "SMH",
    "Software": "IGV",
    "Software - Application": "IGV",
    "Software - Infrastructure": "IGV",
    "Biotech": "XBI",
    "Biotechnology": "XBI",
    "Banks": "KBE",
    "Banks - Regional": "KRE",
    "Oil & Gas E&P": "XOP",
}


def sector_ref(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any] | None:
    """The sector to grade an alert against, as it stood when the alert went out: its ETF's price
    when one is quoted, else the tape's cap-weighted move for the sector so far today."""
    from .dossier import sectors

    now = now or time.time()
    li = engine.universe.get(sym) if sym else None
    if li is None or not li.sector:
        return None
    for etf in (INDUSTRY_ETF.get(li.industry or ""), SECTOR_ETF.get(li.sector)):
        if etf and etf != sym:
            q = engine.price_of(etf, 900, now=now)
            if q and q[1]:
                return {"sector": li.sector, "etf": etf, "px": q[1]}
    row = next((r for r in sectors(engine, now)["rows"] if r["sector"] == li.sector), None)
    if row is not None:
        return {"sector": li.sector, "chg": row["chg"]}
    return None


def grade_sector(
    engine: Engine, e: dict[str, Any], created: float, close_ts: float, now: float
) -> float | None:
    """The sector's move from the alert to its session's close (None when it can't be read)."""
    from .dossier import sectors

    sr = e.get("sector_ref") or {}
    if now < close_ts:
        return None
    if sr.get("etf"):
        q = engine.price_of(sr["etf"], now=now)
        if q and q[1] and q[0] >= created and abs(q[0] - close_ts) < 3 * 3600:
            return round((q[1] / float(sr["px"]) - 1) * 100, 2)
        return None
    if sr.get("chg") is None or now - close_ts > 3 * 3600:
        return None
    if datetime.fromtimestamp(created, ET).date() != datetime.fromtimestamp(close_ts, ET).date():
        return None  # the tape's day change only spans the alert's own session
    row = next(
        (r for r in sectors(engine, now, max_age_s=3 * 3600)["rows"] if r["sector"] == sr["sector"]), None
    )
    if row is None:
        return None
    return round(((1 + row["chg"] / 100) / (1 + sr["chg"] / 100) - 1) * 100, 2)


def post_mortem(engine: Engine, a: dict[str, Any]) -> dict[str, Any] | None:
    """Why a graded alert didn't go its way by the close, or None (not graded, or it did)."""
    from .dossier import session_closes

    e = a.get("edge") or {}
    out = e.get("outcome") or {}
    if not out:
        return None
    sym, row = next(iter(out.items()))
    sign = row.get("sign") or 0
    if not sign or row.get("close") is None or sign * row["close"] > 0:
        return None
    stock = round(sign * row["close"], 2)
    mc = (e.get("mkt_outcome") or {}).get("close")
    sc = (e.get("sector_outcome") or {}).get("close")
    mkt = round(sign * mc, 2) if mc is not None else None
    sec = round(sign * sc, 2) if sc is not None else None
    sec_name = (
        (e.get("sector_ref") or {}).get("etf") or (e.get("sector_ref") or {}).get("sector") or "its sector"
    )
    created = float(a.get("created") or 0)
    close_ts = session_closes(created, 1, getattr(engine.calendar, "holidays", set()))[0]
    title = str(a.get("title") or "")
    later = [
        i
        for i in engine.storage.items_for_ticker(sym, since=created + 600, limit=20)
        if (i.get("published") or i.get("detected") or 0) <= close_ts
        and i.get("title")
        and i["title"] != title
    ]
    later.sort(key=lambda i: i.get("published") or i.get("detected") or 0)
    res: dict[str, Any] = {"symbol": sym, "stock": stock, "market": mkt, "sector": sec, "sector_name": sec_name,
                           "later": [{"title": i["title"][:160], "source": i.get("source"), "url": i.get("url")} for i in later[:3]]}  # fmt: skip
    if sec is not None and sec <= -SWAMP and stock - sec >= -0.25:
        res["cause"] = "sector"
        res["text"] = (
            f"{sec_name} went {sec:+.1f}% against the call; {sym} did {'better' if stock > sec + 0.25 else 'about the same'} ({stock:+.1f}%). The news held up; the group didn't."
        )
    elif mkt is not None and mkt <= -SWAMP and stock - mkt >= -0.25:
        res["cause"] = "market"
        res["text"] = (
            f"The market (SPY) went {mkt:+.1f}% against the call; {sym} did {'better' if stock > mkt + 0.25 else 'about the same'} ({stock:+.1f}%)."
        )
    elif later:
        res["cause"] = "news"
        res["text"] = f"A later headline before the close: “{later[0]['title'][:120]}”"
    else:
        res["cause"] = "faded"
        ctx = [
            f"the market {mkt:+.1f}%" if mkt is not None else "",
            f"{sec_name} {sec:+.1f}%" if sec is not None else "",
        ]
        ctx_s = " and ".join(c for c in ctx if c)
        res["text"] = (
            f"{sym} went {stock:+.1f}% against the call"
            + (f" while {ctx_s} (in the call's direction)" if ctx_s else "")
            + ": the signal itself didn't hold."
        )
    res["label"] = CAUSES[res["cause"]]
    return res


def miss_report(
    engine: Engine, days: float = 60, now: float | None = None, limit: int = 600
) -> dict[str, Any]:
    """Every graded miss of the last ``days``, by cause, overall and per kind of news."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    total: dict[str, int] = dict.fromkeys(CAUSES, 0)
    kinds: dict[str, dict[str, Any]] = {}
    n = 0
    for a in engine.storage.recent_alerts(5000, since=now - days * 86400):
        if n >= limit:
            break
        pm = post_mortem(engine, a)
        if pm is None:
            continue
        n += 1
        total[pm["cause"]] += 1
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        if c:
            k = kinds.setdefault(
                c["key"],
                {
                    "key": c["key"],
                    "label": LABELS.get(c["key"], c["key"]),
                    "n": 0,
                    **dict.fromkeys(CAUSES, 0),
                },
            )
            k["n"] += 1
            k[pm["cause"]] += 1
    rows = sorted(kinds.values(), key=lambda r: -r["n"])
    for r in rows:
        r["failed"] = round(r["faded"] / r["n"], 2)  # the signal itself, not the tape
    return {"days": days, "n": n, "causes": total, "failed": round(total["faded"] / n, 2) if n else None,
            "kinds": rows[:15], "labels": CAUSES}  # fmt: skip
