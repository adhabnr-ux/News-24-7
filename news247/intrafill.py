"""Fill the intraday record from the exchange's minute bars.

The daily bars (bars.py) fill the closes a sleeping server missed, but not the intraday stamps,
which are taken live, inside narrow windows:
- the news clock (5, 15 and 30 minutes in);
- the hour;
- the first print after the bell, for news from outside the session;
- each alert's heat (its best run and worst dip before the close);
- the market's hour;
- each peer's hour and close (peerlearn.py).
A server that slept through those minutes never stamped them, and every record they feed was missing
those alerts: the news clock, live odds, heat and patient entries, into the open, the call's clock
fact, sympathy learned. Those minutes are on file too, as one-minute bars.

Every half hour, alerts 4 hours to 25 days old whose session has closed are read for the intraday
stamps they lack. Each stamp is taken from the one-minute bars around its moment, on the live
stamp's clock: a bar ending within a minute before to 2.5 minutes after it. The bars come from
Alpaca's when the server has its keys, else Yahoo's one-minute chart, with pre- and post-market.
Every filled stamp is listed on its row ("filled"), and heat filled from bars is marked
("exc.filled"), so a fill is never mistaken for a live stamp. An alert whose bars can't be had is
tried twice, then left alone.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .bars import YAHOO_CHART
from .dossier import CHECKPOINTS, session_closes

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
STEP = 60  # one-minute bars
MAX_AGE_D = 25  # Yahoo keeps one-minute bars for about 30 days
MAX_SPAN_S = 4 * 86400  # one request covers at most four days of minutes
TRIES = 2

Bar = tuple[float, float, float, float, float]  # (start, open, high, low, close)


# --------------------------------------------------------------------------- reading bars


def parse_alpaca(doc: Any, sym: str) -> list[Bar]:
    """Alpaca /v2/stocks/bars (1Min) -> [(start, o, h, l, c)], oldest first."""
    rows = ((doc or {}).get("bars") or {}).get(sym.replace("-", ".")) or []
    out = []
    for r in rows:
        t = r.get("t")
        if not t or r.get("c") is None:
            continue
        ts = datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
        out.append((ts, float(r.get("o") or r["c"]), float(r.get("h") or r["c"]), float(r.get("l") or r["c"]),
                    float(r["c"])))  # fmt: skip
    return sorted(out)


def parse_yahoo(doc: Any) -> list[Bar]:
    """Yahoo /v8/finance/chart (interval 1m) -> [(start, o, h, l, c)], oldest first."""
    res = (((doc or {}).get("chart") or {}).get("result") or [None])[0] or {}
    ts = res.get("timestamp") or []
    q = (((res.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close")]
    out = []
    for i, t in enumerate(ts):
        o, h, lo, c = (col[i] if i < len(col) else None for col in cols)
        if c:
            out.append((float(t), float(o or c), float(h or c), float(lo or c), float(c)))
    return sorted(out)


def price_at(bars: list[Bar], ts: float, before: float = 60, after: float = 150) -> float | None:
    """The price at ``ts`` on the live stamp's clock: the close of the bar ending nearest to it, from
    a minute before to 2.5 minutes after."""
    best = None
    for b in bars:
        end = b[0] + STEP
        if ts - before <= end <= ts + after and (best is None or abs(end - ts) < abs(best[0] + STEP - ts)):
            best = b
    return best[4] if best else None


def first_print(bars: list[Bar], op: float) -> float | None:
    """The session's first print: the open of the first bar from 9:30, within five minutes."""
    for b in bars:
        if op <= b[0] <= op + 300:
            return b[1]
    return None


def heat(bars: list[Bar], ref: float, sign: int, start: float, end: float) -> dict[str, float] | None:
    """The best run and worst dip, its way, from the alert to the close."""
    inside = [b for b in bars if b[0] >= start - STEP and b[0] + STEP <= end + 1]
    if not inside or not ref:
        return None
    ups = [(b[2] / ref - 1) * 100 for b in inside]
    downs = [(b[3] / ref - 1) * 100 for b in inside]
    its = [sign * x for x in ups + downs]
    return {"mfe": round(max(its), 2), "mae": round(min(its), 2)}


# --------------------------------------------------------------------------- what's missing


def _todo(a: dict[str, Any], holidays: set[str]) -> dict[str, Any] | None:
    """What this alert's intraday record lacks: {"lead": (sym, ref, sign, keys), "heat", "mkt", "peers"}."""
    e = a.get("edge") or {}
    refs = e.get("refs") or {}
    out_ = e.get("outcome") or {}
    if not refs or not out_ or a.get("kind") == "system":
        return None
    if (e.get("intrafill") or {}).get("tries", 0) >= TRIES:
        return None
    created = float(a["created"])
    close_ts = session_closes(created, 1, holidays)[0]
    op = close_ts - 6.5 * 3600
    sym = next(iter(out_))
    row = out_[sym]
    ref = refs.get(sym)
    sign = row.get("sign") or 0
    if not ref:
        return None
    keys = [k for k, _ in CHECKPOINTS if row.get(k) is None]
    if row.get("h1") is None:
        keys.append("h1")
    if created < op and row.get("open") is None:
        keys.append("open")
    want: dict[str, Any] = {}
    if keys:
        want["lead"] = (sym, float(ref), sign, keys)
    if sign and created < close_ts and not e.get("exc"):
        want["heat"] = (sym, float(ref), sign)
    if e.get("mkt_ref") and (e.get("mkt_outcome") or {}).get("h1") is None:
        want["mkt"] = float(e["mkt_ref"])
    peers = {s: float(r) for s, r in (e.get("peer_refs") or {}).items()
             if r and not all(k in ((e.get("peer_out") or {}).get(s) or {}) for k in ("h1", "close"))}  # fmt: skip
    if peers:
        want["peers"] = peers
    if not want:
        return None
    return {"alert": a, "created": created, "close": close_ts, "open": op, **want}


def needs(engine: Engine, now: float | None = None, limit: int = 3000) -> list[dict[str, Any]]:
    """Alerts 4 hours to 25 days old, their session closed half an hour ago or more, with an
    intraday stamp missing."""
    now = now or time.time()
    holidays = engine.calendar.holidays
    out = []
    for a in engine.storage.alerts_between(now - MAX_AGE_D * 86400, now - 4 * 3600, limit):
        t = _todo(a, holidays)
        if t and now >= t["close"] + 1800:
            out.append(t)
    return out


# --------------------------------------------------------------------------- fetching


async def fetch(engine: Engine, sym: str, start: float, end: float) -> list[Bar]:
    """One-minute bars for ``sym`` from ``start`` to ``end`` (epoch seconds), pre- and post-market
    included, from whichever source is set up."""
    o = engine.cfg.options
    if o.alpaca_key and o.alpaca_secret:
        iso = lambda t: datetime.fromtimestamp(t, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        resp = await engine.http.get(
            o.alpaca_data_url.rstrip("/") + "/v2/stocks/bars",
            headers={"APCA-API-KEY-ID": o.alpaca_key, "APCA-API-SECRET-KEY": o.alpaca_secret},
            params={
                "symbols": sym.replace("-", "."),
                "timeframe": "1Min",
                "start": iso(start),
                "end": iso(end),
                "limit": "10000",
                "adjustment": "raw",
                "feed": getattr(o, "alpaca_stock_feed", "iex"),
            },  # fmt: skip
            timeout_s=20,
        )
        return parse_alpaca(resp.json(), sym)
    feed = getattr(engine, "smallcap_feed", None)
    if feed is not None and hasattr(feed, "yahoo_json"):
        doc = await feed.yahoo_json(
            YAHOO_CHART.format(sym=sym),
            {
                "period1": str(int(start)),
                "period2": str(int(end)),
                "interval": "1m",
                "includePrePost": "true",
            },
        )
        return parse_yahoo(doc)
    return []


def _windows(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge the time spans one symbol needs into requests of at most four days each."""
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + 6 * 3600 and e - out[-1][0] <= MAX_SPAN_S:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


class _Book:
    """Bars per symbol, fetched once per window and shared by every alert that needs them."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.got: dict[str, list[tuple[float, float, list[Bar]]]] = {}

    async def load(self, sym: str, spans: list[tuple[float, float]]) -> None:
        for s, e in _windows(spans):
            try:
                bars = await fetch(self.engine, sym, s, e)
            except Exception:  # noqa: BLE001 - a failed fetch fills nothing; the alert is tried again later
                bars = []
            self.got.setdefault(sym, []).append((s, e, bars))

    def bars(self, sym: str, start: float, end: float) -> list[Bar]:
        return [b for s, e, bars in self.got.get(sym, []) if s <= start and end <= e for b in bars]


# --------------------------------------------------------------------------- filling


async def fill(engine: Engine, now: float | None = None, max_symbols: int = 30) -> int:
    """Fill what the minute bars can; returns stamps filled."""
    now = now or time.time()
    todo = needs(engine, now)
    if not todo:
        return 0
    spans: dict[str, list[tuple[float, float]]] = {}
    for t in todo:
        span = (t["created"] - 120, max(t["close"], t["created"] + 3600) + 300)
        syms = []
        if "lead" in t or "heat" in t:
            syms.append((t.get("lead") or t["heat"])[0])
        if "mkt" in t:
            syms.append("SPY")
        syms += list(t.get("peers") or {})
        for s in syms:
            spans.setdefault(s, []).append(span)
    book = _Book(engine)
    chosen = sorted(spans, key=lambda s: (s != "SPY", -len(spans[s])))[:max_symbols]
    for sym in chosen:
        await book.load(sym, spans[sym])
    filled = 0
    for t in todo:
        a = t["alert"]
        e = (engine.storage.get_alert(a["id"]) or a).get("edge") or {}
        span = (t["created"] - 120, max(t["close"], t["created"] + 3600) + 300)
        patch: dict[str, Any] = {}
        n = 0
        if "lead" in t and t["lead"][0] in chosen:
            sym, ref, _sign, keys = t["lead"]
            bars = book.bars(sym, *span)
            out = dict(e.get("outcome") or {})
            row = dict(out.get(sym) or {})
            got = []
            for k in keys:
                if row.get(k) is not None:
                    continue
                if k == "open":
                    px = first_print(bars, t["open"])
                else:
                    at = t["created"] + (3600 if k == "h1" else dict(CHECKPOINTS)[k])
                    px = price_at(bars, at)
                if px:
                    row[k] = round((px / ref - 1) * 100, 2)
                    got.append(k)
            if got:
                row["filled"] = sorted(set(row.get("filled") or []) | set(got))
                out[sym] = row
                patch["outcome"] = out
                n += len(got)
        if "heat" in t and t["heat"][0] in chosen and not e.get("exc"):
            sym, ref, sign = t["heat"]
            h = heat(book.bars(sym, *span), ref, sign, t["created"], t["close"])
            if h:
                patch["exc"] = {**h, "filled": True}
                n += 1
        if "mkt" in t and "SPY" in chosen and (e.get("mkt_outcome") or {}).get("h1") is None:
            px = price_at(book.bars("SPY", *span), t["created"] + 3600)
            if px:
                patch["mkt_outcome"] = {
                    **(e.get("mkt_outcome") or {}),
                    "h1": round((px / t["mkt"] - 1) * 100, 2),
                }
                n += 1
        if t.get("peers"):
            po = {k: dict(v) for k, v in (e.get("peer_out") or {}).items()}
            for sym, ref in t["peers"].items():
                if sym not in chosen:
                    continue
                bars = book.bars(sym, *span)
                prow = po.setdefault(sym, {})
                got = []
                for k, at in (("h1", t["created"] + 3600), ("close", t["close"])):
                    if k in prow:
                        continue
                    px = price_at(bars, at, before=300 if k == "close" else 60)
                    if px:
                        prow[k] = round((px / ref - 1) * 100, 2)
                        got.append(k)
                if got:
                    prow["filled"] = sorted(set(prow.get("filled") or []) | set(got))
                    n += len(got)
            if any(v.get("filled") for v in po.values()):
                patch["peer_out"] = {k: v for k, v in po.items() if v}
        needed = {
            (t.get("lead") or t.get("heat") or ("",))[0],
            *(["SPY"] if "mkt" in t else []),
            *(t.get("peers") or {}),
        }
        if needed & set(chosen):
            patch["intrafill"] = {"ts": now, "tries": (e.get("intrafill") or {}).get("tries", 0) + 1}
        if patch:
            engine.storage.update_alert_edge(a["id"], patch)
        filled += n
    return filled


# --------------------------------------------------------------------------- coverage


def coverage(engine: Engine, days: float = 30, now: float | None = None) -> list[dict[str, Any]]:
    """How much of the intraday record is graded (the hour, the news clock at 15 minutes, the first
    print for news from outside the session, heat), and how much came from minute bars."""
    now = now or time.time()
    holidays = engine.calendar.holidays
    tally = {h: {"due": 0, "graded": 0, "filled": 0} for h in ("h1", "m15", "open", "heat")}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        if not e.get("refs") or a.get("kind") == "system":
            continue
        created = float(a["created"])
        close_ts = session_closes(created, 1, holidays)[0]
        if now < close_ts + 1800:
            continue
        row = next(iter((e.get("outcome") or {}).values()), {}) or {}
        filled = row.get("filled") or []
        due = ["h1", "m15"]
        if created < close_ts - 6.5 * 3600:
            due.append("open")
        if row.get("sign") and created < close_ts:
            due.append("heat")
        for h in due:
            t = tally[h]
            t["due"] += 1
            if h == "heat":
                if e.get("exc"):
                    t["graded"] += 1
                    t["filled"] += 1 if (e["exc"] or {}).get("filled") else 0
            elif row.get(h) is not None:
                t["graded"] += 1
                t["filled"] += 1 if h in filled else 0
    return [{"horizon": h, **t, "share": round(t["graded"] / t["due"], 2) if t["due"] else None}
            for h, t in tally.items()]  # fmt: skip
