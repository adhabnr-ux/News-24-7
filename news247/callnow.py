"""The call, live: the call is made when the alert goes out; the day keeps adding facts. Re-read it.

The call (thecall.py) adds up what's known when an alert goes out. By mid-morning, more has been
graded live:
- whether the market is trading the news (its volume 15 minutes in, volconfirm.py);
- whether the mainstream has caught up and what's usually left after that (catchup.py);
- whether the stock broke out on a second leg (secondleg.py).
Each comes with its own record. The call now re-reads itself with those facts every minute for the
day's alerts. When the verdict changes ("wait" → "act now", "act now" → "skip"), it's stamped on
the alert (edge.call_now) with the fact that moved it, the sheet shows it, and phones tracking a
call on the stock or that got the alert get one push.

The changes are graded like everything else: when the call turned to act during the day, how did
the stock do from that moment to the close, and when it turned to skip? If turning doesn't pay,
the live facts aren't worth following and it shows. History, not a forecast.
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


def live_facts(engine: Engine, a: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(side, key, text) for each live fact with a clear record (60%+ or 40%-, 8+ graded)."""
    from .catalysts import classify
    from .catchup import catchup_for
    from .secondleg import leg_for
    from .volconfirm import rvol_for

    e = a.get("edge") or {}
    c = e.get("catalyst") or classify(a, getattr(engine, "universe", None))
    kind = c["key"] if c else None
    out: list[tuple[str, str, str]] = []

    def put(key: str, rec: dict[str, Any] | None, hit: float | None, text: str) -> None:
        if rec and hit is not None and rec.get("n", 0) >= MIN_N and (hit >= 0.6 or hit <= 0.4):
            out.append(("+" if hit >= 0.6 else "-", key, f"{text} {round(hit * 100)}%"))

    r15 = (e.get("rvol") or {}).get("r15")
    if r15 is not None:
        rec = rvol_for(getattr(engine, "rvol_rates", None) or {}, kind, r15)
        put("vol15", rec, (rec or {}).get("hit"), f"{r15:g}x volume 15 minutes in:")
    ld = e.get("lead") or {}
    if ld.get("px"):
        rec = catchup_for(getattr(engine, "catchup_rates", None) or {}, kind, ld["lead_s"])
        put(
            "catchup",
            rec,
            ((rec or {}).get("after") or {}).get("hit"),
            "the mainstream has it; after that, went further",
        )
    if (e.get("leg2") or {}).get("px"):
        rec = leg_for(getattr(engine, "leg_rates", None) or {}, kind)
        put("leg2", rec, (rec or {}).get("hit"), "a second leg; second legs went on")
    return out


def call_now(engine: Engine, a: dict[str, Any]) -> dict[str, Any] | None:
    """The call re-read with the live facts, or None when there are none (it stands as made)."""
    from .opening import outside
    from .thecall import the_call

    facts = live_facts(engine, a)
    if not facts:
        return None
    e = a.get("edge") or {}
    return the_call(e, outside(float(a["created"]), engine.calendar.holidays),
                    frozenset(getattr(engine, "call_muted", None) or []), extra=facts)  # fmt: skip


def check(engine: Engine, now: float | None = None) -> list[dict[str, Any]]:
    """Today's alerts whose live call differs from the one they went out with (or last changed to):
    stamp each change once, and return them."""
    now = now or time.time()
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out = []
    for a in engine.storage.alerts_between(day0, now, 800):
        e = a.get("edge") or {}
        was = (e.get("call_now") or e.get("call") or {}).get("verb")
        if not was or a.get("kind") == "system":
            continue
        c = call_now(engine, a)
        if not c or c["verb"] == was:
            continue
        sym = next(iter(e.get("refs") or {}), None) or (a.get("tickers") or [None])[0]
        q = engine.price_of(sym, 600, now=now) if sym else None
        stamp = {"verb": c["verb"], "label": c["label"], "line": c["line"], "from": was, "at": now,
                 "px": q[1] if q and q[1] else None, "live": [t for s, k, t in c.get("live_facts", [])]}  # fmt: skip
        engine.storage.update_alert_edge(a["id"], {"call_now": stamp})
        out.append({"alert": a, "symbol": sym, "call": stamp})
    return out


def graded(a: dict[str, Any]) -> tuple[str, float] | None:
    """(the verdict it turned to, the move from the turn to the close its way)."""
    e = a.get("edge") or {}
    cn = e.get("call_now") or {}
    o = e.get("outcome") or {}
    refs = e.get("refs") or {}
    if not cn.get("px") or not o:
        return None
    sym, row = next(iter(o.items()))
    ref = refs.get(sym)
    if not ref or not row.get("sign") or row.get("close") is None:
        return None
    return cn["verb"], round(row["sign"] * (ref * (1 + row["close"] / 100) / cn["px"] - 1) * 100, 2)


def turn_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{verb: {"verb", "n", "hit", "median"}}: from the moment the call turned to it, to the close."""
    now = now or time.time()
    pools: dict[str, list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        g = graded(a)
        if g:
            pools.setdefault(g[0], []).append(g[1])
    return {v: {"verb": v, "n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)}
            for v, xs in pools.items() if len(xs) >= MIN_N}  # fmt: skip


async def send_turns(engine: Engine, now: float | None = None) -> int:
    """Each change of the call: one push per phone tracking a call on the stock or that got the alert."""
    from .thecall import VERBS
    from .waits import _devices

    now = now or time.time()
    turns = check(engine, now)
    if not turns or engine.webpush is None:
        return 0
    rates = getattr(engine, "turn_rates", None) or {}
    key = f"callnow:{datetime.fromtimestamp(now, ET).date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for t in turns:
        a, sym, c = t["alert"], t["symbol"], t["call"]
        if not sym:
            continue
        r = rates.get(c["verb"])
        rec = (f" Calls that turned to \"{c['label'].lower()}\" during the day went their way {round(r['hit'] * 100)}% "
               f"from the turn to the close (median {r['median']:+.1f}%, {r['n']}).") if r else ""  # fmt: skip
        body = f"{str(a.get('title') or '')[:110]}\n{c['line']}.{rec}"
        icon = VERBS[c["verb"]][0]
        for dev in sorted(_devices(engine, a, sym, "callnow")):
            k = f"{dev}:{a['id']}:{c['verb']}"
            if k in done:
                continue
            done.add(k)
            payload = {"id": f"cn-{a['id']}", "title": f"{icon} {sym}: the call is now {c['label'].lower()} (was {VERBS[c['from']][1].lower()})",
                       "body": body[:500], "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"cn-{sym}",
                       "ts": now, "open": f"/app/#a={a['id']}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent
