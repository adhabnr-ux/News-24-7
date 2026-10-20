"""Your week in evidence: what the week's graded alerts say about this phone's choices and tools.

The Friday card already counts the week's calls. This adds what they mean:

- **Your picks against what you passed on:** the alerts this phone tracked or saved, against the
  ones that reached it (pushed, or opened) and it let go, by the close. Whether its discretion
  adds anything over the alerts themselves is the most useful thing a trader can know about
  their own process.
- **The odds model's week:** what it said, on average, against how many went their way.
- **How the calls ended:** stopped out, target reached, cut on the live odds, dead money...
- **The costliest setting:** the one that held back better alerts than it let through.
- **Why the week's misses missed:** the sector, the market, new news, or the signal.
- **The kinds of news that worked, and the ones that didn't.**

Everything from the week's own grades. History, not a forecast.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_KIND = 3  # graded alerts of a kind this week before it's called best or worst


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "avg": round(sum(xs) / len(xs), 2),
    }


def _ending(lesson: str) -> str:
    s = (lesson or "").lower()
    for key, label in (("stop", "stopped out"), ("target", "target reached"), ("live odds", "cut on the live odds"),
                       ("dead money", "dead money"), ("gave back", "gave back the gain"), ("an hour later", "closed by time"),
                       ("at the close", "closed by time"), ("next day", "closed by time")):  # fmt: skip
        if key in s:
            return label
    return "closed by hand"


def week_evidence(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    from .catalysts import LABELS, classify
    from .keptback import settings_cost
    from .misses import miss_report

    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    w0 = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    p = engine.desk.profiles.get(device) or {}
    wp = engine.webpush
    log = list((wp.log.get(device) or []) if wp is not None else [])  # type: ignore[attr-defined]
    seen = {e["id"] for e in log if e.get("status") == "sent"} | set(p.get("opened") or [])
    picks = {
        t.get("alert_id") for t in [*(p.get("tracked") or []), *(p.get("closed") or [])] if t.get("alert_id")
    }
    picks |= set(p.get("saved") or [])
    picked, passed, everything = [], [], []
    said, went = [], []
    kinds: dict[str, list[float]] = {}
    for a in engine.storage.recent_alerts(3000, since=w0):
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("close") is None:
            continue
        r = row["sign"] * row["close"]
        everything.append(r)
        if a["id"] in picks:
            picked.append(r)
        elif a["id"] in seen:
            passed.append(r)
        od = ((a.get("edge") or {}).get("odds") or {}).get("p")
        if od is not None:
            said.append(float(od))
            went.append(r > 0)
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        if c:
            kinds.setdefault(c["key"], []).append(r)
    rows = [
        {"key": k, "label": LABELS.get(k, k), **(_stats(xs) or {})}
        for k, xs in kinds.items()
        if len(xs) >= MIN_KIND
    ]
    rows.sort(key=lambda r: -r["avg"])
    closed = [
        c for c in p.get("closed") or [] if (c.get("closed") or 0) >= w0 and c.get("pnl_pct") is not None
    ]
    endings: dict[str, list[float]] = {}
    for c in closed:
        endings.setdefault(_ending(c.get("lesson", "")), []).append(c["pnl_pct"])
    cost = settings_cost(engine, device, 7, now)
    costly = next((g for g in cost["gates"] if g["verdict"] == "costing"), None)
    ms = miss_report(engine, 7, now)
    from .exits import exit_report

    ex = exit_report(engine, device, 28, now)  # a month of exits: a week is too few to read
    exits = [i for i in ex["insights"] if i["key"] != "edge"]
    return {
        "week_of": datetime.fromtimestamp(w0, ET).date().isoformat(),
        "picks": _stats(picked),
        "passed": _stats(passed),
        "all": _stats(everything),
        "model": {"n": len(said), "said": round(sum(said) / len(said), 2), "went": round(sum(went) / len(went), 2)} if said else None,
        "endings": sorted(({"ending": k, **(_stats(v) or {})} for k, v in endings.items()), key=lambda r: -r["n"]),
        "costly": {"setting": costly["setting"], "n": costly["n"], "hit": costly["hit"], "avg": costly["avg"], "fix": costly["fix"]} if costly else None,
        "misses": {"n": ms["n"], "causes": ms["causes"], "failed": ms["failed"]} if ms["n"] else None,
        "exits": {"graded": ex["graded"], "insights": exits[:2], "edge": ex.get("edge")} if ex["insights"] else None,
        "best": [r for r in rows if r["avg"] > 0][:2],
        "worst": [r for r in reversed(rows) if r["avg"] < 0][:2],
    }  # fmt: skip


def evidence_lines(ev: dict[str, Any]) -> list[str]:
    """The week's evidence for the Friday push, in a few lines."""
    lines = []
    pk, ps = ev.get("picks"), ev.get("passed")
    if pk and ps:
        better = "beat" if pk["avg"] > ps["avg"] else "trailed"
        lines.append(
            f"Your picks {better} what you passed on: {pk['avg']:+.1f}% vs {ps['avg']:+.1f}% by the close ({pk['n']} vs {ps['n']})"
        )
    m = ev.get("model")
    if m and m["n"] >= 5:
        lines.append(
            f"The odds model said {round(m['said'] * 100)}%; {round(m['went'] * 100)}% went their way ({m['n']})"
        )
    ms = ev.get("misses")
    if ms and ms["n"] >= 3:
        swamped = ms["causes"]["sector"] + ms["causes"]["market"]
        lines.append(
            f"Misses: {swamped} of {ms['n']} swamped by the sector or market, {ms['causes']['faded']} the signal"
        )
    ex = ev.get("exits")
    if ex and ex["insights"]:
        lines.append(ex["insights"][0]["text"])
    if ev.get("best"):
        b = ev["best"][0]
        lines.append(f"Worked: {b['label'].lower()} news {b['avg']:+.1f}% ({b['n']})")
    return lines
