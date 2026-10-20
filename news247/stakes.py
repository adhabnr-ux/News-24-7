"""What's at stake: each upcoming binary event with how both of its outcomes have traded here.

An FDA decision, a trial readout, a report: the stock moves on which way it goes, and the two
branches are rarely the same size. For every scheduled catalyst in the next two weeks (captured
from the headlines, see analysis/catalyst_dates.py) and every report on a phone's own stocks, this
lays out both branches from the graded record of that kind of news at the stock's size: "an
approval: +14% median by the close (9); a rejection or a negative panel: −38% (5)", beside what
options price for it and the phone's own exposure. The record is this server's alerts of that kind,
each graded from its own price. History, not a forecast of the outcome or its size.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
BRANCHES = {
    "pdufa": (("fda_approval", "an approval"), ("fda_negative", "a rejection or delay")),
    "adcom": (("fda_approval", "a positive vote"), ("fda_negative", "a negative vote")),
    "readout": (("trial_win", "the trial works"), ("trial_fail", "the trial fails")),
    "conference": (("trial_win", "good data"), ("trial_fail", "bad data")),
    "earnings": (("earnings_beat", "a beat"), ("earnings_miss", "a miss"), ("guidance_raise", "guidance up"), ("guidance_cut", "guidance down")),
}  # fmt: skip
MIN_N = 4


def _branch(rates: dict[str, Any], key: str, label: str, band: str) -> dict[str, Any]:
    """A branch's record at the stock's size, else at any size; signed as the stock moved (a
    rejection's record is its way for a short, so it's flipped to show the stock's move)."""
    from .catalysts import LABELS

    for b in (band, "*"):
        r = rates.get(f"{key}|{b}") or {}
        c = r.get("close")
        if c and c["n"] >= MIN_N:
            # records are its way; a bad-news kind's way is down: show the stock's own move
            sign = -1 if key in ("fda_negative", "trial_fail", "earnings_miss", "guidance_cut") else 1
            d1 = r.get("d1")
            return {"key": key, "label": label, "kind": LABELS.get(key, key), "n": c["n"], "size": r.get("band_label"),
                    "close": round(sign * c["median"], 2), "d1": round(sign * d1["median"], 2) if d1 else None,
                    "typical": r.get("typical")}  # fmt: skip
    return {"key": key, "label": label, "kind": LABELS.get(key, key), "n": 0}


def event_stakes(
    engine: Engine, device: str = "", now: float | None = None, days: int = 14
) -> dict[str, Any]:
    from .catalysts import size_band

    now = now or time.time()
    today = datetime.fromtimestamp(now, ET).date()
    end = today + timedelta(days=days)
    rates = getattr(engine, "catalyst_rates", None) or {}
    p = engine.desk.profiles.get(device) or {}
    holdings = p.get("holdings") or {}
    mine = set(p.get("watchlist") or []) | set(holdings)
    events: list[dict[str, Any]] = []
    for c in engine.storage.catalysts(today.isoformat()):
        if c.get("date", "") <= end.isoformat() and c.get("symbol") and c.get("event") in BRANCHES:
            events.append(
                {
                    "symbol": c["symbol"],
                    "date": c["date"],
                    "when": c.get("time") or "",
                    "event": c["event"],
                    "title": c.get("title") or "",
                }
            )
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    if book is not None:
        for sym in sorted(mine):
            e = book.lookup(sym, today)
            if e and 0 <= (e.get("in_days") if e.get("in_days") is not None else 99) <= days:
                events.append(
                    {
                        "symbol": sym,
                        "date": e["date"],
                        "when": e.get("when") or "",
                        "event": "earnings",
                        "title": f"{sym} reports",
                    }
                )
    rows = []
    for ev in events:
        sym = ev["symbol"]
        li = engine.universe.get(sym)
        band = size_band(li.market_cap if li else None)
        q = engine.price_of(sym, 86400, now=now)
        cached = (getattr(engine, "implied_cache", {}) or {}).get(sym)
        im = (cached[1] if cached else {}).get("rows") or []
        imp = next((r for r in im if r.get("earnings")), None) or (im[0] if im else None)
        h = holdings.get(sym) or {}
        value = round(h["shares"] * q[1], 2) if h.get("shares") and q else None
        try:
            in_days = (date.fromisoformat(ev["date"]) - today).days
        except ValueError:
            continue
        rows.append({**ev, "in_days": in_days, "mine": sym in mine, "price": q[1] if q else None,
                     "branches": [_branch(rates, k, label, band) for k, label in BRANCHES[ev["event"]]],
                     "implied_pct": imp["move_pct"] if imp else None, "value": value,
                     "tracked": sum(1 for t in p.get("tracked") or [] if t["symbol"] == sym)})  # fmt: skip
    rows.sort(key=lambda r: (r["date"], not r["mine"], r["symbol"]))
    return {"rows": rows, "days": days}


# --------------------------------------------------------------------------- event day

BINARY = ("pdufa", "adcom", "readout", "conference")
EVENT_LABEL = {
    "pdufa": "FDA decision",
    "adcom": "FDA panel",
    "readout": "trial readout",
    "conference": "data at a meeting",
}


def log_events(engine: Engine, now: float | None = None) -> int:
    """Every binary event today, noted once with its implied move and the stock's price before it
    (event_log), so its realized move can be graded against what options priced."""
    import json

    now = now or time.time()
    today = datetime.fromtimestamp(now, ET).date().isoformat()
    log = json.loads(engine.storage.get_setting("event_log") or "[]")
    have = {e["id"] for e in log}
    added = 0
    for c in engine.storage.catalysts(today):
        if c.get("date") != today or c.get("event") not in BINARY or not c.get("symbol") or c["id"] in have:
            continue
        sym = c["symbol"]
        q = engine.price_of(sym, 86400, now=now)
        cached = (getattr(engine, "implied_cache", {}) or {}).get(sym)
        im = (cached[1] if cached else {}).get("rows") or []
        log.append({"id": c["id"], "symbol": sym, "date": today, "event": c["event"], "ref": q[1] if q else None,
                    "implied": im[0]["move_pct"] if im and im[0].get("move_pct") else None, "realized": None, "ts": now})  # fmt: skip
        added += 1
    if added:
        engine.storage.set_setting("event_log", json.dumps(log[-500:]))
    return added


def grade_events(engine: Engine, now: float | None = None) -> int:
    """Each logged event's move over two sessions (the day of and the next), from its price before,
    either way: at the second close, within half an hour of it."""
    import json

    from .dossier import session_closes

    now = now or time.time()
    raw = engine.storage.get_setting("event_log")
    if not raw:
        return 0
    log = json.loads(raw)
    graded = 0
    for e in log:
        if e.get("realized") is not None or not e.get("ref"):
            continue
        second = session_closes(e["ts"], 2, getattr(engine.calendar, "holidays", set()))[1]
        q = engine.price_of(e["symbol"], 900, now=now)
        if q and second <= now <= second + 1800:
            e["realized"] = round(abs(q[1] / e["ref"] - 1) * 100, 2)
            graded += 1
    if graded:
        engine.storage.set_setting("event_log", json.dumps(log))
    return graded


def event_record(engine: Engine, now: float | None = None, days: float = 365) -> dict[str, Any]:
    """Graded events: how far they moved over two sessions, against what options priced."""
    import json
    import statistics as st

    now = now or time.time()
    log = [
        e
        for e in json.loads(engine.storage.get_setting("event_log") or "[]")
        if e.get("realized") is not None and e["ts"] >= now - days * 86400
    ]
    out: dict[str, Any] = {"n": len(log), "kinds": []}
    if not log:
        return out
    paired = [e for e in log if e.get("implied")]
    out["realized"] = round(st.median(e["realized"] for e in log), 2)
    if paired:
        out["implied"] = round(st.median(e["implied"] for e in paired), 2)
        out["paired"] = len(paired)
        out["beat_implied"] = round(
            sum(e["realized"] > e["implied"] for e in paired) / len(paired), 2
        )  # options underpriced it
    for k in BINARY:
        xs = [e for e in log if e["event"] == k]
        if xs:
            pr = [e for e in xs if e.get("implied")]
            out["kinds"].append({"event": k, "label": EVENT_LABEL[k], "n": len(xs), "realized": round(st.median(e["realized"] for e in xs), 2),
                                 "implied": round(st.median(e["implied"] for e in pr), 2) if pr else None,
                                 "beat_implied": round(sum(e["realized"] > e["implied"] for e in pr) / len(pr), 2) if pr else None})  # fmt: skip
    return out


async def send_event_day(engine: Engine, now: float | None = None) -> int:
    """08:00-09:30 ET on a market day: each binary event today on a phone's stocks (watched, held or
    with an open call), once, with both branches, what options price and what the phone holds."""
    import json

    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    if (
        dt.weekday() >= 5
        or dt.date().isoformat() in getattr(engine.calendar, "holidays", set())
        or not ((8, 0) <= (dt.hour, dt.minute) < (9, 30))
    ):
        return 0
    log_events(engine, now)
    if engine.webpush is None:
        return 0
    key = f"eventday:{dt.date().isoformat()}"
    sent_keys = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    for dev, p in list(engine.desk.profiles.items()):
        if not (p.get("prefs") or {}).get("event_day", True):
            continue
        for r in event_stakes(engine, dev, now, days=0)["rows"]:
            if r["event"] not in BINARY or not (r["mine"] or r["tracked"]):
                continue
            k = f"{dev}:{r['symbol']}:{r['event']}"
            if k in sent_keys:
                continue
            sent_keys.add(k)
            branches = [
                f"if {b['label']}: {b['close']:+.1f}% ({b['n']})" for b in r["branches"] if b.get("n")
            ]
            bits = []
            if r.get("implied_pct") is not None:
                bits.append(f"options price ±{r['implied_pct']:.1f}%")
            if r.get("value"):
                bits.append(
                    f"you hold ${r['value']:,.0f}"
                    + (f" (±${r['value'] * r['implied_pct'] / 100:,.0f})" if r.get("implied_pct") else "")
                )
            body = "\n".join(
                x
                for x in (
                    "Here, " + " · ".join(branches) if branches else "No record of either outcome here yet.",
                    " · ".join(bits),
                )
                if x
            )
            payload = {"id": f"ev-{r['symbol']}-{r['date']}", "title": f"📅 {r['symbol']}'s {EVENT_LABEL[r['event']]} is today",
                       "body": body[:400], "severity": "HIGH", "kind": "system", "tickers": [r["symbol"]],
                       "tag": f"ev-{r['symbol']}", "ts": now, "open": "/app/#a=calendar"}  # fmt: skip
            if await engine._to_device(dev, payload):
                sent += 1
    engine.storage.set_setting(key, json.dumps(sorted(sent_keys)))
    return sent
