"""Tune my pushes: replay the last month of graded alerts through every edge bar and budget, for
this phone, and see what each would have delivered.

The settings that decide what reaches a phone (an edge bar on the record or the odds, a daily
budget) are easy to set and hard to judge. This walks every graded alert of the last 30 days that
would have reached the phone on severity alone through the same gates the push does
(desk.block, durable: passing states like focus left out), once per combination, on a copy of the
phone's settings, and grades what would have come through: pushes a day, how often they went
their way by the close, and the average move. The phone's current settings are the baseline.

Two picks come back: **Quality** (the highest average move that still pushes once a day or more)
and **Balanced** (the most total edge: average move times the square root of how many). One tap
applies either. History, not a forecast: a month is a small sample, and the past month's best
settings aren't promised to stay best.
"""

from __future__ import annotations

import copy
import json
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .models import Severity

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
EDGES = (None, "hit50", "hit60", "odds55", "odds60", "odds65", "call")
BUDGETS = (0, 3, 5, 10)
MIN_N = 10  # pushes in the month before a combination is worth recommending
TMP = "dev_tune_sim_000"  # a scratch profile id, never saved


def _label(edge: str | None, budget: int) -> str:
    e = {None: "any record", "hit50": "records 50%+", "hit60": "records 60%+", "odds55": "odds 55%+",
         "odds60": "odds 60%+", "odds65": "odds 65%+", "call": "the call says act or wait"}[edge]  # fmt: skip
    return f"{e}, {f'{budget} a day' if budget else 'no budget'}"


def _graded(
    engine: Engine, device: str, days: float, now: float
) -> tuple[list[tuple[dict[str, Any], float]], int]:
    """(alert, its way by the close) for each graded alert that would reach this phone on severity
    alone, and the number of market days they span."""
    from .dossier import _direction
    from .pushcheck import phone_floor

    p = engine.desk.profiles.get(device) or {}
    floor = phone_floor(engine) or Severity.HIGH
    mine = set(p.get("watchlist") or []) | set(p.get("holdings") or {})
    out, dates = [], set()
    for a in engine.storage.recent_alerts(4000, since=now - days * 86400):
        if a.get("kind") == "system" or not _direction(a):
            continue
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("close") is None:
            continue
        try:
            sev = Severity.parse(str(a.get("severity") or "LOW"))
        except KeyError:
            continue
        if not (sev >= floor or (sev == Severity.MEDIUM and mine & set(a.get("tickers") or []))):
            continue
        out.append((a, round(row["sign"] * row["close"], 2)))
        dates.add(datetime.fromtimestamp(a["created"], ET).date())
    return out, max(1, len(dates))


def _stats(rets: list[float], days: int) -> dict[str, Any]:
    if not rets:
        return {"n": 0, "per_day": 0.0, "hit": None, "avg": None}
    return {"n": len(rets), "per_day": round(len(rets) / days, 1), "hit": round(sum(r > 0 for r in rets) / len(rets), 2),
            "avg": round(sum(rets) / len(rets), 2)}  # fmt: skip


def tune(engine: Engine, device: str, days: float = 30, now: float | None = None) -> dict[str, Any]:
    """{"current", "options": [{edge_min, budget, label, n, per_day, hit, avg, ...}], "quality", "balanced"}"""
    now = now or time.time()
    p = engine.desk.profiles.get(device)
    if p is None:
        return {
            "days": days,
            "sessions": 0,
            "current": _stats([], 1),
            "options": [],
            "quality": None,
            "balanced": None,
        }
    alerts, sessions = _graded(engine, device, days, now)

    def run(prefs: dict[str, Any]) -> dict[str, Any]:
        sim = copy.deepcopy(p)
        sim["prefs"] = {**(sim.get("prefs") or {}), **prefs}
        engine.desk.profiles[TMP] = sim
        try:
            rets = [
                r
                for a, r in alerts
                if engine.desk.block(TMP, a, float(a.get("created") or now), durable=True) is None
            ]
        finally:
            engine.desk.profiles.pop(TMP, None)
        return _stats(rets, sessions)

    prefs = p.get("prefs") or {}
    current = {**run({}), "edge_min": prefs.get("edge_min"), "budget": prefs.get("budget") or 0}
    current["label"] = _label(current["edge_min"], current["budget"])
    options = []
    for edge in EDGES:
        for budget in BUDGETS:
            s = run({"edge_min": edge, "budget": budget})
            options.append({"edge_min": edge, "budget": budget, "label": _label(edge, budget), **s})
    ok = [o for o in options if o["n"] >= MIN_N and o["avg"] is not None]
    quality = max((o for o in ok if o["per_day"] >= 1.0), key=lambda o: (o["avg"], o["n"]), default=None)
    balanced = max(ok, key=lambda o: (o["avg"] * math.sqrt(o["n"]), o["n"]), default=None)
    for pick in (quality, balanced):
        if pick is not None and current["avg"] is not None:
            pick["vs_avg"] = round(pick["avg"] - current["avg"], 2)
    same = lambda o: o and o["edge_min"] == current["edge_min"] and o["budget"] == current["budget"]  # noqa: E731
    return {"days": days, "sessions": sessions, "current": current, "options": options,
            "quality": None if same(quality) else quality, "balanced": None if same(balanced) else balanced}  # fmt: skip


_CACHE: dict[str, tuple[float, str, dict[str, Any]]] = {}


def tune_cached(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    """The same, kept 10 minutes per phone (or until its settings change)."""
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    sig = json.dumps(
        [p.get(k) for k in ("prefs", "cat_off", "kw_neg", "stock_push", "watchlist", "holdings")],
        sort_keys=True,
        default=str,
    )
    hit = _CACHE.get(device)
    if hit and hit[1] == sig and now - hit[0] < 600:
        return hit[2]
    r = tune(engine, device, 30, now)
    _CACHE[device] = (now, sig, r)
    return r
