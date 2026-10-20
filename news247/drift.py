"""A month on: which kinds of news kept drifting after the first week, and which gave it back.

Every alert is now also graded 20 sessions on (dossier.record_month: d20), beside the next day and
five sessions on. Pooled per kind of news, the medians (its way) answer the holder's question the
day-one records can't: is this the kind of news the market takes weeks to price in (post-news
drift: "FDA approvals here: +1.5% five sessions on, +4.2% a month on"), or the kind it overshoots
and hands back?

- **Keeps drifting:** the month's median is 1%+ past the fifth session's, and 55%+ were its way.
- **Gives it back:** it was 0.5%+ its way at five sessions and the month's median is 1%+ lower.
- **Holds:** the month ends within 1% of where the fifth session did.

Five sessions after news on a stock a phone watches, holds or has a call on, a **drift check** push
says where the stock is and which of these its kind of news did here (once per alert, 16:05-17:30
ET, on the day its fifth session closes). A kind needs 8 alerts graded a month on. History, not a
forecast.
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
HORIZONS = ("close", "d1", "d5", "d20")
VERDICT = {"drifts": "kept drifting for a month", "gives_back": "gave it back over the month",
           "holds": "held where it was at five sessions"}  # fmt: skip


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def verdict(r: dict[str, Any]) -> str | None:
    d5, d20 = r.get("d5"), r.get("d20")
    if not d5 or not d20 or d20["n"] < MIN_N:
        return None
    if d20["median"] >= d5["median"] + 1.0 and d20["hit"] >= 0.55:
        return "drifts"
    if d5["median"] >= 0.5 and d20["median"] <= d5["median"] - 1.0:
        return "gives_back"
    return "holds"


def drift_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{kind: {"key", "label", "close", "d1", "d5", "d20", "x_d20", "verdict"}} for "*" and each
    kind of news (catalysts.classify)."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        o = (a.get("edge") or {}).get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if not row.get("sign") or row.get("d5") is None:
            continue
        s = row["sign"]
        mkt = (a.get("edge") or {}).get("mkt_outcome") or {}
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for key in ("*", c["key"]) if c else ("*",):
            g = pools.setdefault(key, {h: [] for h in (*HORIZONS, "x_d20")})
            for h in HORIZONS:
                if row.get(h) is not None:
                    g[h].append(s * row[h])
            if row.get("d20") is not None and mkt.get("d20") is not None:
                g["x_d20"].append(s * (row["d20"] - mkt["d20"]))
    out = {}
    for key, g in pools.items():
        r = {"key": key, "label": "Every alert" if key == "*" else LABELS.get(key, key),
             **{h: _stats(v) for h, v in g.items()}}  # fmt: skip
        r["verdict"] = verdict(r)
        if r["d5"] and r["d5"]["n"] >= MIN_N:
            out[key] = r
    return out


def drift_for(rates: dict[str, Any], a: dict[str, Any], universe: Any = None) -> dict[str, Any] | None:
    """This alert's kind's month when it has one, else every alert's."""
    from .catalysts import classify

    c = (a.get("edge") or {}).get("catalyst") or classify(a, universe)
    for key in (c["key"], "*") if c else ("*",):
        r = rates.get(key)
        if r and r.get("verdict"):
            return r
    return None


def drift_line(r: dict[str, Any] | None) -> str:
    """'Contract win news here: +1.5% five sessions on, +4.2% a month on (median, its way, 12): it kept drifting'."""
    if not r or not r.get("verdict"):
        return ""
    who = "News here" if r["key"] == "*" else f"{r['label']} news here"
    x = f"; {r['x_d20']['median']:+.1f}% against SPY" if r.get("x_d20") and r["x_d20"]["n"] >= MIN_N else ""
    return (f"{who}: {r['d5']['median']:+.1f}% five sessions on, {r['d20']['median']:+.1f}% a month on "
            f"(median, its way, {r['d20']['n']}{x}): it {VERDICT[r['verdict']]}")  # fmt: skip


def drift_board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in rates.values() if r.get("verdict")]
    rows.sort(key=lambda r: -(r["d20"]["median"] - r["d5"]["median"]))
    return {
        "rows": rows,
        "need": MIN_N,
        "waiting": sorted(k for k, r in rates.items() if not r.get("verdict")),
    }


async def send_drift_checks(engine: Engine, now: float | None = None) -> int:
    """16:05-17:30 ET: alerts whose fifth session closed today, on stocks a phone watches, holds or
    has a call on, when their kind of news has a month's record: one push each, once."""
    from .dossier import _direction, session_closes

    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    holidays = getattr(engine.calendar, "holidays", set())
    if (
        dt.weekday() >= 5
        or dt.date().isoformat() in holidays
        or not ((16, 5) <= (dt.hour, dt.minute) < (17, 30))
    ):
        return 0
    if engine.webpush is None:
        return 0
    rates = getattr(engine, "drift_rates", None) or {}
    today_close = dt.replace(hour=16, minute=0, second=0, microsecond=0).timestamp()
    key = f"drift:{dt.date().isoformat()}"
    sent_keys = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for a in engine.storage.alerts_between(now - 12 * 86400, now - 4 * 86400, 2000):
        if a.get("kind") == "system" or not a.get("tickers") or not _direction(a):
            continue
        if session_closes(float(a["created"]), 6, holidays)[5] != today_close:
            continue
        r = drift_for(rates, a, engine.universe)
        if not r:
            continue
        sym = a["tickers"][0]
        row = ((a.get("edge") or {}).get("outcome") or {}).get(sym) or {}
        its = row["sign"] * row["d5"] if row.get("sign") and row.get("d5") is not None else None
        devices = set(engine.desk.watchers([sym])) | {
            d
            for d, p in engine.desk.profiles.items()
            if any(t["symbol"] == sym for t in p.get("tracked") or [])
        }
        for dev in sorted(devices):
            p = engine.desk.profiles.get(dev) or {}
            k = f"{dev}:{a['id']}"
            if k in sent_keys or not (p.get("prefs") or {}).get("drift", True):
                continue
            sent_keys.add(k)
            icon = {"drifts": "📈", "gives_back": "📉", "holds": "➖"}[r["verdict"]]
            head = f"{sym}, five sessions on" + (f": {its:+.1f}% its way" if its is not None else "")
            payload = {"id": f"dr-{a['id']}", "title": f"{icon} {head}", "body": f"{a.get('title', '')[:120]}\n{drift_line(r)}"[:500],
                       "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"dr-{sym}", "ts": now,
                       "open": f"/app/#t={sym}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(sent_keys)))
    return sent
