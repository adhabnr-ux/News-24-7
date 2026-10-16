"""What each phone gets on its own: its stocks in the morning Brief, its end-of-day report card,
and (for any stock) the move the options market is pricing.

* **Your stocks in the Brief**: for each watched stock, the pre-market (or live) move, the
  overnight headline behind it, an earnings report or calendar event in the next two days, and how
  many alerts it drew since the last close. The pushed Brief gets one line about them.
* **The report card** (16:05 ET on market days, pushed once): how the watchlist did (up/down,
  best, worst), the open tracked calls' P&L, the price alerts and stops/targets that fired today,
  and how Foretape's own alerts on those stocks did by the close.
* **The implied move**: the at-the-money straddle for the next few expiries, read from the live
  option chain on demand (cached 10 minutes): "the options market prices about ±6.1% by Friday".
  An earnings date before an expiry is flagged, since that's what the premium is paying for.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .edge import Calendar

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
IMPLIED_TTL_S = 600.0


# --------------------------------------------------------------------------- the implied move


def straddles(
    chain: Any, today: date, price: float | None = None, expiries: int = 4, max_dte: int = 60
) -> list[dict[str, Any]]:
    """The at-the-money straddle (call + put at the strike nearest the stock) for the next few
    expiries: what the options market prices as the move by each date. Mid prices when there is a
    two-sided quote, else the last trade; an expiry with neither side priced is skipped."""
    px = price or chain.price
    if not px:
        return []
    by_exp: dict[date, dict[float, dict[str, Any]]] = {}
    for c in chain.contracts:
        if c.expiry < today or (c.expiry - today).days > max_dte:
            continue
        by_exp.setdefault(c.expiry, {}).setdefault(c.strike, {})[c.cp] = c
    rows = []
    for exp in sorted(by_exp)[: expiries + 2]:
        pairs = {k: v for k, v in by_exp[exp].items() if "C" in v and "P" in v}
        if not pairs:
            continue
        strike = min(pairs, key=lambda k: abs(k - px))
        call, put = pairs[strike]["C"], pairs[strike]["P"]
        cv, pv = call.mid or call.last, put.mid or put.last
        if not cv or not pv:
            continue
        value = cv + pv
        rows.append({
            "expiry": exp.isoformat(), "dte": (exp - today).days, "strike": strike,
            "straddle": round(value, 2), "move_pct": round(value / px * 100.0, 2),
            "low": round(px - value, 2), "high": round(px + value, 2),
            "quoted": bool(call.mid and put.mid),
        })  # fmt: skip
        if len(rows) >= expiries:
            break
    return rows


async def implied_move(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    cache = engine.implied_cache
    hit = cache.get(sym)
    if hit and now - hit[0] < IMPLIED_TTL_S:
        return hit[1]
    feed = engine.options_feed
    if feed is None:
        return {"symbol": sym, "available": False, "why": "The options feed is off on this server."}
    try:
        chain = await feed.fetch_chain(sym)
    except Exception as exc:  # noqa: BLE001 - a failed read is an answer, not a crash
        return {"symbol": sym, "available": False, "why": f"Couldn't read the chain: {type(exc).__name__}"}
    if chain is None:
        out: dict[str, Any] = {"symbol": sym, "available": False, "why": "No listed options."}
    else:
        q = engine.price_of(sym, 1800, now=now)
        price = (q[1] if q else None) or chain.price
        today = datetime.fromtimestamp(now, ET).date()
        rows = straddles(chain, today, price)
        earn = None
        book = getattr(engine.trends, "earnings", None) if engine.trends is not None else None
        if book is not None:
            earn = book.lookup(sym, today)
        for r in rows:
            r["earnings"] = bool(earn and earn["date"] <= r["expiry"])
        out = {
            "symbol": sym, "available": bool(rows), "price": price, "rows": rows, "earnings": earn,
            "iv30": round(chain.iv30 * 100, 1) if chain.iv30 else None, "source": chain.source,
            "why": "" if rows else "No expiry in the next 60 days has both sides priced.",
        }  # fmt: skip
    cache[sym] = (now, out)
    if len(cache) > 300:
        for k, _ in sorted(cache.items(), key=lambda kv: kv[1][0])[:100]:
            del cache[k]
    return out


# --------------------------------------------------------------------------- your stocks in the Brief


def your_stocks(engine: Engine, device: str, now: float | None = None) -> list[dict[str, Any]]:
    """Each watched stock's morning: its move, the overnight headline, what's on its calendar."""
    now = now or time.time()
    p = engine.desk.profiles.get(device)
    if not p or not p.get("watchlist"):
        return []
    since = Calendar.last_close(now, engine.calendar.holidays)
    today = datetime.fromtimestamp(now, ET).date()
    book = getattr(engine.trends, "earnings", None) if engine.trends is not None else None
    events = engine.calendar.upcoming(days=3, now=now)
    rows = []
    for sym in p["watchlist"]:
        q = engine.price_of(sym, 6 * 3600, now=now)
        news = engine.find_catalysts([sym], now, overnight=True)
        earn = book.lookup(sym, today) if book is not None else None
        cal = [
            e for e in events if str(e.get("symbol") or "").upper() == sym and (e.get("in_days") or 0) <= 2
        ]
        n = len(engine.storage.alerts_for_ticker(sym, since=since, limit=50))
        rows.append({
            "symbol": sym, "chg": round(q[2], 2) if q and q[2] is not None else None,
            "price": q[1] if q else None,
            "news": {"title": news[0]["title"], "source": news[0]["source"], "url": news[0]["url"]} if news else None,
            "earnings": earn if earn and earn["in_days"] <= 2 else None,
            "events": [{"title": e["title"], "date": e["date"], "time": e.get("time")} for e in cal][:2],
            "alerts": n,
        })  # fmt: skip
    # what matters first: news or an event, then the biggest movers
    rows.sort(key=lambda r: (not (r["news"] or r["earnings"] or r["events"]), -abs(r["chg"] or 0)))
    return rows


def your_stocks_line(rows: list[dict[str, Any]]) -> str:
    """One line for the pushed Brief: the watched stocks with news, a report or a real move."""
    bits = []
    for r in rows:
        tag = ""
        if r["earnings"]:
            tag = "reports " + (
                "today"
                if r["earnings"]["in_days"] == 0
                else "tomorrow"
                if r["earnings"]["in_days"] == 1
                else "in 2 days"
            )
        elif r["news"]:
            tag = "news"
        chg = f" {r['chg']:+.1f}%" if r["chg"] is not None and abs(r["chg"]) >= 1 else ""
        if tag or chg:
            bits.append(f"{r['symbol']}{chg}" + (f" ({tag})" if tag else ""))
    return ("★ Your stocks: " + ", ".join(bits[:5])) if bits else ""


# --------------------------------------------------------------------------- the report card


def report_card(engine: Engine, device: str, now: float | None = None) -> dict[str, Any] | None:
    """This phone's day: watchlist, tracked calls, what fired, and how Foretape's alerts on its
    stocks did by the close. None when the phone has no watchlist and no tracked calls."""
    now = now or time.time()
    p = engine.desk.profiles.get(device)
    if not p or not (p.get("watchlist") or p.get("tracked")):
        return None
    day_start = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    watch = []
    for sym in p.get("watchlist") or []:
        q = engine.price_of(sym, 6 * 3600, now=now)
        if q and q[2] is not None:
            watch.append({"symbol": sym, "chg": round(q[2], 2)})
    watch.sort(key=lambda r: -r["chg"])
    tracked = []
    for t in p.get("tracked") or []:
        q = engine.price_of(t["symbol"], 6 * 3600, now=now)
        if q and t.get("entry"):
            r = (q[1] / t["entry"] - 1.0) * 100.0
            tracked.append(
                {
                    "symbol": t["symbol"],
                    "direction": t["direction"],
                    "pnl_pct": round(r if t["direction"] == "long" else -r, 2),
                }
            )
    fired = [a for a in p.get("price_alerts") or [] if (a.get("fired") or 0) >= day_start]
    levels = [
        {"symbol": t["symbol"], "which": w}
        for t in p.get("tracked") or []
        for w in ("stop", "target")
        if (t.get(f"{w}_hit") or 0) >= day_start
    ]
    graded = right = 0
    mine = set(p.get("watchlist") or []) | {t["symbol"] for t in p.get("tracked") or []}
    for a in engine.storage.recent_alerts(500, since=day_start):
        out = (a.get("edge") or {}).get("outcome") or {}
        for sym, row in out.items():
            if sym in mine and row.get("close") is not None and row.get("sign"):
                graded += 1
                right += row["sign"] * row["close"] > 0
                break
    return {
        "date": datetime.fromtimestamp(now, ET).date().isoformat(),
        "watch": {"n": len(watch), "up": sum(1 for r in watch if r["chg"] > 0),
                  "best": watch[0] if watch else None, "worst": watch[-1] if len(watch) > 1 else None, "rows": watch},
        "tracked": {"n": len(tracked), "avg": round(sum(t["pnl_pct"] for t in tracked) / len(tracked), 2) if tracked else None, "rows": tracked},
        "fired": [{"symbol": a["symbol"], "kind": a["kind"], "value": a["value"]} for a in fired],
        "levels": levels,
        "alerts": {"graded": graded, "right": right},
    }  # fmt: skip


def report_push(card: dict[str, Any]) -> dict[str, Any] | None:
    """The report card as one notification; None when there's nothing to say."""
    w, t = card["watch"], card["tracked"]
    lines = []
    if w["n"]:
        line = f"Watchlist: {w['up']} of {w['n']} up"
        if w["best"]:
            line += f" · best {w['best']['symbol']} {w['best']['chg']:+.1f}%"
        if w["worst"]:
            line += f" · worst {w['worst']['symbol']} {w['worst']['chg']:+.1f}%"
        lines.append(line)
    if t["n"]:
        lines.append(f"Tracked calls: {t['n']} open, avg {t['avg']:+.1f}%")
    if card["fired"] or card["levels"]:
        lines.append(
            f"Fired today: {len(card['fired'])} price alert(s), {len(card['levels'])} stop/target(s)"
        )
    if card["alerts"]["graded"]:
        a = card["alerts"]
        lines.append(
            f"Foretape on your stocks: {a['right']} of {a['graded']} went the alert's way by the close"
        )
    if not lines:
        return None
    return {
        "id": f"eod-{card['date']}", "title": "📋 Your day", "body": "\n".join(lines)[:600], "severity": "MEDIUM",
        "kind": "system", "tickers": [], "tag": f"eod-{card['date']}", "ts": time.time(), "open": "/app/#a=desk",
    }  # fmt: skip


# --------------------------------------------------------------------------- earnings reactions

SURPRISE_URL = "https://api.nasdaq.com/api/company/{symbol}/earnings-surprise"
EARNINGS_TTL_S = 12 * 3600.0


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace("$", "").replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_surprises(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Nasdaq's earnings-surprise table: one row per reported quarter, newest first."""
    rows = ((((data or {}).get("data") or {}).get("earningsSurpriseTable") or {}).get("rows")) or []
    out = []
    for r in rows:
        try:
            d = datetime.strptime(str(r.get("dateReported", "")).strip(), "%m/%d/%Y").date()
        except ValueError:
            continue
        out.append({
            "date": d.isoformat(), "quarter": str(r.get("fiscalQtrEnd") or ""),
            "eps": _num(r.get("eps")), "consensus": _num(r.get("consensusForecast")),
            "surprise_pct": _num(r.get("percentageSurprise")),
        })  # fmt: skip
    return sorted(out, key=lambda x: x["date"], reverse=True)


def reactions(reports: list[dict[str, Any]], bars: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """Each report's two-day move: the close before the report day to the close after it. That
    window holds the reaction whether the report came before the open or after the close."""
    days = [(datetime.fromtimestamp(t, ET).date().isoformat(), p) for t, p in bars]
    out = []
    for r in reports:
        before = [p for d, p in days if d < r["date"]]
        after = [p for d, p in days if d > r["date"]]
        move = round((after[0] / before[-1] - 1.0) * 100.0, 2) if before and after and before[-1] else None
        out.append({**r, "move_pct": move})
    return out


async def earnings_reactions(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any]:
    """How the stock moved on its last reports (Nasdaq's surprise table + Yahoo daily closes),
    against what options price in for the next one (when the implied move was read)."""
    from .camillo import NASDAQ_HEADERS
    from .market.prices import CHART_URL, parse_chart

    now = now or time.time()
    hit = engine.earnings_cache.get(sym)
    if hit and now - hit[0] < EARNINGS_TTL_S:
        return hit[1]
    try:
        resp = await engine.http.get(SURPRISE_URL.format(symbol=sym), headers=NASDAQ_HEADERS, timeout_s=20)
        reports = parse_surprises(resp.json())
    except Exception as exc:  # noqa: BLE001 - an answer, not a crash
        return {
            "symbol": sym,
            "available": False,
            "why": f"Nasdaq's earnings table didn't answer ({type(exc).__name__}).",
        }
    if not reports:
        out: dict[str, Any] = {"symbol": sym, "available": False, "why": "No reported quarters on Nasdaq."}
    else:
        try:
            resp = await engine.http.get(
                CHART_URL.format(symbol=sym), params={"range": "2y", "interval": "1d"}, timeout_s=20
            )
            bars = (parse_chart(resp.json()) or {}).get("points") or []
        except Exception:  # noqa: BLE001 - the table alone is still worth showing
            bars = []
        rows = reactions(reports, bars)
        moves = [abs(r["move_pct"]) for r in rows if r["move_pct"] is not None]
        beats = [r for r in rows if r["surprise_pct"] is not None]
        out = {
            "symbol": sym, "available": True, "rows": rows,
            "avg_abs_move": round(sum(moves) / len(moves), 2) if moves else None,
            "beat": sum(1 for r in beats if r["surprise_pct"] > 0), "reported": len(beats),
            "next": None,
        }  # fmt: skip
        book = getattr(engine.trends, "earnings", None) if engine.trends is not None else None
        if book is not None:
            out["next"] = book.lookup(sym)
        implied = (engine.implied_cache.get(sym) or (0, {}))[1]
        nxt = next((r for r in implied.get("rows") or [] if r.get("earnings")), None)
        if nxt and out["avg_abs_move"]:
            out["implied"] = {"expiry": nxt["expiry"], "move_pct": nxt["move_pct"],
                              "ratio": round(nxt["move_pct"] / out["avg_abs_move"], 2)}  # fmt: skip
    engine.earnings_cache[sym] = (now, out)
    return out


# --------------------------------------------------------------------------- the calendar feed


def _ics_text(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode())
            cur = b""
        cur += b
    parts.append(cur.decode())
    return "\r\n ".join(parts)


def calendar_ics(engine: Engine, device: str = "", now: float | None = None, days: int = 60) -> str:
    """The catalyst calendar (and this phone's watchlist earnings dates) as an iCalendar feed that
    Apple, Google and Outlook calendars can subscribe to."""
    import hashlib

    now = now or time.time()
    stamp = datetime.fromtimestamp(now, ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//News247//Foretape//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Foretape catalysts", "X-WR-TIMEZONE:America/New_York",
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]  # fmt: skip

    def event(uid_basis: str, day: str, hhmm: str | None, title: str, desc: str, url: str = "") -> None:
        uid = hashlib.sha1(uid_basis.encode()).hexdigest()[:20] + "@foretape"
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}"])
        d = date.fromisoformat(day)
        if hhmm and ":" in hhmm:
            h, m = (int(x) for x in hhmm.split(":")[:2])
            start = datetime(d.year, d.month, d.day, h, m, tzinfo=ET).astimezone(ZoneInfo("UTC"))
            end = datetime.fromtimestamp(start.timestamp() + 1800, ZoneInfo("UTC"))
            lines.extend([f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"])
        else:
            nxt = date.fromordinal(d.toordinal() + 1)
            lines.extend([f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{nxt:%Y%m%d}"])
        lines.extend([f"SUMMARY:{_ics_text(title)}", f"DESCRIPTION:{_ics_text(desc)}"])
        if url:
            lines.append(f"URL:{url}")
        lines.append("END:VEVENT")

    for e in engine.calendar.upcoming(days=days, now=now):
        t = str(e.get("time") or "")
        when = t if re.fullmatch(r"\d{1,2}:\d{2}", t) else None
        desc = " · ".join(
            x
            for x in (str(e.get("kind") or ""), str(e.get("note") or ""), f"impact {e.get('impact') or 1}/3")
            if x
        )
        event(f"cal|{e['date']}|{e['title']}", e["date"], when, e["title"], desc, str(e.get("url") or ""))
    p = engine.desk.profiles.get(device) if device else None
    book = getattr(engine.trends, "earnings", None) if engine.trends is not None else None
    if p and book is not None:
        for sym in p.get("watchlist") or []:
            nxt = book.lookup(sym)
            if nxt and nxt["in_days"] <= days:
                event(
                    f"earn|{sym}|{nxt['date']}",
                    nxt["date"],
                    None,
                    f"{sym} reports earnings",
                    f"{nxt.get('when') or ''} · on your Foretape watchlist",
                )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
