"""The uptime ledger: when was the desk asleep during market hours, and what did it miss?

A free host sleeps after a quiet spell; a deploy or an outage restarts the server. While it's down
nothing is read, so news in that window wasn't alerted live, and the records quietly have a hole.
The engine now notes a heartbeat every minute. On start, the gap since the last one is measured,
and the part of it that fell in the regular session (9:30-16:00 ET on market days) is kept as an
outage. Then:

- **A push on waking** (to every phone) when it slept through 10+ minutes of a session: when, how
  long, and which of the session's big movers (5%+, $300M+) the tape shows now, so the gap is
  known instead of silent.
- **The ledger** (the Desk): the share of market hours awake over the last 20 sessions, and every
  outage with its length.
- **What we missed** (recall.py) marks a miss whose move fell while the desk was asleep, so a
  sleep isn't mistaken for a blind spot.

The fix for sleeping is outside the app (a paid instance, or an uptime pinger on the free one);
this makes the cost visible. Nothing is estimated: the gaps are the server's own heartbeats.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
BEAT_KEY = "uptime:beat"
LOG_KEY = "uptime:outages"
MIN_GAP_S = 180  # shorter gaps are restarts, not sleep
PUSH_MIN_S = 600


def beat(engine: Engine, now: float | None = None) -> None:
    engine.storage.set_setting(BEAT_KEY, str(now or time.time()))


def session_overlap(start: float, end: float, holidays: set[str] | None = None) -> float:
    """Seconds of [start, end) that fell in regular sessions (9:30-16:00 ET, market days)."""
    hol = holidays or set()
    total = 0.0
    d = datetime.fromtimestamp(start, ET).date()
    last = datetime.fromtimestamp(end, ET).date()
    while d <= last:
        if d.weekday() < 5 and d.isoformat() not in hol:
            o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).timestamp()
            c = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).timestamp()
            total += max(0.0, min(end, c) - max(start, o))
        d += timedelta(days=1)
    return total


def on_start(engine: Engine, now: float | None = None) -> dict[str, Any] | None:
    """Measure the gap since the last heartbeat; keep it as an outage when it's longer than a restart."""
    now = now or time.time()
    raw = engine.storage.get_setting(BEAT_KEY)
    beat(engine, now)
    if not raw:
        return None
    last = float(raw)
    gap = now - last
    if gap < MIN_GAP_S:
        return None
    market = session_overlap(last, now, engine.calendar.holidays)
    out = {"from": last, "to": now, "seconds": round(gap), "market_s": round(market)}
    log = json.loads(engine.storage.get_setting(LOG_KEY) or "[]")
    log.append(out)
    engine.storage.set_setting(LOG_KEY, json.dumps(log[-200:]))
    return out


def asleep_at(engine: Engine, ts: float) -> bool:
    """Was the desk down at this moment (inside a kept outage)?"""
    return any(o["from"] <= ts <= o["to"] for o in json.loads(engine.storage.get_setting(LOG_KEY) or "[]"))


async def wake_push(engine: Engine, outage: dict[str, Any] | None, now: float | None = None) -> int:
    """Tell every phone when the desk slept through part of a session, with the movers it may have missed."""
    if not outage or outage["market_s"] < PUSH_MIN_S or engine.webpush is None:
        return 0
    from .recall import movers

    now = now or time.time()
    a, b = datetime.fromtimestamp(outage["from"], ET), datetime.fromtimestamp(outage["to"], ET)
    mins = round(outage["market_s"] / 60)
    big = [m for m in movers(engine, now) if not m["caught"]][:4]
    body = f"Asleep {a:%a %H:%M}–{b:%H:%M} ET: {mins} min of the session went unread."
    if big:
        body += " Moving now with no alert: " + ", ".join(f"{m['symbol']} {m['chg']:+.1f}%" for m in big)
    payload = {"id": f"wake-{int(outage['to'])}", "title": "😴 Foretape was asleep during the session", "body": body[:500],
               "severity": "HIGH", "kind": "system", "tickers": [m["symbol"] for m in big], "tag": "wake", "ts": now,
               "open": "/app/#a=desk"}  # fmt: skip
    sent = 0
    devices = {sub.device for sub in engine.webpush.subs.values() if sub.device}  # type: ignore[attr-defined]
    for dev in sorted(devices):
        if await engine._to_device(dev, payload, urgency="normal"):
            sent += 1
    return sent


def ledger(engine: Engine, sessions: int = 20, now: float | None = None) -> dict[str, Any]:
    """The share of market hours awake over the last ``sessions`` sessions, and the outages in them."""
    now = now or time.time()
    hol = engine.calendar.holidays
    days, d = [], datetime.fromtimestamp(now, ET).date()
    while len(days) < sessions:
        if d.weekday() < 5 and d.isoformat() not in hol:
            days.append(d)
        d -= timedelta(days=1)
    start = datetime(days[-1].year, days[-1].month, days[-1].day, 9, 30, tzinfo=ET).timestamp()
    market_total = session_overlap(start, now, hol)
    log = [o for o in json.loads(engine.storage.get_setting(LOG_KEY) or "[]") if o["to"] >= start]
    asleep = sum(session_overlap(max(o["from"], start), o["to"], hol) for o in log)
    return {"sessions": sessions, "market_h": round(market_total / 3600, 1), "asleep_h": round(asleep / 3600, 1),
            "awake": round(1 - asleep / market_total, 3) if market_total else None,
            "outages": [o for o in reversed(log) if o["market_s"] > 0][:12], "all": len(log)}  # fmt: skip


async def persist(engine: Engine) -> None:
    """Keep the heartbeat and the ledger in the durable state store (when the server has one: on a
    host whose disk is wiped on every restart, the local copy would forget the gap it's measuring)."""
    state = getattr(engine.desk, "state", None)
    if state is None:
        return
    blob = {
        "beat": engine.storage.get_setting(BEAT_KEY),
        "log": json.loads(engine.storage.get_setting(LOG_KEY) or "[]"),
    }
    await state.put("uptime", json.dumps(blob).encode())


async def restore(engine: Engine) -> None:
    """A fresh disk: put back the last heartbeat and the ledger from the state store, before on_start."""
    state = getattr(engine.desk, "state", None)
    if state is None or engine.storage.get_setting(BEAT_KEY):
        return
    raw = await state.get("uptime")
    if not raw:
        return
    blob = json.loads(raw)
    if blob.get("beat"):
        engine.storage.set_setting(BEAT_KEY, str(blob["beat"]))
    if blob.get("log"):
        engine.storage.set_setting(LOG_KEY, json.dumps(blob["log"]))


async def wake_later(engine: Engine, outage: dict[str, Any] | None, delay_s: float = 300) -> int:
    """The wake push, once the tape has had a few minutes to fill (movers need live prices)."""
    import asyncio

    if not outage or outage["market_s"] < PUSH_MIN_S:
        return 0
    await asyncio.sleep(delay_s)
    return await wake_push(engine, outage)
