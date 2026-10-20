"""Into the event: the run-up before an FDA decision, a trial readout or a panel, graded.

Small biotechs (and others) often rise into a known binary event, as traders position for it, then
the event itself decides. "Buy the run-up, sell before the event" is a well-known play; whether it
works here, and whether holding through paid, was never measured. The calendar already captures
these events from headlines (an FDA decision date, a readout, a panel), each with its stock and day.

Once an event is two days past, its stock's daily closes are read (Alpaca's or Yahoo's daily bars,
bars.py) and its path graded:
- **the run-up:** the ten sessions before the event (the close 11 sessions before to the close
  the session before);
- **the event:** the session before to the event day's close;
- **holding through:** the ten sessions before plus the event, against selling the session before.

Pooled by kind of event and company size, the record says: "into FDA decisions, small caps rose
64% of the time in the ten sessions before (median +8.1%); holding through added -3.2% (median)".
Ten sessions before an event on a phone's stock, that phone gets one push with the record (9:35-
10:30 ET). History, not a forecast; events whose bars can't be read are left out.
"""

from __future__ import annotations

import json
import statistics as st
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 6
RUN = 10  # sessions before the event
KEY = "runup:graded"
EVENTS = {"pdufa": "FDA decisions", "readout": "trial readouts", "panel": "FDA panels", "meeting": "medical-meeting data",
          "index": "index changes"}  # fmt: skip


def size(engine: Engine, sym: str) -> str:
    li = engine.universe.get(sym) if getattr(engine, "universe", None) is not None else None
    cap = getattr(li, "market_cap", None)
    return "small" if cap and cap < 2e9 else "large" if cap else "any"


def path(closes: dict[str, float], day: str) -> dict[str, float] | None:
    """The run-up, the event and holding through, in %, from {date: close}; None when the bars don't
    reach eleven sessions before and the event day."""
    days = sorted(closes)
    t = next((i for i, d in enumerate(days) if d >= day), None)  # the event's session (or the next one)
    if t is None or t < RUN + 1:
        return None
    c0, c1, ce = closes[days[t - RUN - 1]], closes[days[t - 1]], closes[days[t]]
    run = (c1 / c0 - 1) * 100
    hold = (ce / c0 - 1) * 100
    return {
        "run": round(run, 2),
        "day": round((ce / c1 - 1) * 100, 2),
        "hold": round(hold, 2),
        "extra": round(hold - run, 2),
    }


def _graded(engine: Engine) -> dict[str, Any]:
    return json.loads(engine.storage.get_setting(KEY) or "{}")


async def grade(engine: Engine, now: float | None = None, max_events: int = 15) -> int:
    """Read the bars for events two days past and not graded yet (tried twice at most)."""
    from . import bars

    now = now or time.time()
    today = datetime.fromtimestamp(now, ET).date()
    done = _graded(engine)
    made = 0
    for ev in engine.storage.catalysts((today - timedelta(days=200)).isoformat()):
        if (
            ev.get("kind") != "binary"
            or not ev.get("symbol")
            or ev["date"] > (today - timedelta(days=2)).isoformat()
        ):
            continue
        g = done.get(ev["id"]) or {}
        if "run" in g or g.get("tries", 0) >= 2:
            continue
        if made >= max_events:
            break
        d = date.fromisoformat(ev["date"])
        try:
            closes = await bars.fetch(
                engine,
                ev["symbol"],
                (d - timedelta(days=24)).isoformat(),
                (d + timedelta(days=4)).isoformat(),
            )
        except Exception:  # noqa: BLE001 - tried again on the next pass
            closes = {}
        pth = path(closes, ev["date"]) if closes else None
        if pth:
            done[ev["id"]] = {**pth, "symbol": ev["symbol"], "event": ev.get("event") or "", "date": ev["date"],
                              "size": size(engine, ev["symbol"])}  # fmt: skip
            made += 1
        else:
            done[ev["id"]] = {"tries": g.get("tries", 0) + 1}
    engine.storage.set_setting(KEY, json.dumps(done))
    return made


def runup_rates(engine: Engine) -> dict[str, Any]:
    """{"event|size": {"key", "label", "n", "run": stats, "event": stats, "extra": stats}} ("*" for any)."""
    pools: dict[str, list[dict[str, Any]]] = {}
    for g in _graded(engine).values():
        if "run" not in g:
            continue
        for ev in (g["event"], "*"):
            for sz in (g["size"], "*"):
                pools.setdefault(f"{ev}|{sz}", []).append(g)
    out = {}
    for key, gs in pools.items():
        if len(gs) < MIN_N:
            continue
        ev, sz = key.split("|")

        def stats(k: str, gs: list[dict[str, Any]] = gs) -> dict[str, Any]:
            xs = [g[k] for g in gs]
            return {
                "n": len(xs),
                "up": round(sum(x > 0 for x in xs) / len(xs), 2),
                "median": round(st.median(xs), 2),
            }

        what = EVENTS.get(ev, "events") if ev != "*" else "events"
        who = {"small": "small caps", "large": "larger companies", "any": "companies", "*": "stocks"}[sz]
        out[key] = {"key": key, "event": ev, "size": sz, "label": f"Into {what}, {who}", "n": len(gs),
                    "run": stats("run"), "on": stats("day"), "extra": stats("extra")}  # fmt: skip
    return out


def runup_for(rates: dict[str, Any], event: str, sz: str) -> dict[str, Any] | None:
    for k in (f"{event}|{sz}", f"{event}|*", f"*|{sz}", "*|*"):
        if k in rates:
            return rates[k]
    return None


def runup_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    return (f"{r['label']}: rose {round(r['run']['up'] * 100)}% of the time in the {RUN} sessions before "
            f"(median {r['run']['median']:+.1f}%); holding through the event added {r['extra']['median']:+.1f}% "
            f"(median, {r['n']})")  # fmt: skip


def sessions_until(day: str, now: float, holidays: set[str]) -> int:
    d = datetime.fromtimestamp(now, ET).date()
    target = date.fromisoformat(day)
    n = 0
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
    return n


async def send_runups(engine: Engine, now: float | None = None) -> int:
    """9:35-10:30 ET: events ten sessions out on a phone's stocks, with the run-up record, once."""
    from .waits import _devices

    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    holidays = getattr(engine.calendar, "holidays", set())
    if (
        dt.weekday() >= 5
        or dt.date().isoformat() in holidays
        or not ((9, 35) <= (dt.hour, dt.minute) < (10, 30))
    ):
        return 0
    if engine.webpush is None:
        return 0
    rates = getattr(engine, "runup_rates", None) or {}
    key = f"runup:{dt.date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for ev in engine.storage.catalysts(dt.date().isoformat()):
        if (
            ev.get("kind") != "binary"
            or not ev.get("symbol")
            or sessions_until(ev["date"], now, holidays) != RUN
        ):
            continue
        r = runup_for(rates, ev.get("event") or "", size(engine, ev["symbol"]))
        if not r:
            continue
        sym = ev["symbol"]
        for dev in sorted(_devices(engine, {"id": ev["id"]}, sym, "runup")):
            k = f"{dev}:{ev['id']}"
            if k in done:
                continue
            done.add(k)
            payload = {"id": f"ru-{ev['id']}"[:60], "title": f"📅 {sym}: {RUN} sessions to its {EVENTS.get(ev.get('event') or '', 'event').rstrip('s')}",
                       "body": f"{ev.get('title', '')[:110]}\n{runup_line(r)}."[:500], "severity": "MEDIUM", "kind": "system",
                       "tickers": [sym], "tag": f"ru-{sym}", "ts": now, "open": f"/app/#t={sym}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = sorted(rates.values(), key=lambda r: (r["event"] == "*", r["size"] == "*", -r["n"]))[:12]
    return {"rows": rows, "need": MIN_N, "sessions": RUN}
