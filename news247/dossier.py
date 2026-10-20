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

import json
import math
import re
import statistics as st
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .desk import alert_category, clean_symbol
from .market.universe import band as band_of
from .market.universe import fmt_cap

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
SEV_W = {"CRITICAL": 3.0, "HIGH": 2.0, "MEDIUM": 1.0, "LOW": 0.5}


def _direction(a: dict[str, Any]) -> str:
    e = a.get("edge") or {}
    d = (e.get("play") or {}).get("direction") or (a.get("analysis") or {}).get("direction") or ""
    if not d:
        d = (e.get("ignition") or {}).get("direction") or (e.get("smallcap") or {}).get("direction") or ""
    if not d:
        d = (e.get("confluence") or {}).get("side") or ""
    if not d:
        d = (e.get("flow") or {}).get("dir") or ""  # options flow, read from its premium (flowrec.py)
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
        if cat == "radar":
            cat = "tape"  # a radar alert and an ignition are the same kind of evidence: the tape
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
        "rel": relative_strength(engine, sym, now),
        "session": session_context(engine, sym, now),
        "peers": peers(engine, sym, now),
        "earnings": _earnings(engine, sym),
        "persona": _persona(engine, sym),
    }


def _persona(engine: Engine, sym: str) -> dict[str, Any] | None:
    """How the stock has taken its own news here (persona.py), when it has enough graded."""
    from .persona import persona_for

    return persona_for(getattr(engine, "persona_rates", None) or {}, sym)


def _earnings(engine: Engine, sym: str) -> dict[str, Any] | None:
    """The next report date, from Nasdaq's earnings calendar (the Camillo desk's book)."""
    book = getattr(engine.trends, "earnings", None) if engine.trends is not None else None
    if book is None:
        return None
    try:
        return book.lookup(sym)
    except Exception:  # noqa: BLE001 - a missing date must not break the page
        return None


# --------------------------------------------------------------------------- outcomes


CHECKPOINTS = (("m5", 300), ("m15", 900), ("m30", 1800))


def record_outcomes(engine: Engine, now: float | None = None) -> int:
    """Stamp each recent alert with its move an hour later, at its session's close, at the next
    session's close (d1) and five sessions on (d5), per reference symbol, in the alert's direction
    (+ = it went the alert's way). A stamp needs a quote within 3 hours of its moment. Returns
    alerts stamped."""
    now = now or time.time()
    made = 0
    holidays = engine.calendar.holidays
    for a in engine.storage.recent_alerts(1500, since=now - 10 * 86400):
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
        closes = session_closes(created, 6, holidays)
        close_ts = closes[0]
        if all(k in r for r in out.values() for k in ("h1", "close", "d1", "d5")) and out:
            continue  # fully graded
        changed = False
        exc_new: dict[str, float] | None = None
        for i, (sym, ref) in enumerate(list(refs.items())[:3]):
            if not ref:
                continue
            row = dict(out.get(sym) or {})
            q = engine.price_of(sym, now=now)
            if not q or not q[1] or q[0] < created:
                continue
            move = (q[1] / float(ref) - 1.0) * 100.0
            if i == 0 and sign and now < close_ts and q[0] <= close_ts:
                # its best run and worst dip before the close, its way (heat.py reads these)
                its = round(sign * move, 2)
                exc = dict(e.get("exc") or {})
                if its > exc.get("mfe", -1e9) or its < exc.get("mae", 1e9):
                    exc["mfe"] = max(exc.get("mfe", its), its)
                    exc["mae"] = min(exc.get("mae", its), its)
                    exc_new = exc
            if i == 0:
                # the news clock: where it stood 5, 15 and 30 minutes in (newsclock.py)
                for key, after in CHECKPOINTS:
                    cp = created + after
                    if key not in row and now >= cp and cp - 60 <= q[0] <= cp + 150:
                        row[key] = round(move, 2)
                        changed = True
                # into the open: news from outside the session, at the first print after 9:30
                op = close_ts - 6.5 * 3600
                if "open" not in row and created < op and op <= q[0] <= op + 300 and now <= op + 900:
                    row["open"] = round(move, 2)
                    changed = True
            if "h1" not in row and now - created >= 3600 and q[0] - created < 3 * 3600:
                row["h1"] = round(move, 2)
                changed = True
            if "close" not in row and now >= close_ts and abs(q[0] - close_ts) < 3 * 3600:
                row["close"] = round(move, 2)
                changed = True
            for key, ts in (("d1", closes[1]), ("d5", closes[5])):
                if key not in row and now >= ts and abs(q[0] - ts) < 3 * 3600:
                    row[key] = round(move, 2)
                    changed = True
            row["sign"] = sign
            out[sym] = row
        patch: dict[str, Any] = {"outcome": out} if changed else {}
        if exc_new is not None:
            patch["exc"] = exc_new
        if e.get("mkt_ref"):  # the market over the same window, on the same clock
            mkt = dict(e.get("mkt_outcome") or {})
            q = engine.price_of("SPY", now=now)
            if q and q[1] and q[0] >= created:
                mmove = round((q[1] / float(e["mkt_ref"]) - 1.0) * 100.0, 2)
                before = len(mkt)
                if "h1" not in mkt and now - created >= 3600 and q[0] - created < 3 * 3600:
                    mkt["h1"] = mmove
                for key, ts in (("close", close_ts), ("d1", closes[1]), ("d5", closes[5])):
                    if key not in mkt and now >= ts and abs(q[0] - ts) < 3 * 3600:
                        mkt[key] = mmove
                if len(mkt) > before:
                    patch["mkt_outcome"] = mkt
        if e.get("sector_ref") and "close" not in (e.get("sector_outcome") or {}):
            from .misses import grade_sector

            sc = grade_sector(engine, e, created, close_ts, now)
            if sc is not None:
                patch["sector_outcome"] = {"close": sc}
        if patch:
            engine.storage.update_alert_edge(a["id"], patch)
            made += 1
    return made


def excess(a: dict[str, Any], horizon: str = "close") -> float | None:
    """How far an alert's stock went its way beyond the market (SPY) over the same window, or None
    when either side isn't graded yet: + = it beat the market in the alert's direction."""
    e = a.get("edge") or {}
    out, mkt = e.get("outcome") or {}, e.get("mkt_outcome") or {}
    row = next(iter(out.values()), None) if out else None
    if not row or not row.get("sign") or row.get(horizon) is None or mkt.get(horizon) is None:
        return None
    return round(row["sign"] * (row[horizon] - mkt[horizon]), 2)


def session_closes(ts: float, n: int, holidays: set[str] | None = None) -> list[float]:
    """The close of the session an alert belongs to (the next one after 16:00), then the ``n - 1``
    sessions after it, skipping weekends and market holidays."""
    hol = holidays or set()
    d = datetime.fromtimestamp(ts, ET)
    close = d.replace(hour=16, minute=0, second=0, microsecond=0)
    if d >= close:
        close += timedelta(days=1)
    out = []
    while len(out) < n:
        if close.weekday() < 5 and close.date().isoformat() not in hol:
            out.append(close.timestamp())
        close += timedelta(days=1)
    return out


def _session_close(ts: float) -> float:
    d = datetime.fromtimestamp(ts, ET)
    close = d.replace(hour=16, minute=0, second=0, microsecond=0)
    if d >= close:  # after hours: the next session's close
        close = close + timedelta(days=1)
        while close.weekday() >= 5:
            close = close + timedelta(days=1)
    return close.timestamp()


def record_key(alert: Any) -> str:
    """The group an alert is graded in: headlines and macro by severity, the rest by kind."""
    cat = alert_category(alert)
    if isinstance(alert, dict):
        sev = alert.get("severity")
    else:
        sev = getattr(getattr(alert, "severity", None), "name", "")
    return f"{cat}:{sev}" if cat in ("news", "macro") else cat


RECORD_LABEL = {"news": "Headlines", "macro": "Macro", "radar": "Radar", "options": "Options",
                "insider": "Insider buys", "confluence": "Confluence"}  # fmt: skip


def base_rate(rates: dict[str, Any], alert: Any, min_n: int = 8) -> dict[str, Any] | None:
    """How alerts like this one did (its group's hit rate and median, an hour later and at the
    close), when the group has at least ``min_n`` graded alerts."""
    key = record_key(alert)
    row = rates.get(key)
    if not row:
        return None
    h1, close = row.get("h1"), row.get("close")
    if not ((h1 and h1["n"] >= min_n) or (close and close["n"] >= min_n)):
        return None
    cat, _, sev = key.partition(":")
    label = RECORD_LABEL.get(cat, cat.title()) + (f" · {sev}" if sev else "")
    out = {"key": key, "label": label, "h1": h1 if h1 and h1["n"] >= min_n else None,
           "close": close if close and close["n"] >= min_n else None}  # fmt: skip
    # a group that went the wrong way most of the time, on enough alerts to mean something
    out["fades"] = any(x and x["n"] >= 20 and x["hit"] <= 0.4 for x in (out["h1"], out["close"]))
    # the typical path after alerts like this: median move (in the alert's direction) at each horizon
    path = [("1h", h1), ("close", close), ("next day", row.get("d1")), ("5 days", row.get("d5"))]
    out["path"] = [{"at": k, "median": x["median"], "n": x["n"]} for k, x in path if x and x["n"] >= min_n]
    return out


def base_line(base: dict[str, Any] | None) -> str:
    """'Alerts like this (Options): 62% went their way in 1h, 58% by the close · 31 graded'."""
    if not base:
        return ""
    bits = []
    if base.get("h1"):
        bits.append(f"{round(base['h1']['hit'] * 100)}% went their way in 1h")
    if base.get("close"):
        bits.append(f"{round(base['close']['hit'] * 100)}% by the close")
    n = max((base.get(k) or {}).get("n", 0) for k in ("h1", "close"))
    fades = " · historically fades" if base.get("fades") else ""
    return f"Alerts like this ({base['label']}): {', '.join(bits)} · {n} graded{fades}"


def track_record(engine: Engine, days: float = 30, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    groups: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(2000, since=now - days * 86400):
        out = (a.get("edge") or {}).get("outcome") or {}
        if not out:
            continue
        key = record_key(a)
        sym, row = next(iter(out.items()))
        sign = row.get("sign") or 0
        if not sign:
            continue
        g = groups.setdefault(key, {"h1": [], "close": [], "d1": [], "d5": [], "x_close": []})
        for k in ("h1", "close", "d1", "d5"):
            if row.get(k) is not None:
                g[k].append(sign * row[k])
        x = excess(a)
        if x is not None:
            g["x_close"].append(x)
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

        rows.append(
            {
                "kind": key,
                "h1": stats(g["h1"]),
                "close": stats(g["close"]),
                "d1": stats(g["d1"]),
                "d5": stats(g["d5"]),
                "x_close": stats(g["x_close"]),  # its way beyond the market (SPY), by the close
            }
        )
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


def search(
    engine: Engine, text: str, now: float | None = None, days: float = 30, min_sev: str = "", kind: str = ""
) -> dict[str, Any]:
    """Companies, alerts and headlines matching the text; alerts can be narrowed to a severity and
    up, a kind (news, radar, options, insider, macro, confluence) and a window."""
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
    found = engine.storage.search(text, since=now - days * 86400, limit=80)
    if min_sev in SEV_W:
        floor = list(SEV_W).index(min_sev)  # CRITICAL, HIGH, MEDIUM, LOW: lower index = more severe
        found["alerts"] = [a for a in found["alerts"] if list(SEV_W).index(a.get("severity", "LOW")) <= floor]
    if kind:
        found["alerts"] = [a for a in found["alerts"] if alert_category(a) == kind]
        if kind != "news":
            found["items"] = []
    found["alerts"], found["items"] = found["alerts"][:25], found["items"][:25]
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


# --------------------------------------------------------------------------- confluence

CONF_MIN_KINDS = 3  # independent kinds of evidence on one side: headlines, options, insider, tape
CONF_MIN_WEIGHT = 4.0
CONF_HIGH_WEIGHT = 7.0
CONF_FRESH_S = 2 * 3600  # at least one non-insider signal this recent: something is happening now
CONF_KIND_LABEL = {"news": "headlines", "options": "options flow", "insider": "insider buying",
                   "tape": "the tape", "macro": "macro"}  # fmt: skip


def confluence_candidates(engine: Engine, now: float | None = None) -> list[dict[str, Any]]:
    """Stocks where independent kinds of evidence line up on one side right now: the evidence tally
    leans clearly one way, with 3+ kinds of evidence on that side, a combined weight of 4+, and at
    least one fresh (2-hour) signal that isn't an insider filing. Strongest first."""
    now = now or time.time()
    syms: set[str] = set()
    for a in engine.storage.recent_alerts(300, since=now - 6.5 * 3600):
        if a.get("kind") in ("news", "options", "price"):
            syms.update(t for t in (a.get("tickers") or [])[:3] if t and t not in MACRO)
    if engine.radar is not None:
        syms.update(
            s
            for s, r in engine.radar.board.items()
            if (r.get("ignition") or {}).get("detected", 0) > now - 6.5 * 3600
        )
    if engine.options_radar is not None:
        syms.update(
            r["symbol"]
            for r in engine.options_radar.snapshot(80)
            if now - (r.get("detected") or 0) < 6.5 * 3600
        )
    out = []
    for sym in syms:
        alerts = engine.storage.alerts_for_ticker(sym, since=now - 3 * 86400, limit=40)
        items = engine.storage.items_for_ticker(sym, since=now - 3 * 86400, limit=40)
        sig = ticker_signals(engine, sym, alerts, items, now)
        t = tally(sig)
        if t["lean"] not in ("bullish", "bearish"):
            continue
        side = "up" if t["lean"] == "bullish" else "down"
        mine = [x for x in sig if x["side"] == side]
        kinds = sorted({x["kind"] for x in mine})
        weight = round(sum(x["weight"] for x in mine), 1)
        fresh = [x for x in mine if x["kind"] != "insider" and now - (x.get("ts") or 0) <= CONF_FRESH_S]
        if len(kinds) < CONF_MIN_KINDS or weight < CONF_MIN_WEIGHT or not fresh:
            continue
        out.append({"symbol": sym, "side": side, "weight": weight, "against": t["bear"] if side == "up" else t["bull"],
                    "kinds": kinds, "signals": sorted(mine, key=lambda x: -x["weight"])[:6]})  # fmt: skip
    out.sort(key=lambda c: -c["weight"])
    return out


MACRO = {"SPY", "QQQ", "IWM", "DIA", "TLT", "USO", "XLE", "GLD", "UUP", "VIX", "^VIX"}


# --------------------------------------------------------------------------- peers and relative strength


def peers(engine: Engine, raw: str, now: float | None = None, limit: int = 10) -> dict[str, Any]:
    """The stock's industry peers (its sector when the industry is thin), nearest in size first,
    with their moves today; and, when the stock itself moved 3%+, the peers that haven't moved
    yet (a quarter of its move or less, same direction): where a sympathy move would come from."""
    now = now or time.time()
    sym = clean_symbol(raw) or ""
    li = engine.universe.get(sym) if sym else None
    if li is None or not (li.industry or li.sector):
        return {"symbol": sym, "basis": None, "rows": [], "laggards": []}

    def pool(attr: str) -> list[Any]:
        val = getattr(li, attr)
        return [x for x in engine.universe.by_symbol.values()
                if x.symbol != sym and x.common and val and getattr(x, attr) == val and (x.market_cap or 0) >= 300e6]  # fmt: skip

    basis, group = "industry", pool("industry")
    if len(group) < 3:  # a thin industry: its sector, when that adds anyone
        wider = pool("sector")
        if len(wider) > len(group):
            basis, group = "sector", wider
    mc = li.market_cap or 1e9
    group.sort(key=lambda x: abs(math.log((x.market_cap or 1e9) / mc)))
    rows = []
    for x in group[:limit]:
        q = engine.price_of(x.symbol, 1800, now=now)
        rows.append(
            {
                "symbol": x.symbol,
                "name": x.name,
                "cap": x.cap_label,
                "chg": round(q[2], 2) if q and q[2] is not None else None,
            }
        )
    me = engine.price_of(sym, 1800, now=now)
    my = me[2] if me else None
    laggards = []
    if my is not None and abs(my) >= 3:
        sign = 1 if my > 0 else -1
        laggards = [r for r in rows if r["chg"] is not None and r["chg"] * sign <= abs(my) / 4]
    moved = [r["chg"] for r in rows if r["chg"] is not None]
    return {
        "symbol": sym, "basis": basis, "group": li.industry if basis == "industry" else li.sector,
        "me": round(my, 2) if my is not None else None,
        "avg": round(sum(moved) / len(moved), 2) if moved else None,
        "rows": rows, "laggards": laggards[:5],
    }  # fmt: skip


def relative_strength(
    engine: Engine, sym: str, now: float | None = None, market: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Today's move against the S&P 500 (SPY) and the stock's own sector: is the move its own?"""
    now = now or time.time()
    me = engine.price_of(sym, 1800, now=now)
    if not me or me[2] is None:
        return None
    li = engine.universe.get(sym)
    spy = engine.price_of("SPY", 1800, now=now)
    sec = None
    if li is not None and li.sector:
        board = market if market is not None else sectors(engine, now)
        sec = next((r for r in board["rows"] if r["sector"] == li.sector), None)
    out: dict[str, Any] = {
        "chg": round(me[2], 2),
        "spy": None,
        "sector": None,
        "sector_name": li.sector if li else "",
    }
    if spy and spy[2] is not None:
        out["spy"] = round(spy[2], 2)
        out["vs_spy"] = round(me[2] - spy[2], 2)
    if sec is not None:
        out["sector"] = sec["chg"]
        out["vs_sector"] = round(me[2] - sec["chg"], 2)
    ref = out.get("vs_sector", out.get("vs_spy"))
    if ref is None:
        out["read"] = ""
    elif abs(me[2]) >= 1.5 and abs(ref) <= 0.5 * abs(me[2]):
        out["read"] = "Moving with its " + ("sector" if "vs_sector" in out else "market")
    elif abs(ref) >= 1.5:
        out["read"] = "Its own move" + (
            " (against the market)" if out.get("spy") and out["spy"] * me[2] < 0 else ""
        )
    else:
        out["read"] = "In line"
    return out


# --------------------------------------------------------------------------- gaps, sector drill-down, halts


def gaps(
    engine: Engine, now: float | None = None, min_pct: float = 3.0, max_age_s: float = 900, limit: int = 25
) -> dict[str, Any]:
    """Every company worth $300M+ moving ``min_pct``% or more on the whole-market tape, each with
    the headline behind it (overnight news counts: it's the reason for a morning gap). Before the
    open these are the pre-market gappers."""
    now = now or time.time()
    up, down = [], []
    for sym, q in list(engine.live_quotes.quotes.items()):
        if now - q.ts > max_age_s or q.change_pct is None or abs(q.change_pct) < min_pct:
            continue
        li = engine.universe.get(sym)
        if li is None or not li.common or (li.market_cap or 0) < 300e6:
            continue
        row = {"symbol": sym, "name": li.name, "cap": li.cap_label, "band": li.band, "sector": li.sector,
               "price": q.price, "chg": round(q.change_pct, 2)}  # fmt: skip
        (up if q.change_pct > 0 else down).append(row)
    up.sort(key=lambda r: -r["chg"])
    down.sort(key=lambda r: r["chg"])
    up, down = up[:limit], down[:limit]
    for r in up + down:
        why = engine.find_catalysts([r["symbol"]], now, overnight=True)
        r["why"] = (
            {"title": why[0]["title"], "source": why[0]["source"], "url": why[0]["url"], "age": why[0]["age"]}
            if why
            else None
        )
    status = engine.calendar.market_status(now)
    return {"up": up, "down": down, "phase": status.get("phase"), "market": status, "min_pct": min_pct}


def sector_detail(
    engine: Engine, name: str, now: float | None = None, max_age_s: float = 900
) -> dict[str, Any]:
    """One sector of the market map: each industry's cap-weighted move and every stock in it."""
    now = now or time.time()
    stocks = []
    for sym, q in list(engine.live_quotes.quotes.items()):
        if now - q.ts > max_age_s or q.change_pct is None:
            continue
        li = engine.universe.get(sym)
        if li is None or li.sector != name or not li.common or (li.market_cap or 0) < 300e6:
            continue
        stocks.append({"symbol": sym, "name": li.name, "industry": li.industry or "Other", "cap": li.cap_label,
                       "mcap": li.market_cap, "chg": round(q.change_pct, 2)})  # fmt: skip
    inds: dict[str, list[dict[str, Any]]] = {}
    for s in stocks:
        inds.setdefault(s["industry"], []).append(s)
    industries = []
    for ind, xs in inds.items():
        cap = sum(x["mcap"] for x in xs)
        industries.append({"industry": ind, "n": len(xs), "chg": round(sum(x["chg"] * x["mcap"] for x in xs) / cap, 2),
                           "up": sum(1 for x in xs if x["chg"] > 0)})  # fmt: skip
    industries.sort(key=lambda r: -r["chg"])
    stocks.sort(key=lambda r: -r["chg"])
    for s in stocks:
        s.pop("mcap")
    return {"sector": name, "industries": industries, "stocks": stocks[:80], "n": len(stocks)}


HALT_TITLE = re.compile(
    r"— (.+?) \[([A-Z0-9]+)\]\s*$"
)  # sources/halts.py: "TRADING HALT SYM (Name) — Label [CODE]"


# --------------------------------------------------------------------------- the strategy lab

SEV_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
LAB_KINDS = ("all", "news", "radar", "options", "insider", "confluence", "macro")
LAB_HORIZONS = {"h1": "an hour later", "close": "at the close", "d1": "the next day", "d5": "five days on"}


def strategy_lab(
    engine: Engine,
    device: str = "",
    *,
    kind: str = "all",
    min_sev: str = "HIGH",
    horizon: str = "close",
    scope: str = "all",
    days: float = 60,
    stake: float = 1000.0,
    skip_fades: bool = False,
    catalyst: str = "all",
    band: str = "all",
    now: float | None = None,
) -> dict[str, Any]:
    """If you had followed every alert like this: a paper trade of ``stake`` dollars on each one,
    in its direction, from its own reference price, out at ``horizon``. The curve, the hit rate,
    the average and median trade, the best and worst, the deepest drawdown, and a split by kind.
    Only alerts that have been graded count; no costs or slippage."""
    now = now or time.time()
    if kind not in LAB_KINDS:
        raise ValueError("kind is " + ", ".join(LAB_KINDS))
    if horizon not in LAB_HORIZONS:
        raise ValueError("horizon is h1, close, d1 or d5")
    if min_sev not in ("MEDIUM", "HIGH", "CRITICAL"):
        raise ValueError("min_sev is MEDIUM, HIGH or CRITICAL")
    if scope not in ("all", "mine"):
        raise ValueError("scope is all or mine")
    from .catalysts import LABELS, classify

    if catalyst != "all" and catalyst not in LABELS:
        raise ValueError("not a kind of news Foretape reads")
    if band not in ("all", "small", "mid", "large"):
        raise ValueError("band is all, small, mid or large")
    days = max(1.0, min(365.0, float(days)))
    mine: set[str] = set()
    if scope == "mine":
        p = engine.desk.profiles.get(device) or {}
        mine = set(p.get("watchlist") or []) | set(p.get("holdings") or {})
    floor = SEV_ORDER.get(min_sev, 2)
    trades = []
    for a in engine.storage.recent_alerts(5000, since=now - days * 86400):
        if SEV_ORDER.get(a.get("severity"), 0) < floor or a.get("kind") == "system":
            continue
        cat = alert_category(a)
        if kind != "all" and cat != kind:
            continue
        e = a.get("edge") or {}
        if skip_fades and (e.get("base") or {}).get("fades"):
            continue
        out = e.get("outcome") or {}
        if not out:
            continue
        if catalyst != "all" or band != "all":
            c = e.get("catalyst") or classify(a, engine.universe)
            if not c or (catalyst != "all" and c["key"] != catalyst) or (band != "all" and c["band"] != band):
                continue
        sym, row = next(iter(out.items()))
        sign, val = row.get("sign") or 0, row.get(horizon)
        if not sign or val is None:
            continue
        if scope == "mine" and not ({sym} | set(a.get("tickers") or [])) & mine:
            continue
        ret = sign * float(val)
        trades.append({"id": a["id"], "ts": a["created"], "symbol": sym, "title": a.get("title", ""),
                       "kind": cat, "severity": a.get("severity"), "ret": round(ret, 2),
                       "pnl": round(stake * ret / 100.0, 2)})  # fmt: skip
    trades.sort(key=lambda t: t["ts"])
    base = {"kind": kind, "min_sev": min_sev, "horizon": horizon, "horizon_label": LAB_HORIZONS[horizon],
            "catalyst": catalyst, "band": band,
            "scope": scope, "days": days, "stake": stake, "skip_fades": skip_fades}  # fmt: skip
    if not trades:
        return {**base, "n": 0, "curve": [], "by_kind": [], "best": None, "worst": None}
    cum = peak = 0.0
    drawdown = 0.0
    curve = []
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        drawdown = min(drawdown, cum - peak)
        curve.append([t["ts"], round(cum, 2)])
    rets = [t["ret"] for t in trades]
    by: dict[str, list[float]] = {}
    for t in trades:
        by.setdefault(t["kind"], []).append(t["ret"])
    by_kind = sorted(
        ({"kind": k, "n": len(v), "hit": round(sum(x > 0 for x in v) / len(v), 2),
          "avg": round(sum(v) / len(v), 2), "pnl": round(stake * sum(v) / 100.0, 2)} for k, v in by.items()),
        key=lambda r: -r["pnl"],
    )  # fmt: skip
    return {
        **base,
        "n": len(trades),
        "wins": sum(r > 0 for r in rets),
        "hit": round(sum(r > 0 for r in rets) / len(rets), 2),
        "avg": round(sum(rets) / len(rets), 2),
        "median": round(st.median(rets), 2),
        "pnl": round(cum, 2),
        "max_drawdown": round(drawdown, 2),
        "best": max(trades, key=lambda t: t["ret"]),
        "worst": min(trades, key=lambda t: t["ret"]),
        "curve": curve if len(curve) <= 400 else curve[:: max(1, len(curve) // 400)] + [curve[-1]],
        "by_kind": by_kind,
        "recent": trades[-8:][::-1],
    }


# --------------------------------------------------------------------------- sympathy: peers that haven't moved

SYMPATHY_MIN_MOVE = 4.0  # % on the day for the stock whose news it is
SYMPATHY_PER_DAY = 3  # pushes per phone


def sympathy_record(engine: Engine, now: float | None = None, days: float = 120) -> dict[str, Any]:
    """How past sympathy flags played out: did the peer that hadn't moved catch up (move the
    leader's way) within the hour and by the close? Graded by Foretape as they happen."""
    now = now or time.time()
    log = json.loads(engine.storage.get_setting("sympathy_log") or "[]")
    log = [e for e in log if e["ts"] >= now - days * 86400]

    def stats(k: str) -> dict[str, Any] | None:
        xs = [e[k] for e in log if e.get(k) is not None]
        if not xs:
            return None
        return {
            "n": len(xs),
            "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
            "median": round(st.median(xs), 2),
        }

    return {"flags": len(log), "h1": stats("h1"), "close": stats("close")}


def grade_sympathy(engine: Engine, now: float | None = None) -> int:
    """Grade logged sympathy flags: the peer's move in the leader's direction an hour after the
    flag, and at the close (read between 16:00 and 16:30 ET that day; missed closes stay empty)."""
    now = now or time.time()
    raw = engine.storage.get_setting("sympathy_log")
    if not raw:
        return 0
    log = json.loads(raw)
    graded = 0
    for e in log:
        q = engine.price_of(e["lag"], 900, now=now)
        if not q or not e.get("ref"):
            continue
        move = round(e["sign"] * (q[1] / e["ref"] - 1.0) * 100.0, 2)
        if e.get("h1") is None and now >= e["ts"] + 3600 and now <= e["ts"] + 5400:
            e["h1"] = move
            graded += 1
        d = datetime.fromtimestamp(e["ts"], ET)
        close = d.replace(hour=16, minute=0, second=0, microsecond=0).timestamp()
        if e.get("close") is None and close <= now <= close + 1800:
            e["close"] = move
            graded += 1
    if graded:
        engine.storage.set_setting("sympathy_log", json.dumps(log[-500:]))
    return graded


def since_you_looked(
    engine: Engine, device: str, sym: str, now: float | None = None
) -> dict[str, Any] | None:
    """What happened on a stock since this phone last opened its page: the move from the price it
    saw then, and the alerts and headlines that came in. None when it hasn't looked before, looked
    in the last 20 minutes, or looked more than 60 days ago."""
    now = now or time.time()
    seen = ((engine.desk.profiles.get(device) or {}).get("seen") or {}).get(sym)
    if not seen or not 1200 <= now - seen["ts"] <= 60 * 86400:
        return None
    q = engine.price_of(sym)
    move = round((q[1] / seen["px"] - 1.0) * 100.0, 2) if q and seen.get("px") else None
    alerts = engine.storage.alerts_for_ticker(sym, since=seen["ts"], limit=30)
    rank = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1}
    top = sorted(alerts, key=lambda a: (-rank.get(a.get("severity"), 0), -a.get("created", 0)))[:3]
    heads = engine.storage.items_for_ticker(sym, since=seen["ts"], limit=30, min_score=20.0)
    if move is None and not alerts and not heads:
        return {"ts": seen["ts"], "quiet": True, "move_pct": None, "alerts": 0, "headlines": 0, "top": []}
    return {
        "ts": seen["ts"],
        "quiet": False,
        "move_pct": move,
        "alerts": len(alerts),
        "headlines": len(heads),
        "top": [
            {"id": a["id"], "title": a["title"], "severity": a["severity"], "created": a["created"]}
            for a in top
        ],
    }


RESUME_TITLE = re.compile(r"^▶ (\S+) resumes trading at (\S+) ET")


def halts_board(engine: Engine, now: float | None = None, hours: float = 24) -> dict[str, Any]:
    """Trading halts from Nasdaq's feed (all US exchanges), newest first, with the reason and the
    resumption time when the feed gave one."""
    now = now or time.time()
    rows = []
    items = engine.storage.items_from("nasdaq-halts", since=now - hours * 3600, limit=120)
    resumed = {}  # symbol -> "10:35:00", from the resumption items (newest first, so the latest wins)
    for it in items:
        m = RESUME_TITLE.match(it["title"])
        if m:
            resumed.setdefault(m.group(1), m.group(2))
    for it in items:
        if RESUME_TITLE.match(it["title"]):
            continue
        summ = it.get("summary") or ""
        m = HALT_TITLE.search(it["title"])
        mk = re.search(r"Market: ([^.]*)\.", summ)
        resume = re.search(r"Resumes ([^E]+?) ET", summ)
        rows.append({"symbol": (it.get("tickers") or [""])[0], "title": it["title"], "code": m.group(2) if m else "",
                     "reason": m.group(1) if m else "", "market": mk.group(1) if mk else "",
                     "halted": it.get("published") or it["detected"],
                     "resume": resume.group(1).strip() if resume else resumed.get((it.get("tickers") or [""])[0], ""),
                     "url": it.get("url")})  # fmt: skip
    return {"rows": rows, "hours": hours}


def worth_a_look(
    engine: Engine, device: str, now: float | None = None, limit: int = 8
) -> list[dict[str, Any]]:
    """Stocks next to yours that are doing something today: peers of your watchlist (not on it)
    that moved 3%+, are on the radar, or drew options flow or a confluence alert today."""
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    mine = set(p.get("watchlist") or [])
    if not mine:
        return []
    radar = engine.radar.board if engine.radar is not None else {}
    flow = (
        {r["symbol"]: r for r in engine.options_radar.snapshot(200)}
        if engine.options_radar is not None
        else {}
    )
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    conf = {
        t for a in engine.storage.recent_alerts(200, since=day0) if a.get("kind") == "confluence" for t in a.get("tickers") or []
    }  # fmt: skip
    seen: dict[str, dict[str, Any]] = {}
    for sym in list(mine)[:15]:
        for r in peers(engine, sym, now, limit=10)["rows"]:
            s = r["symbol"]
            if s in mine or s in seen:
                continue
            why, score = [], 0.0
            if s in conf:
                why.append("confluence alert")
                score += 4
            f = flow.get(s)
            if f and (f.get("detected") or 0) >= day0:
                why.append("options flow")
                score += 2
            rd = radar.get(s)
            if rd and (rd.get("ignition") or rd.get("flagged")):
                why.append("on the radar")
                score += 2
            if r["chg"] is not None and abs(r["chg"]) >= 3:
                why.append(f"{r['chg']:+.1f}% today")
                score += abs(r["chg"]) / 3
            if why:
                seen[s] = {**r, "peer_of": sym, "why": why, "score": round(score, 2)}
    return sorted(seen.values(), key=lambda r: -r["score"])[:limit]


# --------------------------------------------------------------------------- the day in one screen, sources


def day_timeline(engine: Engine, now: float | None = None) -> dict[str, Any]:
    """Today's alerts on a time axis (ET), with how each was graded, and the S&P 500's path (SPY,
    sampled every 5 minutes while the server ran)."""
    now = now or time.time()
    d0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = d0.timestamp()
    rows = []
    for a in engine.storage.recent_alerts(400, since=t0):
        if a.get("kind") == "system":
            continue
        out = (a.get("edge") or {}).get("outcome") or {}
        g = next(iter(out.values()), None) if out else None
        rows.append({
            "id": a["id"], "t": a["created"], "severity": a["severity"], "kind": alert_category(a),
            "title": a["title"][:140], "tickers": (a.get("tickers") or [])[:3], "direction": _direction(a), "grade": g,
        })  # fmt: skip
    rows.sort(key=lambda r: r["t"])
    spy = [(t, p) for t, p in engine.day_marks.get("SPY", []) if t >= t0]
    counts = {s: sum(1 for r in rows if r["severity"] == s) for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
    return {"date": d0.date().isoformat(), "start": t0, "rows": rows, "spy": spy, "counts": counts,
            "market": engine.calendar.market_status(now)}  # fmt: skip


def source_record(
    engine: Engine, days: float = 60, now: float | None = None, min_n: int = 3
) -> dict[str, Any]:
    """Which sources' alerts went their way: each first-to-publish source's graded headline alerts
    (an hour later and at the close), in the alert's own direction."""
    now = now or time.time()
    groups: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(3000, since=now - days * 86400):
        src = ((a.get("item") or {}).get("source")) or ""
        out = (a.get("edge") or {}).get("outcome") or {}
        if a.get("kind") != "news" or not src or not out:
            continue
        row = next(iter(out.values()))
        sign = row.get("sign") or 0
        if not sign:
            continue
        g = groups.setdefault(src, {"h1": [], "close": []})
        for k in ("h1", "close"):
            if row.get(k) is not None:
                g[k].append(sign * row[k])

    def stats(xs: list[float]) -> dict[str, Any] | None:
        return (
            {
                "n": len(xs),
                "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                "median": round(st.median(xs), 2),
            }
            if xs
            else None
        )

    rows = [{"source": s, "h1": stats(g["h1"]), "close": stats(g["close"])} for s, g in groups.items()]
    rows = [r for r in rows if max((r["h1"] or {}).get("n", 0), (r["close"] or {}).get("n", 0)) >= min_n]
    rows.sort(key=lambda r: -max((r["h1"] or {}).get("n", 0), (r["close"] or {}).get("n", 0)))
    return {"days": days, "rows": rows}


# --------------------------------------------------------------------------- breadth, volume, buzz


def breadth(engine: Engine, now: float | None = None, max_age_s: float = 900) -> dict[str, Any]:
    """The market under the index: how many companies worth $300M+ are up and down on the live tape,
    the cap-weighted move of all of them, big movers each way, and the split by size."""
    now = now or time.time()
    rows = []
    for sym, q in list(engine.live_quotes.quotes.items()):
        if now - q.ts > max_age_s or q.change_pct is None:
            continue
        li = engine.universe.get(sym)
        if li is None or not li.common or (li.market_cap or 0) < 300e6:
            continue
        rows.append((q.change_pct, li.market_cap, li.band))
    if not rows:
        return {"n": 0, "market": engine.calendar.market_status(now)}
    up = sum(1 for c, _, _ in rows if c > 0.05)
    down = sum(1 for c, _, _ in rows if c < -0.05)
    cap = sum(m for _, m, _ in rows)
    bands: dict[str, list[tuple[float, float]]] = {}
    for c, m, b in rows:
        bands.setdefault(b, []).append((c, m))
    order = ["mega cap", "large cap", "mid cap", "small cap", "micro cap", "nano cap"]
    by_band = [
        {"band": b, "n": len(xs), "up": sum(1 for c, _ in xs if c > 0.05),
         "chg": round(sum(c * m for c, m in xs) / sum(m for _, m in xs), 2)}
        for b, xs in sorted(bands.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
    ]  # fmt: skip
    return {
        "n": len(rows), "up": up, "down": down, "flat": len(rows) - up - down,
        "pct_up": round(up / len(rows) * 100, 1), "cap_weighted": round(sum(c * m for c, m, _ in rows) / cap, 2),
        "equal_weighted": round(sum(c for c, _, _ in rows) / len(rows), 2),
        "up5": sum(1 for c, _, _ in rows if c >= 5), "down5": sum(1 for c, _, _ in rows if c <= -5),
        "bands": by_band, "market": engine.calendar.market_status(now),
    }  # fmt: skip


def volume_leaders(
    engine: Engine, now: float | None = None, min_rvol: float = 3.0, limit: int = 25
) -> dict[str, Any]:
    """Stocks trading several times their normal volume for this time of day (the ignition
    scanner's read of the whole-market tape), with the headline behind each when there is one."""
    now = now or time.time()
    if engine.ignition is None:
        return {"enabled": False, "rows": []}
    rows = []
    for r in engine.ignition.volume_board(now, min_rvol):
        li = engine.universe.get(r["symbol"])
        if li is not None and not li.common:
            continue
        cap = (li.market_cap if li is not None else None) or r["market_cap"]
        why = engine.find_catalysts([r["symbol"]], now, overnight=True)
        rows.append({**r, "name": (li.name if li is not None else "") or r["name"], "cap": fmt_cap(cap), "band": band_of(cap),
                     "why": {"title": why[0]["title"], "source": why[0]["source"], "url": why[0]["url"], "age": why[0]["age"]} if why else None})  # fmt: skip
        if len(rows) >= limit:
            break
    return {"enabled": True, "rows": rows, "min_rvol": min_rvol, "market": engine.calendar.market_status(now)}


def buzz(
    engine: Engine, now: float | None = None, window_s: float = 3600, min_items: int = 3, limit: int = 20
) -> dict[str, Any]:
    """Stocks the news is suddenly about: headlines naming them in the last hour (from 2+ sources)
    against their usual hourly count over the past week."""
    now = now or time.time()
    week = now - 7 * 86400
    recent: dict[str, list[dict[str, Any]]] = {}
    usual: dict[str, int] = {}
    for it in engine.storage.items_with_tickers(since=week):
        for t in it["tickers"][:4]:
            if t in MACRO:
                continue
            if it["detected"] >= now - window_s:
                recent.setdefault(t, []).append(it)
            else:
                usual[t] = usual.get(t, 0) + 1
    rows = []
    hours = max(1.0, (now - window_s - week) / 3600)
    for t, items in recent.items():
        sources = {i["source"] for i in items}
        if len(items) < min_items or len(sources) < 2:
            continue
        base = usual.get(t, 0) / hours
        top = max(items, key=lambda i: i.get("score") or 0)
        rows.append({"symbol": t, "n": len(items), "sources": len(sources), "usual_per_hour": round(base, 2),
                     "ratio": round(len(items) / max(base, 0.1), 1),
                     "top": {"title": top["title"], "source": top["source"], "url": top.get("url"), "score": top.get("score")}})  # fmt: skip
    rows.sort(key=lambda r: (-r["ratio"], -r["n"]))
    return {"rows": rows[:limit], "window_s": window_s}


# --------------------------------------------------------------------------- priced in? time of day; session context


def _graded(engine: Engine, days: float, now: float) -> list[tuple[dict[str, Any], dict[str, Any], int]]:
    """(alert, its first graded row, sign) for every directional, graded alert in the window."""
    out = []
    for a in engine.storage.recent_alerts(3000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o:
            continue
        row = next(iter(o.values()))
        if row.get("sign"):
            out.append((a, row, row["sign"]))
    return out


def _bucket_stats(groups: dict[str, dict[str, list[float]]], order: list[str]) -> list[dict[str, Any]]:
    def stats(xs: list[float]) -> dict[str, Any] | None:
        return (
            {
                "n": len(xs),
                "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                "median": round(st.median(xs), 2),
            }
            if xs
            else None
        )

    return [
        {"bucket": k, "h1": stats(groups[k]["h1"]), "close": stats(groups[k]["close"])}
        for k in order
        if k in groups
    ]


def priced_in_record(engine: Engine, days: float = 60, now: float | None = None) -> dict[str, Any]:
    """Do alerts still work after the stock already moved? Graded alerts split by the stock's
    move in the 15 minutes before the alert, in the alert's direction: fresh (under 1%), already
    moved its way (1%+), or moved against it (1%+ the other way)."""
    now = now or time.time()
    groups: dict[str, dict[str, list[float]]] = {}
    for a, row, sign in _graded(engine, days, now):
        pre = (a.get("edge") or {}).get("pre") or {}
        if not pre:
            continue
        x = next(iter(pre.values())) * sign
        k = "fresh" if abs(x) < 1 else "already moved its way" if x > 0 else "moved against it first"
        g = groups.setdefault(k, {"h1": [], "close": []})
        for h in ("h1", "close"):
            if row.get(h) is not None:
                g[h].append(sign * row[h])
    return {
        "days": days,
        "rows": _bucket_stats(groups, ["fresh", "already moved its way", "moved against it first"]),
    }


TIME_BUCKETS = [((4, 0), (9, 30), "pre-market"), ((9, 30), (10, 30), "the open"), ((10, 30), (14, 0), "midday"),
                ((14, 0), (16, 0), "power hours"), ((16, 0), (20, 0), "after hours")]  # fmt: skip


def time_bucket(ts: float) -> str:
    d = datetime.fromtimestamp(ts, ET)
    hm = (d.hour, d.minute)
    if d.weekday() >= 5:
        return "weekend"
    return next((name for a, b, name in TIME_BUCKETS if a <= hm < b), "overnight")


def time_of_day_record(engine: Engine, days: float = 60, now: float | None = None) -> dict[str, Any]:
    """When do the alerts work? Graded alerts by the time (ET) they went out."""
    now = now or time.time()
    groups: dict[str, dict[str, list[float]]] = {}
    for a, row, sign in _graded(engine, days, now):
        g = groups.setdefault(time_bucket(float(a.get("created", 0))), {"h1": [], "close": []})
        for h in ("h1", "close"):
            if row.get(h) is not None:
                g[h].append(sign * row[h])
    return {
        "days": days,
        "rows": _bucket_stats(groups, [b[2] for b in TIME_BUCKETS] + ["overnight", "weekend"]),
    }


def session_context(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any] | None:
    """Where the stock sits in its session: against VWAP (from the tape's read) and against its
    9:35 price (the end of the first five minutes), with the session's high and low."""
    now = now or time.time()
    q = engine.price_of(sym, 1800, now=now)
    path = engine.live_quotes.path(sym, now)
    ig = (
        engine.ignition.latest(sym, now)
        if engine.ignition is not None and hasattr(engine.ignition, "latest")
        else None
    )
    if not q:
        return None
    px = q[1]
    out: dict[str, Any] = {"price": px}
    vwap = (ig or {}).get("vwap")
    if vwap:
        out["vwap"] = round(vwap, 4)
        out["vs_vwap"] = round((px / vwap - 1) * 100, 2)
    if path is not None:
        if path.first5:
            out["first5"] = path.first5
            out["vs_first5"] = round((px / path.first5 - 1) * 100, 2)
        if path.s_high and path.s_low:
            out["s_high"], out["s_low"] = path.s_high, path.s_low
    return out if len(out) > 1 else None


def earnings_week(
    engine: Engine, now: float | None = None, min_cap: float = 2e9, days: int = 7
) -> dict[str, Any]:
    """Every company worth ``min_cap``+ reporting in the next ``days`` days (Nasdaq's earnings
    calendar), by day, biggest first."""
    now = now or time.time()
    book = getattr(engine.trends, "earnings", None) if engine.trends is not None else None
    if book is None:
        return {"enabled": False, "days": []}
    today = datetime.fromtimestamp(now, ET).date()
    by_day: dict[str, list[dict[str, Any]]] = {}
    for sym in list(book.next):
        e = book.lookup(sym, today)
        if not e or e["in_days"] > days:
            continue
        li = engine.universe.get(sym)
        if li is None or (li.market_cap or 0) < min_cap:
            continue
        by_day.setdefault(e["date"], []).append({"symbol": sym, "name": li.name, "cap": li.cap_label, "mcap": li.market_cap,
                                                 "when": e.get("when") or "", "in_days": e["in_days"]})  # fmt: skip
    out = []
    for d in sorted(by_day):
        rows = sorted(by_day[d], key=lambda r: -(r["mcap"] or 0))
        for r in rows:
            r.pop("mcap")
        for r in rows:  # what the background warm-up already read: options' price and the usual reaction
            im = (engine.implied_cache.get(r["symbol"]) or (0, {}))[1]
            nxt = next((x for x in im.get("rows") or [] if x.get("earnings")), None)
            r["implied"] = nxt["move_pct"] if nxt else None
            er = (engine.earnings_cache.get(r["symbol"]) or (0, {}))[1]
            r["usual"] = er.get("avg_abs_move")
        out.append({"date": d, "in_days": rows[0]["in_days"], "rows": rows})
    return {"enabled": True, "days": out, "min_cap": min_cap}


# --------------------------------------------------------------------------- in play, regime, the close, similar alerts


def in_play(engine: Engine, now: float | None = None, limit: int = 15) -> dict[str, Any]:
    """The stocks that matter right now, from every board at once: the move, relative volume, news
    buzz, options flow, a confluence alert, the radar, and whether there's a reason. Each row says
    why it's in play."""
    now = now or time.time()
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows: dict[str, dict[str, Any]] = {}

    def row(sym: str) -> dict[str, Any]:
        return rows.setdefault(sym, {"symbol": sym, "score": 0.0, "why": []})

    for sym, q in list(engine.live_quotes.quotes.items()):
        if now - q.ts > 900 or q.change_pct is None or abs(q.change_pct) < 3:
            continue
        li = engine.universe.get(sym)
        if li is None or not li.common or (li.market_cap or 0) < 300e6:
            continue
        r = row(sym)
        r["score"] += min(abs(q.change_pct), 15) / 3
        r["why"].append(f"{q.change_pct:+.1f}% today")
    if engine.ignition is not None and hasattr(engine.ignition, "volume_board"):
        for v in engine.ignition.volume_board(now, 3.0)[:60]:
            r = row(v["symbol"])
            r["score"] += min(v["rvol"], 12) / 2
            r["why"].append(f"{v['rvol']:.0f}× normal volume")
    for b in buzz(engine, now)["rows"]:
        r = row(b["symbol"])
        r["score"] += min(b["n"], 8) / 2
        r["why"].append(f"{b['n']} headlines this hour")
    if engine.options_radar is not None:
        for o in engine.options_radar.snapshot(100):
            if (o.get("detected") or 0) >= day0:
                r = row(o["symbol"])
                r["score"] += 2
                r["why"].append(f"options {o.get('kind', 'flow')}")
    if engine.radar is not None:
        for sym, b in list(engine.radar.board.items()):
            if b.get("flagged") or b.get("ignition"):
                r = row(sym)
                r["score"] += 1.5
                r["why"].append("igniting" if b.get("ignition") else "on the radar")
    for a in engine.storage.recent_alerts(200, since=day0):
        if a.get("kind") == "confluence":
            for t in a.get("tickers") or []:
                r = row(t)
                r["score"] += 3
                r["why"].append("confluence alert")
    out = []
    for r in sorted(rows.values(), key=lambda x: -x["score"])[: limit * 2]:
        li = engine.universe.get(r["symbol"])
        if li is not None and not li.common:
            continue
        q = engine.price_of(r["symbol"], 1800, now=now)
        why = engine.find_catalysts([r["symbol"]], now, overnight=True)
        if why:
            r["score"] += 1
        out.append({**r, "score": round(r["score"], 1), "why": list(dict.fromkeys(r["why"])), "name": li.name if li else "",
                    "cap": li.cap_label if li else "", "chg": round(q[2], 2) if q and q[2] is not None else None,
                    "reason": {"title": why[0]["title"], "url": why[0]["url"], "source": why[0]["source"]} if why else None})  # fmt: skip
    out.sort(key=lambda x: -x["score"])
    return {"rows": out[:limit], "market": engine.calendar.market_status(now)}


DEFENSIVE = {"Utilities", "Consumer Staples", "Health Care", "Real Estate", "Telecommunications"}
CYCLICAL = {
    "Technology",
    "Consumer Discretionary",
    "Industrials",
    "Finance",
    "Financials",
    "Energy",
    "Basic Materials",
}


def regime(engine: Engine, now: float | None = None) -> dict[str, Any]:
    """Risk-on, risk-off or mixed, read from the live tape: cyclical sectors against defensive
    ones, breadth, and small caps against large. Every input is shown; it's a read, not a call."""
    now = now or time.time()
    sec = {r["sector"]: r["chg"] for r in sectors(engine, now)["rows"]}
    b = breadth(engine, now)
    cyc = [v for k, v in sec.items() if k in CYCLICAL]
    dfn = [v for k, v in sec.items() if k in DEFENSIVE]
    if not b.get("n") or not cyc or not dfn:
        return {"label": "unknown", "why": [], "inputs": {}}
    spread = round(sum(cyc) / len(cyc) - sum(dfn) / len(dfn), 2)
    bands = {x["band"]: x["chg"] for x in b.get("bands") or []}
    small = bands.get("small cap", bands.get("micro cap"))
    large = bands.get("large cap", bands.get("mega cap"))
    size = round(small - large, 2) if small is not None and large is not None else None
    on = (spread >= 0.5) + (b["pct_up"] >= 55) + (size is not None and size >= 0.5)
    off = (spread <= -0.5) + (b["pct_up"] <= 45) + (size is not None and size <= -0.5)
    label = "risk-on" if on >= 2 and not off else "risk-off" if off >= 2 and not on else "mixed"
    why = [f"cyclicals {'ahead of' if spread >= 0 else 'behind'} defensives by {abs(spread):.1f} pts",
           f"{b['pct_up']:.0f}% of stocks up"]  # fmt: skip
    if size is not None:
        why.append(f"small caps {'ahead of' if size >= 0 else 'behind'} large by {abs(size):.1f} pts")
    return {"label": label, "why": why, "inputs": {"cyc_minus_def": spread, "pct_up": b["pct_up"], "small_minus_large": size,
                                                   "cap_weighted": b["cap_weighted"]}}  # fmt: skip


def close_recap(engine: Engine, now: float | None = None) -> dict[str, Any]:
    """The close in one screen: the indexes, breadth, the regime, the best and worst sectors, the
    biggest movers and why, and how today's alerts did by the close."""
    now = now or time.time()
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    idx = {}
    for s in ("SPY", "QQQ", "IWM", "DIA"):
        q = engine.price_of(s, 6 * 3600, now=now)
        if q and q[2] is not None:
            idx[s] = round(q[2], 2)
    sec = sectors(engine, now, max_age_s=6 * 3600)["rows"]
    g = gaps(engine, now, max_age_s=6 * 3600, limit=5)
    graded = right = 0
    for a in engine.storage.recent_alerts(500, since=day0):
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if row and row.get("sign") and row.get("close") is not None:
            graded += 1
            right += row["sign"] * row["close"] > 0
    return {
        "date": datetime.fromtimestamp(now, ET).date().isoformat(), "indexes": idx,
        "breadth": breadth(engine, now, max_age_s=6 * 3600), "regime": regime(engine, now),
        "best": sec[:2], "worst": sec[-2:][::-1] if len(sec) > 2 else [],
        "up": g["up"][:3], "down": g["down"][:3], "alerts": {"graded": graded, "right": right},
    }  # fmt: skip


def close_push(c: dict[str, Any]) -> dict[str, Any] | None:
    lines = []
    if c["indexes"]:
        lines.append(" · ".join(f"{k} {v:+.1f}%" for k, v in c["indexes"].items()))
    b = c["breadth"]
    if b.get("n"):
        lines.append(f"{b['up']} up / {b['down']} down ({b['pct_up']:.0f}%) · {c['regime']['label']}")
    if c["best"]:
        lines.append(
            "Led: "
            + ", ".join(f"{r['sector']} {r['chg']:+.1f}%" for r in c["best"])
            + (
                "; lagged: " + ", ".join(f"{r['sector']} {r['chg']:+.1f}%" for r in c["worst"])
                if c["worst"]
                else ""
            )
        )
    for r in c["up"][:1] + c["down"][:1]:
        lines.append(
            f"{r['symbol']} {r['chg']:+.1f}%" + (f": {r['why']['title'][:80]}" if r.get("why") else "")
        )
    if c["alerts"]["graded"]:
        lines.append(f"Alerts: {c['alerts']['right']} of {c['alerts']['graded']} went their way by the close")
    if not lines:
        return None
    return {"id": f"close-{c['date']}", "title": "🔔 The close", "body": "\n".join(lines)[:600], "severity": "MEDIUM",
            "kind": "system", "tickers": [], "tag": f"close-{c['date']}", "ts": time.time(), "open": "/app/#a=brief"}  # fmt: skip


def similar_alerts(
    engine: Engine, alert: dict[str, Any], days: float = 180, limit: int = 12
) -> dict[str, Any]:
    """Earlier alerts of the same kind on the same stock, with how each was graded."""
    sym = next(iter(alert.get("tickers") or []), None)
    if not sym:
        return {"rows": [], "stats": None}
    key = record_key(alert)
    rows, moves = [], []
    for a in engine.storage.alerts_for_ticker(
        sym, since=float(alert.get("created", time.time())) - days * 86400, limit=200
    ):
        if (
            a["id"] == alert.get("id")
            or a.get("created", 0) >= alert.get("created", 0)
            or record_key(a) != key
        ):
            continue
        g = ((a.get("edge") or {}).get("outcome") or {}).get(sym)
        rows.append({"id": a["id"], "title": a["title"], "created": a["created"], "grade": g})
        if g and g.get("sign") and g.get("close") is not None:
            moves.append(g["sign"] * g["close"])
    stats = (
        {
            "n": len(moves),
            "hit": round(sum(m > 0 for m in moves) / len(moves), 2),
            "median": round(st.median(moves), 2),
        }
        if moves
        else None
    )
    return {"symbol": sym, "key": key, "rows": rows[:limit], "stats": stats}


# --------------------------------------------------------------------------- portfolio, tomorrow, rule preview


def portfolio(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    """This phone's holdings, live: value, today's P&L in dollars, unrealized P&L against cost,
    exposure by sector, and the biggest contributors today."""
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    rows: list[dict[str, Any]] = []
    sectors_: dict[str, float] = {}
    total = day = unreal = cost_basis = 0.0
    for sym, h in (p.get("holdings") or {}).items():
        q = engine.price_of(sym, 6 * 3600, now=now)
        li = engine.universe.get(sym)
        price = q[1] if q else None
        chg = q[2] if q else None
        value = price * h["shares"] if price else None
        dpnl = value - value / (1 + chg / 100) if value is not None and chg is not None else None
        upnl = (price - h["cost"]) * h["shares"] if price and h.get("cost") else None
        rows.append({"symbol": sym, "name": li.name if li else "", "shares": h["shares"], "cost": h.get("cost"), "price": price,
                     "chg": round(chg, 2) if chg is not None else None, "value": round(value, 2) if value is not None else None,
                     "day_pnl": round(dpnl, 2) if dpnl is not None else None, "unreal": round(upnl, 2) if upnl is not None else None,
                     "unreal_pct": round((price / h["cost"] - 1) * 100, 2) if price and h.get("cost") else None,
                     "sector": (li.sector if li else "") or "Other",
                     "news": next(({"title": w["title"], "url": w["url"], "source": w["source"], "age": w["age"]}
                                   for w in engine.find_catalysts([sym], now, overnight=True)), None)})  # fmt: skip
        if value is not None:
            total += value
            sectors_[rows[-1]["sector"]] = sectors_.get(rows[-1]["sector"], 0.0) + value
            if dpnl is not None:
                day += dpnl
            if upnl is not None:
                unreal += upnl
                cost_basis += h["cost"] * h["shares"]
    rows.sort(key=lambda r: -(r["value"] or 0))
    for r in rows:
        r["weight"] = round(r["value"] / total * 100, 1) if total and r["value"] is not None else None
    movers = sorted((r for r in rows if r["day_pnl"] is not None), key=lambda r: -abs(r["day_pnl"]))[:3]
    prev = total - day
    return {
        "rows": rows, "value": round(total, 2), "day_pnl": round(day, 2), "day_pct": round(day / prev * 100, 2) if prev else None,
        "unreal": round(unreal, 2) if cost_basis else None, "unreal_pct": round(unreal / cost_basis * 100, 2) if cost_basis else None,
        "sectors": sorted(({"sector": k, "weight": round(v / total * 100, 1)} for k, v in sectors_.items()), key=lambda r: -r["weight"]) if total else [],
        "contributors": [{"symbol": r["symbol"], "day_pnl": r["day_pnl"]} for r in movers],
    }  # fmt: skip


def tomorrow(engine: Engine, device: str = "", now: float | None = None) -> dict[str, Any]:
    """The next session, the evening before: after-hours movers with their reasons, the companies
    worth $2B+ reporting that day (this phone's first) and the scheduled events."""
    now = now or time.time()
    nxt = datetime.fromtimestamp(session_closes(now, 1, engine.calendar.holidays)[0], ET).date()
    if datetime.fromtimestamp(now, ET).hour < 16 and datetime.fromtimestamp(now, ET).date() == nxt:
        nxt = datetime.fromtimestamp(session_closes(now + 86400, 1, engine.calendar.holidays)[0], ET).date()
    g = gaps(engine, now, max_age_s=4 * 3600, limit=5)
    p = engine.desk.profiles.get(device) or {}
    mine = set(p.get("watchlist") or []) | set(p.get("holdings") or {})
    reports = []
    book = getattr(engine.trends, "earnings", None) if engine.trends is not None else None
    if book is not None:
        for sym in list(book.next):
            e = book.lookup(sym)
            if not e or e["date"] != nxt.isoformat():
                continue
            li = engine.universe.get(sym)
            if sym in mine or (li is not None and (li.market_cap or 0) >= 2e9):
                reports.append(
                    {
                        "symbol": sym,
                        "when": e.get("when") or "",
                        "mine": sym in mine,
                        "mcap": (li.market_cap if li else 0) or 0,
                    }
                )
    reports.sort(key=lambda r: (not r["mine"], -r["mcap"]))
    for r in reports:
        r.pop("mcap")
    events = [{"title": e["title"], "time": e.get("time"), "impact": e.get("impact") or 1} for e in engine.calendar.upcoming(days=5, now=now)
              if e["date"] == nxt.isoformat() and (e.get("impact") or 1) >= 2]  # fmt: skip
    return {
        "date": nxt.isoformat(),
        "movers": g["up"][:4] + g["down"][:4],
        "reports": reports[:20],
        "events": events,
    }


def tomorrow_push(t: dict[str, Any]) -> dict[str, Any] | None:
    lines = []
    for r in t["movers"][:3]:
        lines.append(
            f"{r['symbol']} {r['chg']:+.1f}%" + (f": {r['why']['title'][:80]}" if r.get("why") else "")
        )
    if t["reports"]:
        lines.append(
            "Reporting: " + ", ".join(("★" if r["mine"] else "") + r["symbol"] for r in t["reports"][:8])
        )
    for e in t["events"][:2]:
        lines.append(f"{e['time'] + ' ET: ' if e.get('time') else ''}{e['title'][:70]}")
    if not lines:
        return None
    day = datetime.fromisoformat(t["date"]).strftime("%A")
    return {"id": f"tm-{t['date']}", "title": f"🌙 {day}: what to know", "body": "\n".join(lines)[:600], "severity": "MEDIUM",
            "kind": "system", "tickers": [], "tag": f"tm-{t['date']}", "ts": time.time(), "open": "/app/#a=brief"}  # fmt: skip


def rule_preview(
    engine: Engine, device: str, rule: dict[str, Any], now: float | None = None, days: int = 30
) -> dict[str, Any]:
    """How often a smart alert would have fired over the last ``days`` days. Headline rules are
    replayed against stored headlines; the move in "headline, then a move" is checked on the
    headlines that became graded alerts (their move an hour later); tape rules (volume, VWAP, gaps)
    need prices the server didn't keep, so they aren't replayed."""
    now = now or time.time()
    kind, v = rule.get("kind"), float(rule.get("value") or 0)
    p = engine.desk.profiles.get(device) or {}
    syms = (
        (p.get("watchlist") or [])[:60]
        if rule.get("symbol") == "*"
        else [str(rule.get("symbol") or "").upper()]
    )
    if kind not in ("headline", "news_move"):
        return {
            "replayable": False,
            "why": "Tape rules (volume, VWAP, gaps) need intraday prices the server doesn't keep, so they can't be replayed.",
        }
    since = now - days * 86400
    heads = 0
    for sym in syms:
        heads += len(
            engine.storage.items_for_ticker(
                sym, since=since, limit=500, min_score=v if kind == "headline" else 20
            )
        )
    out: dict[str, Any] = {
        "replayable": True,
        "days": days,
        "headlines": heads,
        "per_week": round(heads / days * 7, 1),
    }
    if kind == "news_move":
        graded = moved = 0
        for sym in syms:
            for a in engine.storage.alerts_for_ticker(sym, since=since, limit=300):
                row = ((a.get("edge") or {}).get("outcome") or {}).get(sym)
                if row and row.get("h1") is not None:
                    graded += 1
                    moved += abs(row["h1"]) >= v
        out.update({"graded": graded, "moved": moved})
    return out


# --------------------------------------------------------------------------- market memory


def memory_snapshot(engine: Engine, now: float | None = None) -> dict[str, Any] | None:
    """The session in a few numbers, kept every day (settings ``memory:<date>``) so the history builds."""
    now = now or time.time()
    c = close_recap(engine, now)
    b = c["breadth"]
    if not c["indexes"] and not b.get("n"):
        return None
    secs = sectors(engine, now, max_age_s=6 * 3600)["rows"]
    return {"date": c["date"], "indexes": c["indexes"], "pct_up": b.get("pct_up"), "n": b.get("n"), "cap_weighted": b.get("cap_weighted"),
            "regime": c["regime"]["label"], "sectors": {r["sector"]: r["chg"] for r in secs}}  # fmt: skip


def market_memory(engine: Engine, days: int = 10) -> dict[str, Any]:
    rows = engine.storage.db.execute(
        "SELECT value FROM settings WHERE key LIKE 'memory:%' ORDER BY key DESC LIMIT ?", (days,)
    ).fetchall()
    snaps = [json.loads(r[0]) for r in rows]
    # each sector's run over the window: how many of these sessions it rose, and its total
    trend: dict[str, list[float]] = {}
    for s in snaps:
        for k, v in (s.get("sectors") or {}).items():
            trend.setdefault(k, []).append(v)
    sector_runs = sorted(({"sector": k, "sessions": len(v), "up": sum(1 for x in v if x > 0), "sum": round(sum(v), 2)} for k, v in trend.items()),
                         key=lambda r: -r["sum"])  # fmt: skip
    return {"days": snaps, "sectors": sector_runs}
