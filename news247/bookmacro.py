"""Your book on market-wide news: what a tariff, Fed or oil headline has meant for what you hold.

Market-wide headlines are now graded by theme on a basket of sector ETFs (macrotape.py). A phone
with a portfolio holds stocks, not ETFs. Each holding is read through its industry's ETF (semis via
SMH, software via IGV, biotech via XBI, regional banks via KRE), else its sector's SPDR, else SPY.
When a market-wide headline goes out, the theme's record for each of those ETFs gives the book's
expected hour, in dollars and as a share of the book: "your book moved -$1,240 (-1.1%) in the hour
after headlines like it, by its sectors' records (SMH -2.0% for NVDA, AMD)".

Holders get one push per headline when the theme has a record (5+ graded) and the estimate is 0.3%
of the book or more. The sheet of any market-wide headline shows the estimate for this phone. An
estimate from sector records, not the stocks themselves: a holding moves with its sector, mostly.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_SHARE = 0.3  # % of the book: smaller estimates aren't worth a push


def etf_for(engine: Engine, sym: str) -> str:
    """The ETF a holding is read through: its industry's, else its sector's, else SPY."""
    from .misses import INDUSTRY_ETF, SECTOR_ETF

    li = engine.universe.get(sym) if getattr(engine, "universe", None) is not None else None
    if li is None:
        return "SPY"
    return INDUSTRY_ETF.get(li.industry) or SECTOR_ETF.get(li.sector) or "SPY"


def estimate(engine: Engine, device: str, theme: str, now: float | None = None) -> dict[str, Any] | None:
    """{"theme", "label", "n", "value", "dollars", "pct", "rows": [{symbol, etf, value, median, dollars}]}
    for this phone's holdings, from the theme's record; None without holdings or a record."""
    now = now or time.time()
    rec = (getattr(engine, "theme_rates", None) or {}).get(theme)
    p = engine.desk.profiles.get(device) or {}
    holdings = p.get("holdings") or {}
    if not rec or not holdings:
        return None
    rows = []
    for sym, h in holdings.items():
        q = engine.price_of(sym, 86400, now=now)
        price = q[1] if q and q[1] else h.get("cost")
        if not price or not h.get("shares"):
            continue
        etf = etf_for(engine, sym)
        r = (rec["etfs"].get(etf) or {}).get("h1") or ((rec["etfs"].get("SPY") or {}).get("h1"))
        used = etf if (rec["etfs"].get(etf) or {}).get("h1") else "SPY"
        value = price * h["shares"]
        if not r:
            continue
        rows.append({"symbol": sym, "etf": used, "value": round(value, 2), "median": r["median"], "n": r["n"],
                     "dollars": round(value * r["median"] / 100, 2)})  # fmt: skip
    if not rows:
        return None
    total = sum(r["value"] for r in rows)
    dollars = sum(r["dollars"] for r in rows)
    rows.sort(key=lambda r: abs(r["dollars"]), reverse=True)
    return {"theme": theme, "label": rec["label"], "n": rec["n"], "value": round(total, 2), "dollars": round(dollars, 2),
            "pct": round(dollars / total * 100, 2) if total else 0.0, "rows": rows}  # fmt: skip


def line(est: dict[str, Any]) -> str:
    by: dict[str, list[str]] = {}
    for r in est["rows"]:
        by.setdefault(f"{r['etf']} {r['median']:+.1f}%", []).append(r["symbol"])
    parts = [f"{k} for {', '.join(v[:3])}" for k, v in list(by.items())[:3]]
    sign = "-" if est["dollars"] < 0 else "+"
    return (f"Your book moved {sign}${abs(est['dollars']):,.0f} ({est['pct']:+.1f}%) in the hour after "
            f"{est['label'].lower()} headlines like it ({est['n']}), by its sectors' records: {'; '.join(parts)}.")  # fmt: skip


async def send_book(engine: Engine, alert: dict[str, Any], now: float | None = None) -> int:
    """A market-wide headline just went out: each phone with holdings gets its book's estimate, once."""
    from .macrotape import theme_of

    now = now or time.time()
    theme = theme_of(alert)
    if not theme or engine.webpush is None:
        return 0
    key = f"bookmacro:{datetime.fromtimestamp(now, ET).date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for dev, p in list(engine.desk.profiles.items()):
        if not p.get("holdings") or not (p.get("prefs") or {}).get("bookmacro", True):
            continue
        k = f"{dev}:{alert['id']}"
        if k in done:
            continue
        est = estimate(engine, dev, theme, now)
        if not est or abs(est["pct"]) < MIN_SHARE:
            continue
        done.add(k)
        payload = {"id": f"bm-{alert['id']}", "title": f"🌐 {est['label']} headline: your book {est['pct']:+.1f}% in the hour, by the record",
                   "body": f"{str(alert.get('title') or '')[:110]}\n{line(est)}"[:500], "severity": "MEDIUM", "kind": "system",
                   "tickers": [], "tag": f"bm-{theme}", "ts": now, "open": f"/app/#a={alert['id']}"}  # fmt: skip
        if await engine._to_device(dev, payload, urgency="normal"):
            sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent
