"""Fill the record from the exchange's daily bars.

Every grade at a session's close (the close of its day, the next session's, five and twenty sessions
on) is stamped from a live quote within three hours of that close. A server that was asleep then (a
free host that sleeps after a quiet spell, a restart, an outage) never stamps it, and the alert
drops out of every record that horizon feeds. Those closes are on file elsewhere: the exchange's
daily bars. A few times an hour, alerts 1-45 days old with a close horizon passed and not stamped
are filled from each stock's daily close on that session (and SPY's, for the market's side),
fetched from Alpaca's bars when the server has its keys, else Yahoo's daily chart. Each filled
horizon is listed on the row ("filled"), so a fill is never mistaken for a live stamp. The hour and
the minute checkpoints can't be filled from daily bars and stay as they are.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .dossier import session_closes

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
HORIZONS = (("close", 0), ("d1", 1), ("d5", 5), ("d20", 20))
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


def needs(engine: Engine, now: float | None = None, limit: int = 4000) -> dict[str, list[dict[str, Any]]]:
    """{symbol: [{"alert", "sym", "ref", "missing": {horizon: session date}}]} for alerts 1-45 days old
    with a close horizon passed (by a day, so a live stamp had its chance) and not stamped."""
    now = now or time.time()
    holidays = engine.calendar.holidays
    out: dict[str, list[dict[str, Any]]] = {}
    for a in engine.storage.alerts_between(now - 45 * 86400, now - 86400, limit):
        e = a.get("edge") or {}
        o = e.get("outcome") or {}
        refs = e.get("refs") or {}
        if not o or not refs:
            continue
        closes = session_closes(float(a["created"]), 21, holidays)
        for sym, row in list(o.items())[:3]:
            ref = refs.get(sym)
            if not ref or not row.get("sign"):
                continue
            missing = {}
            for h, i in HORIZONS:
                ts = closes[i]
                if row.get(h) is None and now - ts > 86400:
                    missing[h] = datetime.fromtimestamp(ts, ET).date().isoformat()
            if missing:
                out.setdefault(sym, []).append(
                    {"alert": a, "sym": sym, "ref": float(ref), "missing": missing}
                )
    return out


def parse_alpaca(doc: Any, sym: str) -> dict[str, float]:
    """Alpaca /v2/stocks/bars -> {date: close}."""
    rows = ((doc or {}).get("bars") or {}).get(sym.replace("-", ".")) or []
    out = {}
    for r in rows:
        t, c = r.get("t"), r.get("c")
        if t and c:
            out[datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(ET).date().isoformat()] = (
                float(c)
            )
    return out


def parse_yahoo(doc: Any) -> dict[str, float]:
    """Yahoo /v8/finance/chart (interval 1d) -> {date: close}."""
    res = (((doc or {}).get("chart") or {}).get("result") or [None])[0] or {}
    ts = res.get("timestamp") or []
    closes = ((((res.get("indicators") or {}).get("quote") or [{}])[0]) or {}).get("close") or []
    return {
        datetime.fromtimestamp(t, ET).date().isoformat(): float(c)
        for t, c in zip(ts, closes, strict=False)
        if c
    }


async def fetch(engine: Engine, sym: str, first: str, last: str) -> dict[str, float]:
    """The stock's daily closes from ``first`` to ``last`` (ISO dates), from whichever source is set up."""
    o = engine.cfg.options
    start, end = date.fromisoformat(first), date.fromisoformat(last) + timedelta(days=1)
    if o.alpaca_key and o.alpaca_secret:
        resp = await engine.http.get(
            o.alpaca_data_url.rstrip("/") + "/v2/stocks/bars",
            headers={"APCA-API-KEY-ID": o.alpaca_key, "APCA-API-SECRET-KEY": o.alpaca_secret},
            params={
                "symbols": sym.replace("-", "."),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": "raw",
                "feed": getattr(o, "alpaca_stock_feed", "iex"),
            },  # fmt: skip
            timeout_s=20,
        )
        return parse_alpaca(resp.json(), sym)
    feed = getattr(engine, "smallcap_feed", None)
    if feed is not None and hasattr(feed, "yahoo_json"):
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=ET).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, tzinfo=ET).timestamp())
        doc = await feed.yahoo_json(
            YAHOO_CHART.format(sym=sym), {"period1": str(p1), "period2": str(p2), "interval": "1d"}
        )
        return parse_yahoo(doc)
    return {}


async def fill(engine: Engine, now: float | None = None, max_symbols: int = 25) -> int:
    """Fill what the daily bars can; returns horizons filled."""
    now = now or time.time()
    todo = needs(engine, now)
    if not todo:
        return 0
    filled = 0
    spy: dict[str, float] | None = None
    for sym in sorted(todo, key=lambda s: -len(todo[s]))[:max_symbols]:
        jobs = todo[sym]
        dates = sorted(d for j in jobs for d in j["missing"].values())
        try:
            closes = await fetch(engine, sym, dates[0], dates[-1])
        except Exception:  # noqa: BLE001 - a failed fetch fills nothing; the next pass tries again
            continue
        if not closes:
            continue
        for j in jobs:
            a = engine.storage.get_alert(j["alert"]["id"]) or j["alert"]
            e = a.get("edge") or {}
            out = dict(e.get("outcome") or {})
            row = dict(out.get(sym) or {})
            got = [h for h, d in j["missing"].items() if row.get(h) is None and d in closes]
            if not got:
                continue
            for h in got:
                row[h] = round((closes[j["missing"][h]] / j["ref"] - 1) * 100, 2)
            row["filled"] = sorted(set(row.get("filled") or []) | set(got))
            out[sym] = row
            patch: dict[str, Any] = {"outcome": out}
            if e.get("mkt_ref"):
                if spy is None:
                    try:
                        spy = await fetch(
                            engine,
                            "SPY",
                            dates[0],
                            max(d for js in todo.values() for x in js for d in x["missing"].values()),
                        )
                    except Exception:  # noqa: BLE001
                        spy = {}
                mkt = dict(e.get("mkt_outcome") or {})
                for h in got:
                    d = j["missing"][h]
                    if mkt.get(h) is None and d in spy:
                        mkt[h] = round((spy[d] / float(e["mkt_ref"]) - 1) * 100, 2)
                patch["mkt_outcome"] = mkt
            engine.storage.update_alert_edge(a["id"], patch)
            filled += len(got)
    return filled


def coverage(engine: Engine, days: float = 30, now: float | None = None) -> dict[str, Any]:
    """How much of the record is graded at each horizon (alerts whose horizon has passed), and how
    much of that came from the daily bars."""
    now = now or time.time()
    holidays = engine.calendar.holidays
    tally = {h: {"due": 0, "graded": 0, "filled": 0} for h, _ in HORIZONS}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        if not e.get("refs") or a.get("kind") == "system":
            continue
        row = next(iter((e.get("outcome") or {}).values()), {}) or {}
        closes = session_closes(float(a["created"]), 21, holidays)
        for h, i in HORIZONS:
            if now - closes[i] > 86400:
                t = tally[h]
                t["due"] += 1
                if row.get(h) is not None:
                    t["graded"] += 1
                    if h in (row.get("filled") or []):
                        t["filled"] += 1
    return {"days": days, "horizons": [{"horizon": h, **t, "share": round(t["graded"] / t["due"], 2) if t["due"] else None}
                                       for h, t in tally.items()]}  # fmt: skip
