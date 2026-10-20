"""The mainstream catch-up, graded: what the head start was worth, and what's left after it.

Foretape's edge is speed: the same story reaches CNBC, Reuters, Bloomberg or the WSJ minutes or hours
after the alert, and each alert records when (edge.lead). What that head start was worth was never
measured. Now the stock's price is noted the moment a mainstream outlet carries the story, so each
alert splits its day in two, its way:
- **before the catch-up:** what the head start captured;
- **after it:** whether the story went further once everyone had it, by the close.

Pooled by kind of news and by how long the head start was (under 15 minutes, 15-60, 1-4 hours, 4
hours+), the record says it: "contract wins caught up on within the hour: 72% of the day's move came
before; after it, 41% went further (median -0.3%)". When the mainstream catches up on a story on a
phone's stock, that phone gets one push with where the stock stands and the record. A late catch-up
on a story whose record says "nothing left" is the signal to stop chasing it. History, not a
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
BUCKETS = (("fast", "within 15 minutes", 0, 900), ("hour", "in 15-60 minutes", 900, 3600),
           ("hours", "in 1-4 hours", 3600, 4 * 3600), ("late", "after 4 hours or more", 4 * 3600, float("inf")))  # fmt: skip
B_LABEL = {k: lbl for k, lbl, _, _ in BUCKETS}
OUTLETS = {"cnbc": "CNBC", "yahoo": "Yahoo Finance", "gnews": "Google News", "mw": "MarketWatch", "wsj": "the WSJ",
           "bloomberg": "Bloomberg", "reuters": "Reuters", "ft": "the FT", "barrons": "Barron's", "ap": "the AP"}  # fmt: skip


def bucket(lead_s: float) -> str:
    return next(k for k, _, lo, hi in BUCKETS if lo <= lead_s < hi)


def outlet(source: str) -> str:
    head = source.split("-")[0].lower()
    return OUTLETS.get(head, head.upper() if len(head) <= 4 else head.title())


def split(a: dict[str, Any], holidays: set[str] | None = None) -> tuple[float, float, float, float] | None:
    """(lead seconds, before, after, the whole day), its way, in %, for an alert the mainstream
    caught up on before its close, or None."""
    from .dossier import session_closes

    e = a.get("edge") or {}
    ld = e.get("lead") or {}
    o = e.get("outcome") or {}
    refs = e.get("refs") or {}
    if not ld.get("px") or not o:
        return None
    sym, row = next(iter(o.items()))
    ref = refs.get(sym)
    if not ref or not row.get("sign") or row.get("close") is None:
        return None
    if ld["at"] >= session_closes(float(a["created"]), 1, holidays)[0]:
        return None  # caught up after the close: the day's split doesn't apply
    s = row["sign"]
    close_px = ref * (1 + row["close"] / 100)
    before = s * (ld["px"] / ref - 1) * 100
    after = s * (close_px / ld["px"] - 1) * 100
    return ld["lead_s"], round(before, 2), round(after, 2), round(s * row["close"], 2)


def catchup_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"kind|bucket": {"key", "label", "n", "after": stats, "before_median", "captured"}} for every
    kind ("*" for any) and head start ("*" for any). "captured": of the winners' day, the median
    share made before the catch-up."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        sp = split(a, engine.calendar.holidays)
        if not sp or a.get("kind") == "system":
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            for b in (bucket(sp[0]), "*"):
                pools.setdefault((kind, b), []).append(sp[1:])
    out = {}
    for (kind, b), xs in pools.items():
        if len(xs) < MIN_N:
            continue
        after = [x[1] for x in xs]
        wins = [x[0] / x[2] for x in xs if x[2] >= 0.5]
        who = "News" if kind == "*" else LABELS.get(kind, kind)
        out[f"{kind}|{b}"] = {"key": f"{kind}|{b}", "kind": kind, "bucket": b, "n": len(xs),
                              "label": f"{who} the mainstream caught up on" + (f" {B_LABEL[b]}" if b != "*" else ""),
                              "after": {"n": len(xs), "hit": round(sum(x > 0 for x in after) / len(xs), 2), "median": round(st.median(after), 2)},
                              "before_median": round(st.median(x[0] for x in xs), 2),
                              "captured": round(min(1.0, max(0.0, st.median(wins))), 2) if len(wins) >= MIN_N // 2 else None}  # fmt: skip
    return out


def catchup_for(rates: dict[str, Any], kind: str | None, lead_s: float) -> dict[str, Any] | None:
    b = bucket(lead_s)
    for k in (f"{kind}|{b}" if kind else None, f"*|{b}", f"{kind}|*" if kind else None, "*|*"):
        if k and k in rates:
            return rates[k]
    return None


def catchup_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    a = r["after"]
    cap = (
        f"{round(r['captured'] * 100)}% of the winners' day came before it; "
        if r.get("captured") is not None
        else ""
    )
    return f"{r['label']}: {cap}after it, {round(a['hit'] * 100)}% went further by the close (median {a['median']:+.1f}%, {r['n']})"


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = [rates[f"*|{b}"] for b, *_ in BUCKETS if f"*|{b}" in rates]
    kinds = sorted(
        (r for r in rates.values() if r["kind"] != "*" and r["bucket"] == "*"), key=lambda r: -r["n"]
    )[:10]
    return {"rows": rows, "all": rates.get("*|*"), "kinds": kinds, "need": MIN_N}


def _dur(s: float) -> str:
    return f"{round(s / 60)} min" if s < 3600 else f"{s / 3600:.1f} h"


async def send_catchup(engine: Engine, alert_id: str, now: float | None = None) -> int:
    """The mainstream just carried a story on a phone's stock: one push per phone, with where the
    stock stands since the alert and what's left after a catch-up like it."""
    from .catalysts import classify
    from .waits import _devices

    now = now or time.time()
    a = engine.storage.get_alert(alert_id)
    if not a or engine.webpush is None or not a.get("tickers"):
        return 0
    e = a.get("edge") or {}
    ld = e.get("lead") or {}
    sym = a["tickers"][0]
    if not ld.get("px"):
        return 0
    c = e.get("catalyst") or classify(a, engine.universe)
    r = catchup_for(getattr(engine, "catchup_rates", None) or {}, c["key"] if c else None, ld["lead_s"])
    since = f"Since the alert: {ld['its']:+.1f}% its way. " if ld.get("its") is not None else ""
    body = f"{str(a.get('title') or '')[:110]}\n{since}{catchup_line(r)}".strip()
    key = f"catchup:{datetime.fromtimestamp(now, ET).date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for dev in sorted(_devices(engine, a, sym, "catchup")):
        k = f"{dev}:{alert_id}"
        if k in done:
            continue
        done.add(k)
        payload = {"id": f"cu-{alert_id}", "title": f"📰 {outlet(ld['source'])} has {sym} now, {_dur(ld['lead_s'])} after your alert",
                   "body": body[:500], "severity": "MEDIUM", "kind": "system", "tickers": [sym], "tag": f"cu-{sym}",
                   "ts": now, "open": f"/app/#a={alert_id}"}  # fmt: skip
        if await engine._to_device(dev, payload, urgency="normal"):
            sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent
