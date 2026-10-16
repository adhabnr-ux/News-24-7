"""Views built from everything the engine already knows, for the app.

* **Ticker dossier** (``/api/ticker/SYM``): one screen for one stock. The company, the live price
  and today's range, why it is moving (the freshest catalysts), the evidence tally (bullish vs
  bearish signals: headlines, options bought on one side, insider purchases, the tape), every
  alert on it with what the stock did since, insider buys, options, the radar and ignition, and
  what is on its calendar.
* **Track record** (``/api/record``): how each kind of alert did afterwards. Every alert with a
  reference price gets its move one hour later and at that session's close recorded (in the
  alert's direction); this aggregates them by kind.
* **Smart money** (``/api/smart``): options flow, insider purchases and ignitions in one feed.
* **Market map** (``/api/sectors``): today's move by sector from the live tape, cap-weighted,
  with each sector's leaders and laggards.
* **Search** (``/api/search``): alerts, headlines and companies.
"""

from __future__ import annotations

import statistics as st
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .desk import alert_category, clean_symbol

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
SEV_W = {"CRITICAL": 3.0, "HIGH": 2.0, "MEDIUM": 1.0, "LOW": 0.5}


def _direction(a: dict[str, Any]) -> str:
    e = a.get("edge") or {}
    d = (e.get("play") or {}).get("direction") or (a.get("analysis") or {}).get("direction") or ""
    if not d:
        d = (e.get("ignition") or {}).get("direction") or (e.get("smallcap") or {}).get("direction") or ""
    if not d and (a.get("move") or {}).get("pct") is not None:
        d = "up" if a["move"]["pct"] > 0 else "down"
    return d if d in ("up", "down") else ""


def _since(a: dict[str, Any], sym: str, price_of: Any) -> float | None:
    """The stock's move since the alert, from the alert's own reference price."""
    refs = (a.get("edge") or {}).get("refs") or {}
    ref = refs.get(sym)
    if ref is None:
        mv = a.get("move") or {}
        if mv.get("symbol") == sym:
            ref = mv.get("price")
    q = price_of(sym)
    if not ref or not q or not q[1]:
        return None
    return round((q[1] / float(ref) - 1.0) * 100.0, 2)


# --------------------------------------------------------------------------- the tally


def tally(signals: list[dict[str, Any]]) -> dict[str, Any]:
    bull = sum(s["weight"] for s in signals if s["side"] == "up")
    bear = sum(s["weight"] for s in signals if s["side"] == "down")
    total = bull + bear
    if total == 0:
        lean = "none"
    elif bull >= 2 * bear and bull - bear >= 1.5:
        lean = "bullish"
    elif bear >= 2 * bull and bear - bull >= 1.5:
        lean = "bearish"
    else:
        lean = "mixed"
    return {
        "bull": round(bull, 1),
        "bear": round(bear, 1),
        "lean": lean,
        "share_bull": round(bull / total, 2) if total else None,
        "signals": sorted(signals, key=lambda s: -s["ts"])[:14],
    }


def ticker_signals(
    engine: Engine, sym: str, alerts: list[dict[str, Any]], items: list[dict[str, Any]], now: float
) -> list[dict[str, Any]]:
    """Each piece of evidence with its side and weight: fresh headlines (3 days, by severity and
    score), options bought on one side today, insider purchases (90 days; opportunistic counts
    more, routine and planned less), and what the tape is doing (an ignition, the radar)."""
    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for a in alerts:
        if now - a.get("created", 0) > 3 * 86400 or a.get("kind") not in ("news", "options", "price"):
            continue
        d = _direction(a)
        if not d:
            continue
        cat = alert_category(a)
        w = SEV_W.get(a.get("severity", "LOW"), 0.5)
        if cat == "insider":
            continue  # counted below with its own weight
        if cat == "options":
            w = 2.0
        out.append(
            {
                "side": d,
                "weight": w,
                "kind": cat,
                "label": a.get("title", "")[:140],
                "ts": a.get("created", 0),
                "id": a.get("id"),
            }
        )
        seen_titles.add(a.get("title", ""))
    for it in items:
        if now - it.get("detected", 0) > 3 * 86400 or it.get("title") in seen_titles:
            continue
        if it.get("source") == "sec-insider-buys":
            continue  # counted below with its own weight
        d = it.get("direction")
        if d not in ("up", "down") or (it.get("score") or 0) < 25:
            continue
        out.append(
            {
                "side": d,
                "weight": 0.5,
                "kind": "news",
                "label": it["title"][:140],
                "ts": it.get("detected", 0),
            }
        )
    for it in engine.storage.items_for_ticker(sym, since=now - 90 * 86400, limit=10):
        if it.get("source") != "sec-insider-buys":
            continue
        t = it.get("title", "")
        w = 3.0 if "opportunistic" in t else 0.5 if ("routine" in t or "10b5-1" in t) else 2.0
        out.append(
            {"side": "up", "weight": w, "kind": "insider", "label": t[:140], "ts": it.get("detected", 0)}
        )
    seen = engine.options_seen.get(sym) if hasattr(engine, "options_seen") else None
    if seen and now - seen[0] < 6.5 * 3600:
        out.append({"side": seen[1], "weight": 2.0, "kind": "options", "label": seen[2], "ts": seen[0]})
    row = (engine.radar.board.get(sym) if engine.radar is not None else None) or {}
    ign = row.get("ignition")
    if ign and now - (ign.get("detected") or 0) < 6.5 * 3600:
        out.append({"side": ign["direction"], "weight": 1.5 if ign.get("primed") else 1.0, "kind": "tape",
                    "label": f"{'Ignition' if ign['kind'] == 'ignition' else 'Build-up'}: {ign['move_pct']:+.1f}% on {ign.get('burst') or ign.get('rvol') or '?'}× volume",
                    "ts": ign["detected"]})  # fmt: skip
    return out


# --------------------------------------------------------------------------- the dossier


def ticker_dossier(engine: Engine, raw: str, now: float | None = None) -> dict[str, Any] | None:
    sym = clean_symbol(raw)
    if not sym:
        return None
    now = now or time.time()
    li = engine.universe.get(sym)
    q = engine.price_of(sym)
    path = engine.live_quotes.path(sym, now)
    hist = list(engine.detector.history.get(sym, ()))[-240:] if sym in engine.detector.history else []
    if path is not None and len(path.hist) > len(hist):
        hist = list(path.hist)
    alerts = engine.storage.alerts_for_ticker(sym, since=now - 30 * 86400, limit=40)
    items = engine.storage.items_for_ticker(sym, since=now - 7 * 86400, limit=40)
    rows = []
    for a in alerts:
        rows.append(
            {
                "id": a["id"],
                "title": a["title"],
                "severity": a["severity"],
                "kind": alert_category(a),
                "created": a["created"],
                "direction": _direction(a),
                "since": _since(a, sym, engine.price_of),
                "outcome": ((a.get("edge") or {}).get("outcome") or {}).get(sym),
            }
        )
    insider = [
        {"title": it["title"], "detected": it["detected"], "url": it.get("url")}
        for it in engine.storage.items_for_ticker(sym, since=now - 180 * 86400, limit=12)
        if it.get("source") == "sec-insider-buys"
    ]
    news = [
        {"title": it["title"], "source": it["source"], "url": it.get("url"), "detected": it["detected"],
         "score": it.get("score"), "direction": it.get("direction")}
        for it in items if (it.get("score") or 0) >= 15 and it.get("source") != "sec-insider-buys"
    ][:15]  # fmt: skip
    options = None
    if engine.options_radar is not None:
        options = next((r for r in engine.options_radar.snapshot(200) if r.get("symbol") == sym), None)
    radar = dict(engine.radar.board.get(sym) or {}) if engine.radar is not None else {}
    events = [
        e for e in engine.calendar.upcoming(days=90, now=now) if str(e.get("symbol") or "").upper() == sym
    ][:6]
    signals = ticker_signals(engine, sym, alerts, items, now)
    watchers = len(engine.desk.watchers([sym])) if getattr(engine, "desk", None) is not None else 0
    return {
        "symbol": sym,
        "name": li.name if li is not None else "",
        "market_cap": li.market_cap if li is not None else None,
        "cap": li.cap_label if li is not None else "",
        "band": li.band if li is not None else "",
        "sector": li.sector if li is not None else "",
        "industry": li.industry if li is not None else "",
        "otc": bool(li.otc) if li is not None else False,
        "quote": {"ts": q[0], "price": q[1], "chg_day": q[2]} if q else None,
        "range": {"high": path.high, "low": path.low, "open": path.open} if path is not None else None,
        "series": [[round(t, 1), round(p, 4)] for t, p in hist[:: max(1, len(hist) // 120)]],
        "why": engine.find_catalysts([sym], now, overnight=True),
        "tally": tally(signals),
        "alerts": rows,
        "news": news,
        "insider": insider,
        "options": options,
        "radar": radar or None,
        "calendar": events,
        "watchers": watchers,
        "market": engine.calendar.market_status(now),
    }


# --------------------------------------------------------------------------- outcomes


def record_outcomes(engine: Engine, now: float | None = None) -> int:
    """Stamp each recent alert with its move an hour later and at its session's close, per
    reference symbol, in the alert's direction (+ = it went the alert's way). Returns stamps made."""
    now = now or time.time()
    made = 0
    for a in engine.storage.recent_alerts(400, since=now - 3 * 86400):
        e = a.get("edge") or {}
        refs = dict(e.get("refs") or {})
        mv = a.get("move") or {}
        if not refs and mv.get("symbol") and mv.get("price"):
            refs = {mv["symbol"]: mv["price"]}
        if not refs:
            continue
        sign = {"up": 1, "down": -1}.get(_direction(a), 0)
        out = dict(e.get("outcome") or {})
        created = float(a.get("created", 0))
        close_ts = _session_close(created)
        changed = False
        for sym, ref in list(refs.items())[:3]:
            if not ref:
                continue
            row = dict(out.get(sym) or {})
            q = engine.price_of(sym, now=now)
            if not q or not q[1] or q[0] < created:
                continue
            move = (q[1] / float(ref) - 1.0) * 100.0
            if "h1" not in row and now - created >= 3600 and q[0] - created < 3 * 3600:
                row["h1"] = round(move, 2)
                changed = True
            if "close" not in row and now >= close_ts and abs(q[0] - close_ts) < 3 * 3600:
                row["close"] = round(move, 2)
                changed = True
            row["sign"] = sign
            out[sym] = row
        if changed:
            engine.storage.update_alert_edge(a["id"], {"outcome": out})
            made += 1
    return made


def _session_close(ts: float) -> float:
    d = datetime.fromtimestamp(ts, ET)
    close = d.replace(hour=16, minute=0, second=0, microsecond=0)
    if d >= close:  # after hours: the next session's close
        close = close + timedelta(days=1)
        while close.weekday() >= 5:
            close = close + timedelta(days=1)
    return close.timestamp()


def track_record(engine: Engine, days: float = 30, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    groups: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(2000, since=now - days * 86400):
        out = (a.get("edge") or {}).get("outcome") or {}
        if not out:
            continue
        cat = alert_category(a)
        key = f"{cat}:{a.get('severity')}" if cat in ("news", "macro") else cat
        sym, row = next(iter(out.items()))
        sign = row.get("sign") or 0
        if not sign:
            continue
        g = groups.setdefault(key, {"h1": [], "close": []})
        for k in ("h1", "close"):
            if row.get(k) is not None:
                g[k].append(sign * row[k])
    rows = []
    for key, g in groups.items():

        def stats(xs: list[float]) -> dict[str, Any] | None:
            if not xs:
                return None
            return {
                "n": len(xs),
                "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                "median": round(st.median(xs), 2),
            }

        rows.append({"kind": key, "h1": stats(g["h1"]), "close": stats(g["close"])})
    rows.sort(key=lambda r: -((r["close"] or r["h1"] or {}).get("n") or 0))
    return {
        "days": days,
        "rows": rows,
        "note": "Moves in each alert's direction (+ = it went the alert's way), from the alert's reference price. Not a forecast; the system never trades.",
    }


# --------------------------------------------------------------------------- smart money


def smart_money(engine: Engine, now: float | None = None, limit: int = 40) -> dict[str, Any]:
    now = now or time.time()
    rows: list[dict[str, Any]] = []
    if engine.options_radar is not None:
        for r in engine.options_radar.snapshot(60):
            if now - (r.get("detected") or 0) > 86400:
                continue
            calls, puts = r.get("call_premium") or 0, r.get("put_premium") or 0
            side = {"calls": "up", "puts": "down"}.get(r.get("side") or "") or (
                "up" if calls > puts else "down" if puts > calls else ""
            )
            rows.append({"kind": "options", "symbol": r.get("symbol"), "ts": r.get("detected") or 0, "side": side,
                         "label": f"{r.get('kind', 'flow')} · calls ${calls / 1e6:.1f}M · puts ${puts / 1e6:.1f}M" if calls or puts else r.get("kind", "options"),
                         "stock_change_pct": r.get("stock_change_pct"), "severity": r.get("severity")})  # fmt: skip
    for it in engine.storage.items_from("sec-insider-buys", since=now - 7 * 86400, limit=30):
        rows.append({"kind": "insider", "symbol": (it.get("tickers") or [""])[0], "ts": it["detected"], "side": "up",
                     "label": it["title"], "severity": it.get("severity"), "url": it.get("url")})  # fmt: skip
    if engine.radar is not None:
        for sym, r in engine.radar.board.items():
            ign = r.get("ignition")
            if ign and now - (ign.get("detected") or 0) < 6.5 * 3600:
                rows.append({"kind": "ignition", "symbol": sym, "ts": ign["detected"], "side": ign["direction"],
                             "label": f"{ign['kind']} {ign['move_pct']:+.1f}% · {ign.get('burst') or ign.get('rvol') or '?'}× volume" + (" · primed: " + ign["primed"][0] if ign.get("primed") else ""),
                             "alert_id": r.get("alert_id")})  # fmt: skip
    rows.sort(key=lambda r: -r["ts"])
    return {"rows": rows[:limit], "market": engine.calendar.market_status(now)}


# --------------------------------------------------------------------------- market map


def sectors(engine: Engine, now: float | None = None, max_age_s: float = 900) -> dict[str, Any]:
    """Today's cap-weighted move per sector, from every live price the tape holds."""
    now = now or time.time()
    by: dict[str, list[tuple[str, float, float]]] = {}
    for sym, q in list(engine.live_quotes.quotes.items()):
        if now - q.ts > max_age_s or q.change_pct is None:
            continue
        li = engine.universe.get(sym)
        if li is None or not li.market_cap or not li.sector or li.market_cap < 300e6:
            continue
        by.setdefault(li.sector, []).append((sym, q.change_pct, li.market_cap))
    rows = []
    for sec, xs in by.items():
        if len(xs) < 3:
            continue
        cap = sum(x[2] for x in xs)
        avg = sum(x[1] * x[2] for x in xs) / cap
        xs.sort(key=lambda x: -x[1])
        rows.append({
            "sector": sec, "chg": round(avg, 2), "n": len(xs), "up": sum(1 for x in xs if x[1] > 0),
            "leaders": [{"symbol": s, "chg": round(c, 2)} for s, c, _ in xs[:3]],
            "laggards": [{"symbol": s, "chg": round(c, 2)} for s, c, _ in xs[-3:][::-1]],
        })  # fmt: skip
    rows.sort(key=lambda r: -r["chg"])
    return {"rows": rows, "covered": sum(r["n"] for r in rows), "market": engine.calendar.market_status(now)}


# --------------------------------------------------------------------------- search


def search(engine: Engine, text: str, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    text = (text or "").strip()[:60]
    if len(text) < 2:
        return {"query": text, "companies": [], "alerts": [], "items": []}
    companies = []
    sym = clean_symbol(text)
    if sym and engine.universe.get(sym) is not None:
        li = engine.universe.get(sym)
        companies.append({"symbol": li.symbol, "name": li.name, "cap": li.cap_label})
    if len(text) >= 3:  # names that start with (or contain a word starting with) the text, biggest first
        low = text.lower()
        hits = [
            li for li in engine.universe.by_symbol.values()
            if li.common and (li.name.lower().startswith(low) or f" {low}" in li.name.lower())
        ]  # fmt: skip
        hits.sort(key=lambda li: (not li.name.lower().startswith(low), -(li.market_cap or 0)))
        for li in hits[:6]:
            if all(c["symbol"] != li.symbol for c in companies):
                companies.append({"symbol": li.symbol, "name": li.name, "cap": li.cap_label})
    found = engine.storage.search(text, since=now - 30 * 86400, limit=25)
    return {
        "query": text,
        "companies": companies[:6],
        "alerts": [
            {
                "id": a["id"],
                "title": a["title"],
                "severity": a["severity"],
                "created": a["created"],
                "tickers": a.get("tickers") or [],
            }
            for a in found["alerts"]
        ],
        "items": [
            {
                "title": it["title"],
                "source": it["source"],
                "url": it.get("url"),
                "detected": it["detected"],
                "score": it.get("score"),
            }
            for it in found["items"]
        ],
    }
