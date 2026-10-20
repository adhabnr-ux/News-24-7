"""The second leg: a news stock breaking to a new high (its way) well after the alert, graded.

News moves rarely go in one line. The first leg comes with the headline; then the stock rests, and
the ones that matter often break out again an hour or more later. That breakout is a classic entry,
and the desk now watches for it on the day's alerts. A second leg is when the stock trades at a new
day's extreme, its way:
- an hour or more after the alert;
- 2%+ its way from the alert price;
- after 30+ minutes without a new extreme (a real rest, not one long climb).

Each is stamped on its alert (edge.leg2) once, and phones that got the alert, or watch, hold or have
a call on the stock, get one push. Graded from the breakout price to the close, its way, by kind of
news. The push carries the record: "second legs on contract news went on by the close 57% (median
+1.1%, 23)". History, not a forecast; an entry this late gives up the first leg by design.
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
MIN_AGE = 3600
MIN_MOVE = 2.0
REST = 1800
MIN_N = 8


def check(engine: Engine, now: float | None = None) -> list[dict[str, Any]]:
    """Today's in-session alerts whose stock just broke to a new extreme its way after a rest."""
    from .dossier import _direction

    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    if dt.weekday() >= 5 or not ((9, 30) <= (dt.hour, dt.minute) < (16, 0)):
        return []
    memo: dict[str, tuple[float, float]] = engine.__dict__.setdefault("_leg_memo", {})
    day0 = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out, seen = [], set()
    for a in engine.storage.alerts_between(day0, now - MIN_AGE, 600):
        e = a.get("edge") or {}
        if a.get("kind") == "system" or not a.get("tickers") or e.get("leg2"):
            continue
        sym = a["tickers"][0]
        sign = {"up": 1, "down": -1}.get(_direction(a), 0)
        ref = (e.get("refs") or {}).get(sym)
        if not sign or not ref or sym in seen:
            continue
        seen.add(sym)
        q = engine.price_of(sym, 300, now=now)
        if not q or not q[1]:
            continue
        px = q[1]
        key = f"{a['id']}:{sym}"
        best, best_ts = memo.get(key, (None, None))  # the extreme so far, its way, and when it was set
        if best is None:
            path = engine.live_quotes.path(sym, now) if hasattr(engine.live_quotes, "path") else None
            if path:  # the day's extreme so far, and when it was set (0: from a daily bar, time unknown)
                hi, at = (path.high, path.high_ts) if sign > 0 else (path.low, path.low_ts)
                memo[key] = (hi, at or now)
            else:
                memo[key] = (px, now)
            continue
        if sign * (px - best) > 0:  # a new extreme
            rested = now - best_ts >= REST
            its = round(sign * (px / ref - 1) * 100, 2)
            memo[key] = (px, now)
            if rested and its >= MIN_MOVE:
                out.append({"alert": a, "symbol": sym, "sign": sign, "px": px, "its": its,
                            "rest_min": round((now - best_ts) / 60), "age_min": round((now - float(a["created"])) / 60)})  # fmt: skip
    return out


def graded(a: dict[str, Any]) -> float | None:
    """From the breakout to the close, its way, in %."""
    e = a.get("edge") or {}
    leg = e.get("leg2") or {}
    o = e.get("outcome") or {}
    refs = e.get("refs") or {}
    if not leg.get("px") or not o:
        return None
    sym, row = next(iter(o.items()))
    ref = refs.get(sym)
    if not ref or not row.get("sign") or row.get("close") is None:
        return None
    return round(row["sign"] * (ref * (1 + row["close"] / 100) / leg["px"] - 1) * 100, 2)


def leg_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"kind": {"key", "label", "n", "hit", "median"}} ("*" for any kind)."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        x = graded(a)
        if x is None:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            pools.setdefault(kind, []).append(x)
    return {k: {"key": k, "label": "Second legs" + ("" if k == "*" else f" on {LABELS.get(k, k).lower()} news"), "n": len(xs),
                "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)}
            for k, xs in pools.items() if len(xs) >= MIN_N}  # fmt: skip


def leg_for(rates: dict[str, Any], kind: str | None) -> dict[str, Any] | None:
    return rates.get(kind or "") or rates.get("*")


def leg_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    return (
        f"{r['label']} went on by the close {round(r['hit'] * 100)}% (median {r['median']:+.1f}%, {r['n']})."
    )


async def send_legs(engine: Engine, now: float | None = None) -> int:
    """Stamp each second leg on its alert and push it, once per alert per phone."""
    from .catalysts import classify
    from .waits import _devices

    now = now or time.time()
    legs = check(engine, now)
    if not legs:
        return 0
    rates = getattr(engine, "leg_rates", None) or {}
    key = f"leg2:{datetime.fromtimestamp(now, ET).date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for g in legs:
        a, sym = g["alert"], g["symbol"]
        engine.storage.update_alert_edge(
            a["id"], {"leg2": {"ts": now, "px": g["px"], "its": g["its"], "rest_min": g["rest_min"]}}
        )
        if hasattr(engine, "autopilot_signal"):  # rules following second legs: in at the breakout
            await engine.autopilot_signal(a, "leg2", " on its second leg", now)
        if engine.webpush is None:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        word = "high" if g["sign"] > 0 else "low"
        hours = g["age_min"] / 60
        when = f"{hours:.1f} h" if hours >= 1 else f"{g['age_min']} min"
        body = (f"{str(a.get('title') or '')[:110]}\nA new day's {word} after {g['rest_min']} minutes without one: "
                f"{g['its']:+.1f}% its way since the alert. {leg_line(leg_for(rates, c['key'] if c else None))}").strip()  # fmt: skip
        for dev in sorted(_devices(engine, a, sym, "leg2")):
            k = f"{dev}:{a['id']}"
            if k in done:
                continue
            done.add(k)
            payload = {"id": f"l2-{a['id']}", "title": f"🚀 {sym} second leg: a new {word} {when} after its news",
                       "body": body[:500], "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"l2-{sym}",
                       "ts": now, "open": f"/app/#a={a['id']}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = sorted(rates.values(), key=lambda r: (r["key"] != "*", -r["n"]))[:12]
    return {"rows": rows, "need": MIN_N}
