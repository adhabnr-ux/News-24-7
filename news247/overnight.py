"""Overnight: hold the call into tomorrow, or close it before the bell?

Every graded alert has its move at its session's close and at the next session's close, so the
night in between is on record: from the close to the next close, its way. Pooled by kind of news
and by where the alert stood at the close (1%+ against it, flat, up 1-3% its way, 3%+ its way),
the record answers the end-of-day question for an open paper call: "contract wins up 1-3% by the
close added +0.4% by the next close, 61% of the time (44)". The most specific row with 8+ graded is
used (the kind at that standing, else any news at that standing).

At 15:40-15:55 ET each phone with open calls gets one push: each call's standing, its row, a read
(hold when 55%+ went on with a positive median, close when 45% or fewer did or the median is
negative, else either way), and a warning when the stock reports tonight or tomorrow morning (a gap
can go straight through a stop). Paper only; history, not a forecast.
"""

from __future__ import annotations

import json
import statistics as st
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 8
STANDING = (
    ("against", "1%+ against it"),
    ("flat", "within 1%"),
    ("up", "up 1-3% its way"),
    ("big", "up 3%+ its way"),
)
STANDING_LABEL = dict(STANDING)


def standing(x: float) -> str:
    return "against" if x <= -1 else "flat" if x < 1 else "up" if x < 3 else "big"


def night(row: dict[str, Any]) -> tuple[float, float] | None:
    """(where it stood at the close, the move from the close to the next close), both its way."""
    if not row.get("sign") or row.get("close") is None or row.get("d1") is None:
        return None
    s = row["sign"]
    return round(s * row["close"], 2), round(
        s * ((1 + row["d1"] / 100) / (1 + row["close"] / 100) - 1) * 100, 2
    )


def overnight_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"kind|standing": {"key", "kind", "standing", "label", "n", "hit", "median"}}."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[tuple[str, str], list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        nt = night(next(iter(o.values())))
        if nt is None:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            pools.setdefault((kind, standing(nt[0])), []).append(nt[1])
    out = {}
    for (kind, sd), xs in pools.items():
        if len(xs) < MIN_N:
            continue
        who = "News" if kind == "*" else LABELS.get(kind, kind)
        out[f"{kind}|{sd}"] = {"key": f"{kind}|{sd}", "kind": kind, "standing": sd, "label": f"{who} {STANDING_LABEL[sd]} at the close",
                               "n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)}  # fmt: skip
    return out


def overnight_for(rates: dict[str, Any], kind: str | None, its: float) -> dict[str, Any] | None:
    sd = standing(its)
    for k in (f"{kind}|{sd}" if kind else None, f"*|{sd}"):
        if k and k in rates:
            return rates[k]
    return None


def read_of(r: dict[str, Any] | None) -> str:
    if not r:
        return "no record"
    if r["hit"] >= 0.55 and r["median"] > 0:
        return "hold"
    if r["hit"] <= 0.45 or r["median"] < 0:
        return "close"
    return "either"


def calls_tonight(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    """Each open call's standing, the record for it, the read, and report risk tonight or tomorrow."""
    from .catalysts import classify

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    rates = getattr(engine, "overnight_rates", None) or {}
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    today = datetime.fromtimestamp(now, ET).date()
    rows = []
    for t in p.get("tracked") or []:
        sym, sign = t["symbol"], 1 if t.get("direction", "long") == "long" else -1
        q = engine.price_of(sym, 1800, now=now)
        if not q or not t.get("entry"):
            continue
        pnl = round(sign * (q[1] / t["entry"] - 1) * 100, 2)
        a = engine.storage.get_alert(t["alert_id"]) if t.get("alert_id") else None
        kind, its = None, pnl
        if a:
            c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
            kind = c["key"] if c else None
            ref = ((a.get("edge") or {}).get("refs") or {}).get(sym)
            if ref:
                its = round(sign * (q[1] / ref - 1) * 100, 2)  # where the alert stands, the call's way
        r = overnight_for(rates, kind, its)
        er = book.lookup(sym, today) if book is not None else None
        report = None
        if (
            er
            and er.get("in_days") is not None
            and (er["in_days"] == 1 or (er["in_days"] == 0 and "after" in str(er.get("when") or "").lower()))
        ):
            report = "tonight" if er["in_days"] == 0 else "tomorrow"
        rows.append({"id": t["id"], "symbol": sym, "direction": t.get("direction", "long"), "pnl": pnl, "its": its,
                     "standing": standing(its), "record": r, "read": read_of(r), "report": report})  # fmt: skip
    return {"rows": rows, "need": MIN_N}


def line(r: dict[str, Any]) -> str:
    rec = r["record"]
    head = f"{r['symbol']} {r['pnl']:+.1f}%: {r['read'] if r['read'] != 'no record' else 'no record yet'}"
    if rec:
        head += f" · {rec['label'].lower()} went on its way by the next close {round(rec['hit'] * 100)}% (median {rec['median']:+.1f}%, {rec['n']})"
    if r["report"]:
        head += f" · ⚠ reports {r['report']}: a gap can go through a stop"
    return head


async def send_overnight(engine: Engine, now: float | None = None) -> int:
    """15:40-15:55 ET on market days: each phone with open calls, once a day."""
    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    if dt.weekday() >= 5 or dt.date().isoformat() in getattr(engine.calendar, "holidays", set()):
        return 0
    if not ((15, 40) <= (dt.hour, dt.minute) < (15, 55)) or engine.webpush is None:
        return 0
    key = f"overnight:{dt.date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for dev, p in list(engine.desk.profiles.items()):
        if dev in done or not p.get("tracked") or not (p.get("prefs") or {}).get("overnight", True):
            continue
        rows = calls_tonight(engine, dev, now)["rows"]
        if not rows:
            continue
        done.add(dev)
        holds = sum(r["read"] == "hold" for r in rows)
        closes = sum(r["read"] == "close" for r in rows)
        title = f"🌙 Into the close: {len(rows)} open call{'s' if len(rows) != 1 else ''}" + (
            f" · {holds} hold, {closes} close" if holds or closes else ""
        )
        payload = {"id": f"on-{dt.date()}-{dev[-6:]}", "title": title, "body": "\n".join(line(r) for r in rows[:5])[:600],
                   "severity": "MEDIUM", "kind": "system", "tickers": [r["symbol"] for r in rows[:5]], "tag": "overnight",
                   "ts": now, "open": "/app/#a=desk"}  # fmt: skip
        if await engine._to_device(dev, payload, urgency="normal"):
            sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent
