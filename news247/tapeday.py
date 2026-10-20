"""The tape today: some days the news works, some days nothing does. Say which, early.

Every alert is graded an hour in. On a cold day the first alerts fail together: risk-off, a heavy
tape, sellers into every headline. A trader who notices by late morning sits the rest of the day out.
One who doesn't keeps paying. This grades whether the early read predicts the rest:
- each past session's first 8 directional news alerts in the session, an hour in: **cold** when 3
  or fewer went their way, **hot** when 6 or more did, else normal;
- the rest of that day's alerts, by the close.

Pooled over the sessions on file: "on cold days here, the rest of the day's alerts went their way
38% by the close (median -0.6%, 14 sessions) vs 56% on normal days". Live, once today's first 8 have
their hour, the tape is read. When it's cold or hot and that state's record differs from normal by
10+ points on 5+ sessions, every phone gets one push that day (Settings → The tape today). The Watch
tab shows today's read beside the record. History, not a forecast.
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
FIRST = 8
MIN_DAYS = 5
LABEL = {"cold": "a cold tape", "hot": "a hot tape", "normal": "a normal tape"}


def _in_session(ts: float) -> bool:
    dt = datetime.fromtimestamp(ts, ET)
    return dt.weekday() < 5 and (9, 30) <= (dt.hour, dt.minute) < (16, 0)


def _row(a: dict[str, Any]) -> dict[str, Any] | None:
    o = (a.get("edge") or {}).get("outcome") or {}
    row = next(iter(o.values()), None) if o else None
    return row if row and row.get("sign") else None


def state_of(first: list[dict[str, Any]]) -> str | None:
    """cold / hot / normal from the first alerts' hour, or None until all have one."""
    if len(first) < FIRST:
        return None
    hs = [(_row(a) or {}).get("h1") for a in first[:FIRST]]
    if any(h is None for h in hs):
        return None
    won = sum(1 for a, h in zip(first[:FIRST], hs, strict=True) if (_row(a) or {})["sign"] * h > 0)
    return "cold" if won <= 3 else "hot" if won >= 6 else "normal"


def _days(engine: Engine, since: float, until: float) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for a in engine.storage.alerts_between(since, until, 20000):
        if a.get("kind") != "news" or not _row(a) or not _in_session(float(a["created"])):
            continue
        by.setdefault(datetime.fromtimestamp(float(a["created"]), ET).date().isoformat(), []).append(a)
    for xs in by.values():
        xs.sort(key=lambda a: float(a["created"]))
    return by


def tape_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{state: {"state", "sessions", "n", "hit", "median"}}: the rest of the day's alerts by the close."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, ET).date().isoformat()
    pools: dict[str, list[float]] = {}
    sessions: dict[str, int] = {}
    for day, xs in _days(engine, now - days * 86400, now).items():
        if day == today:
            continue
        stt = state_of(xs)
        rest = [r["sign"] * r["close"] for a in xs[FIRST:] if (r := _row(a)) and r.get("close") is not None]
        if not stt or not rest:
            continue
        pools.setdefault(stt, []).extend(rest)
        sessions[stt] = sessions.get(stt, 0) + 1
    return {s: {"state": s, "sessions": sessions[s], "n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                "median": round(st.median(xs), 2)} for s, xs in pools.items()}  # fmt: skip


def today(engine: Engine, now: float | None = None) -> dict[str, Any]:
    """Today's read: the first alerts' hour so far, the state once it's known, and its record."""
    now = now or time.time()
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    xs = _days(engine, day0, now).get(datetime.fromtimestamp(now, ET).date().isoformat(), [])
    first = xs[:FIRST]
    hs = [(r["sign"] * r["h1"]) for a in first if (r := _row(a)) and r.get("h1") is not None]
    stt = state_of(xs)
    rates = getattr(engine, "tape_rates", None) or {}
    return {"alerts": len(xs), "first": len(first), "graded": len(hs), "won": sum(1 for h in hs if h > 0),
            "state": stt, "record": rates.get(stt) if stt else None, "normal": rates.get("normal"), "need": FIRST}  # fmt: skip


def tape_line(t: dict[str, Any]) -> str:
    r, n = t.get("record"), t.get("normal")
    if not t.get("state") or not r:
        return ""
    vs = f" vs {round(n['hit'] * 100)}% on normal days" if n and t["state"] != "normal" else ""
    return (f"{t['won']} of the first {FIRST} alerts went their way an hour in. On days like it here, the rest of the day's alerts "
            f"went their way {round(r['hit'] * 100)}% by the close (median {r['median']:+.1f}%, {r['sessions']} sessions){vs}.")  # fmt: skip


async def send_tape(engine: Engine, now: float | None = None) -> int:
    """Once a day, when today's tape is cold or hot and that state's record clearly differs from normal."""
    now = now or time.time()
    if engine.webpush is None or not _in_session(now):
        return 0
    key = f"tapeday:{datetime.fromtimestamp(now, ET).date().isoformat()}"
    if engine.storage.get_setting(key):
        return 0
    t = today(engine, now)
    r, n = t["record"], t["normal"]
    if (
        t["state"] not in ("cold", "hot")
        or not r
        or not n
        or r["sessions"] < MIN_DAYS
        or n["sessions"] < MIN_DAYS
    ):
        return 0
    if abs(r["hit"] - n["hit"]) < 0.10:
        return 0
    engine.storage.set_setting(key, json.dumps({"state": t["state"], "at": now}))
    icon, word = ("🧊", "A cold tape today") if t["state"] == "cold" else ("🔥", "A hot tape today")
    sent = 0
    for dev, p in list(engine.desk.profiles.items()):
        if not (p.get("prefs") or {}).get("tapeday", True):
            continue
        payload = {"id": f"tp-{key[8:]}", "title": f"{icon} {word}", "body": tape_line(t)[:400], "severity": "MEDIUM",
                   "kind": "system", "tickers": [], "tag": "tapeday", "ts": now, "open": "/app/#a=watch"}  # fmt: skip
        if await engine._to_device(dev, payload, urgency="normal"):
            sent += 1
    return sent
