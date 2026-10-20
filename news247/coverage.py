"""How far the story spread, and how late the alert was to it.

Every headline is filed into a story with the other outlets' versions of it (storage: items.story_id),
first one first. Two facts fall out of that for each news alert, and both are graded:

- **How late:** minutes from the story's first headline (from any source) to the one this alert
  came from. An alert *first* (within 2 minutes) has the news before most of the market; one *15+
  minutes into its story* is reporting what others already have, and the tape may have it priced.
- **How far it spread:** how many different outlets carried the story within an hour of its first
  headline. A single-source story is either a scoop or nothing; four or more is news everyone saw.

Both are stamped on each alert an hour after it (edge.coverage), and alerts from before this note
are filled in from the stories already on file (a few hundred per pass), so the record starts from
the whole history, not from today. A new alert carries its lateness at once; when it's 15+ minutes
late and late alerts have a record, the push says so. The sheet shows how far its story has spread
so far beside the record for that breadth. History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
LATE = (("first", "first to it (within 2 minutes)"), ("early", "2-15 minutes into its story"),
        ("late", "15+ minutes into its story"))  # fmt: skip
WIDE = (("one", "one outlet"), ("few", "2-3 outlets"), ("many", "4+ outlets"))  # fmt: skip
LATE_LABEL, WIDE_LABEL = dict(LATE), dict(WIDE)


def late_bin(minutes: float) -> str:
    return "first" if minutes < 2 else "early" if minutes < 15 else "late"


def wide_bin(n: int) -> str:
    return "one" if n <= 1 else "few" if n <= 3 else "many"


def read(engine: Engine, a: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """{"late": minutes after the story's first headline, "first_source", "n30", "n60", "sources"}
    for a news alert from its stored story; None without a source item."""
    item = a.get("item") or {}
    uid = item.get("uid")
    if not uid:
        return None
    detected = float(item.get("detected") or a.get("created") or 0)
    story = engine.storage.story_of(uid)
    srcs = engine.storage.story_sources(story) if story is not None else []
    if not srcs:
        return {"late": 0.0, "first_source": item.get("source") or "", "n30": 1, "n60": 1, "sources": 1}
    t0 = min(float(s["detected"]) for s in srcs)
    now = now or time.time()
    return {"late": round(max(0.0, detected - t0) / 60, 1), "first_source": srcs[0]["source"],
            "n30": sum(1 for s in srcs if float(s["detected"]) - t0 <= 1800),
            "n60": sum(1 for s in srcs if float(s["detected"]) - t0 <= 3600 and float(s["detected"]) <= now),
            "sources": len(srcs)}  # fmt: skip


def stamp_coverage(engine: Engine, now: float | None = None, batch: int = 300) -> int:
    """Stamp edge.coverage on news alerts an hour old or more (newest first, back 180 days), a batch
    per pass, so the history fills in and new alerts follow an hour after they're sent."""
    now = now or time.time()
    made = 0
    for a in engine.storage.alerts_without("coverage", now - 3600, now - 180 * 86400, batch):
        c = read(engine, a, now)
        if c is None:
            continue
        engine.storage.update_alert_edge(a["id"], {"coverage": c})
        made += 1
    return made


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def coverage_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"late:first": {...}, "wide:many": {...}, ...}: graded news alerts by how late they came to
    their story and how far it spread in its first hour; an hour later and at the close, its way."""
    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        c = e.get("coverage")
        o = e.get("outcome") or {}
        if not c or not o:
            continue
        row = next(iter(o.values()))
        if not row.get("sign"):
            continue
        for k in (f"late:{late_bin(c['late'])}", f"wide:{wide_bin(c['n60'])}"):
            g = pools.setdefault(k, {"h1": [], "close": []})
            for h in ("h1", "close"):
                if row.get(h) is not None:
                    g[h].append(row["sign"] * row[h])
    out = {}
    for k, g in pools.items():
        dim, b = k.split(":")
        out[k] = {"key": k, "dim": dim, "bin": b, "label": (LATE_LABEL if dim == "late" else WIDE_LABEL)[b].capitalize(),
                  "h1": _stats(g["h1"]), "close": _stats(g["close"])}  # fmt: skip
    return out


def _usable(r: dict[str, Any] | None) -> bool:
    return bool(r and r.get("close") and r["close"]["n"] >= MIN_N)


def late_line(minutes: float | None, first_source: str, rates: dict[str, Any]) -> str:
    """The push's warning for an alert 15+ minutes into its story, with the record for late alerts."""
    if minutes is None or late_bin(minutes) != "late":
        return ""
    r = rates.get("late:late")
    rec = (f": alerts that late went their way {round(r['close']['hit'] * 100)}% by the close "
           f"(median {r['close']['median']:+.1f}%, {r['close']['n']})") if _usable(r) else ""  # fmt: skip
    src = f" ({first_source})" if first_source else ""
    return f"🕰 Late to the story: it first broke {minutes:.0f} minutes earlier{src}{rec}"


def coverage_board(rates: dict[str, Any]) -> dict[str, Any]:
    order = {"first": 0, "early": 1, "late": 2, "one": 0, "few": 1, "many": 2}
    rows = sorted(rates.values(), key=lambda r: (r["dim"] != "late", order[r["bin"]]))
    return {"late": [r for r in rows if r["dim"] == "late"], "wide": [r for r in rows if r["dim"] == "wide"],
            "need": MIN_N}  # fmt: skip
