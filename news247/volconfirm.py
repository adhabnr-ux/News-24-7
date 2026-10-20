"""Volume confirmation, graded: is the market trading the news, or ignoring it?

A headline is a claim; volume is the market voting on it. A stock trading at ten times its normal
volume for the time of day fifteen minutes after its news is being repriced. One at its usual volume
is being ignored, however big the headline sounds. That's an old trading rule, and now a graded one.
Every alert notes its stock's relative volume (today's volume against its normal for this time of
day, from the tape) when it goes out and again 15 minutes in (edge.rvol: {"r0", "r15"}).

Graded by the close, by band 15 minutes in (under 2x, 2-5x, 5-10x, 10x+; the first read when
there's no second), for every kind of news and each kind. At 15 minutes, when the alert's band has a
clear record (60%+ or 40%- with 8+ graded), phones with the stock get one push: "confirmed by volume"
or "no volume behind it". The first read's band record counts in the call. History, not a forecast.
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
BANDS = (("quiet", "under 2x normal volume", 0, 2), ("busy", "2-5x normal volume", 2, 5),
         ("heavy", "5-10x normal volume", 5, 10), ("frenzy", "10x+ normal volume", 10, float("inf")))  # fmt: skip
LABEL = {k: lbl for k, lbl, _, _ in BANDS}


def band(r: float | None) -> str | None:
    if r is None or r < 0:
        return None
    return next(k for k, _, lo, hi in BANDS if lo <= r < hi)


def read(engine: Engine, sym: str, now: float | None = None, max_age_s: float = 900) -> float | None:
    """The stock's relative volume for the time of day, from the tape's latest read."""
    ig = getattr(engine, "ignition", None)
    if ig is None or not hasattr(ig, "latest"):
        return None
    try:
        r = ig.latest(sym, now=now, max_age_s=max_age_s)
    except TypeError:
        r = ig.latest(sym, now)
    v = (r or {}).get("rvol")
    return round(float(v), 1) if v is not None else None


def check15(engine: Engine, now: float | None = None) -> list[dict[str, Any]]:
    """Stamp the 15-minute read on alerts 15-25 minutes old; returns the ones stamped."""
    now = now or time.time()
    out = []
    for a in engine.storage.alerts_between(now - 1500, now - 900, 400):
        e = a.get("edge") or {}
        rv = dict(e.get("rvol") or {})
        if a.get("kind") == "system" or not a.get("tickers") or "r15" in rv:
            continue
        r = read(engine, a["tickers"][0], now, 600)
        if r is None:
            continue
        rv["r15"] = r
        engine.storage.update_alert_edge(a["id"], {"rvol": rv})
        out.append({**a, "edge": {**e, "rvol": rv}})
    return out


def rvol_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"r15:band|kind" and "r0:band|kind": {"key", "label", "n", "hit", "median"}} by the close."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        rv = e.get("rvol") or {}
        o = e.get("outcome") or {}
        if not rv or not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if not row.get("sign") or row.get("close") is None:
            continue
        its = row["sign"] * row["close"]
        c = e.get("catalyst") or classify(a, engine.universe)
        b15 = band(rv.get("r15") if rv.get("r15") is not None else rv.get("r0"))
        b0 = band(rv.get("r0"))
        for kind in ("*", c["key"]) if c else ("*",):
            if b15:
                pools.setdefault(f"r15:{b15}|{kind}", []).append(its)
            if b0:
                pools.setdefault(f"r0:{b0}|{kind}", []).append(its)
    out = {}
    for key, xs in pools.items():
        if len(xs) < MIN_N:
            continue
        when_band, kind = key.split("|")
        when, b = when_band.split(":")
        who = "News" if kind == "*" else LABELS.get(kind, kind)
        at = "15 minutes in" if when == "r15" else "when it went out"
        out[key] = {"key": key, "when": when, "band": b, "kind": kind, "label": f"{who} on {LABEL[b]} {at}", "n": len(xs),
                    "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)}  # fmt: skip
    return out


def rvol_for(
    rates: dict[str, Any], kind: str | None, r: float | None, when: str = "r15"
) -> dict[str, Any] | None:
    b = band(r)
    if not b:
        return None
    for k in (f"{when}:{b}|{kind}" if kind else None, f"{when}:{b}|*"):
        if k and k in rates:
            return rates[k]
    return None


def rvol_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    return f"{r['label']}: went its way {round(r['hit'] * 100)}% by the close (median {r['median']:+.1f}%, {r['n']})"


async def send_confirms(engine: Engine, now: float | None = None) -> int:
    """At 15 minutes: one push per alert per phone with the stock, when its volume band's record is clear."""
    from .catalysts import classify
    from .waits import _devices

    now = now or time.time()
    stamped = check15(engine, now)
    if not stamped or engine.webpush is None:
        return 0
    rates = getattr(engine, "rvol_rates", None) or {}
    key = f"volconf:{datetime.fromtimestamp(now, ET).date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for a in stamped:
        e = a["edge"]
        c = e.get("catalyst") or classify(a, engine.universe)
        r15 = e["rvol"]["r15"]
        rec = rvol_for(rates, c["key"] if c else None, r15)
        if not rec or not (rec["hit"] >= 0.6 or rec["hit"] <= 0.4):
            continue
        sym = a["tickers"][0]
        verdict = "confirmed by volume" if rec["hit"] >= 0.6 else "no volume behind it"
        icon = "📊" if rec["hit"] >= 0.6 else "🔇"
        body = (
            f"{str(a.get('title') or '')[:110]}\n{r15:g}x its normal volume 15 minutes in. {rvol_line(rec)}."
        )
        for dev in sorted(_devices(engine, a, sym, "volconf")):
            k = f"{dev}:{a['id']}"
            if k in done:
                continue
            done.add(k)
            payload = {"id": f"vc-{a['id']}", "title": f"{icon} {sym} 15 minutes in: {verdict}", "body": body[:500],
                       "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"vc-{sym}", "ts": now,
                       "open": f"/app/#a={a['id']}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = [rates[f"r15:{b}|*"] for b, *_ in BANDS if f"r15:{b}|*" in rates]
    kinds = sorted(
        (r for r in rates.values() if r["kind"] != "*" and r["when"] == "r15"), key=lambda r: -r["n"]
    )[:10]
    return {"rows": rows, "kinds": kinds, "need": MIN_N}
