"""The alert line, tested: headlines that scored just under it, graded as if they had gone out.

Everything else here grades alerts, so the line itself (the score a headline needs to become one)
has never been tested: a headline that scored 33 against a line of 35 vanished without a trace. Now
every headline naming a stock that scores within 15 points under the line, with a direction, is
kept in the shadow: its stock's price when it was read, and later its move an hour on and at its
session's close, its way, exactly like an alert (nothing is pushed or shown on the tape).

Pooled by how far under the line (0-5, 5-10, 10-15 points), beside the alerts just over it (their
headline scored 0-5 points over), the record says whether the line sits in the right place: "Just
under the line (30-35): 58% went their way by the close (median +0.4%, 41); alerts just over it:
55% (63)". When the band just under does as well as the alerts just over, on 15+ graded, it says
how many a day lowering the line by 5 would add. Per source, too: which feeds' near misses worked.
History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .dossier import session_closes

if TYPE_CHECKING:
    from .engine import Engine
    from .models import Analysis, NewsItem

ET = ZoneInfo("America/New_York")
NEAR = 15.0
BANDS = ((0, 5), (5, 10), (10, 15))
MIN_N = 15


def record(
    engine: Engine, item: NewsItem, analysis: Analysis, score: float, now: float | None = None
) -> bool:
    """Keep a headline that scored within NEAR points under the line, if it names a stock, has a
    direction and the stock has a live price. Once per headline."""
    line = engine.cfg.scoring.medium
    if not (line - NEAR <= score < line) or not analysis.tickers or analysis.direction not in ("up", "down"):
        return False
    now = now or time.time()
    sym = analysis.tickers[0]
    q = engine.price_of(sym, 900, now=now)
    if not q or not q[1]:
        return False
    data = {"symbol": sym, "sign": 1 if analysis.direction == "up" else -1, "ref": q[1], "score": round(score, 1),
            "line": line, "source": item.source, "title": item.title[:200]}  # fmt: skip
    return engine.storage.add_shadow(item.uid, now, data)


def grade(engine: Engine, now: float | None = None) -> int:
    """An hour on and at the session's close, from a quote within 3 hours of each moment."""
    now = now or time.time()
    holidays = engine.calendar.holidays
    made = 0
    for s in engine.storage.shadows(now - 3 * 86400, 3000):
        if "close" in s:
            continue
        q = engine.price_of(s["symbol"], now=now)
        if not q or not q[1] or q[0] < s["created"]:
            continue
        move = round((q[1] / s["ref"] - 1) * 100, 2)
        close_ts = session_closes(s["created"], 1, holidays)[0]
        new = dict(s)
        if "h1" not in s and now - s["created"] >= 3600 and q[0] - s["created"] < 3 * 3600:
            new["h1"] = move
        if now >= close_ts and abs(q[0] - close_ts) < 3 * 3600:
            new["close"] = move
        if new != s:
            sid = new.pop("id")
            new.pop("created")
            engine.storage.update_shadow(sid, new)
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


def line_report(engine: Engine, days: float = 60, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    line = engine.cfg.scoring.medium
    graded = [s for s in engine.storage.shadows(now - days * 86400) if s.get("close") is not None]
    sessions = max(1, len({datetime.fromtimestamp(s["created"], ET).date() for s in graded}))
    bands = []
    for lo, hi in BANDS:
        xs = [s["sign"] * s["close"] for s in graded if lo <= line - s["score"] < hi]
        bands.append(
            {
                "from": round(line - hi, 1),
                "to": round(line - lo, 1),
                "under": f"{lo}-{hi}",
                **(_stats(xs) or {"n": 0}),
            }
        )
    over = []
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        sc = (a.get("analysis") or {}).get("score")
        o = (a.get("edge") or {}).get("outcome") or {}
        if a.get("kind") != "news" or sc is None or not (line <= sc < line + 5) or not o:
            continue
        row = next(iter(o.values()))
        if row.get("sign") and row.get("close") is not None:
            over.append(row["sign"] * row["close"])
    just_over = _stats(over)
    srcs: dict[str, list[float]] = {}
    for s in graded:
        srcs.setdefault(s["source"], []).append(s["sign"] * s["close"])
    sources = sorted(
        ({"source": k, **(_stats(v) or {})} for k, v in srcs.items() if len(v) >= 5), key=lambda r: -r["n"]
    )[:8]
    import json

    log = json.loads(engine.storage.get_setting("line_log") or "[]")
    out: dict[str, Any] = {"line": line, "default": getattr(engine, "line_default", line), "log": log[-5:],
                           "days": days, "graded": len(graded), "bands": bands, "just_over": just_over,
                           "sources": sources, "need": MIN_N, "kept": len(engine.storage.shadows(now - days * 86400))}  # fmt: skip
    top = bands[0]
    if top["n"] >= MIN_N and just_over and just_over["n"] >= MIN_N:
        if top["hit"] >= just_over["hit"] - 0.03 and top["median"] > 0:
            out["verdict"] = "lower"
            out["line_text"] = (f"Headlines just under the line ({top['from']:g}-{top['to']:g}) did as well as alerts just over it: "
                                f"{round(top['hit'] * 100)}% vs {round(just_over['hit'] * 100)}% by the close. Lowering the line to "
                                f"{top['from']:g} would add about {top['n'] / sessions:.1f} alerts a day like them.")  # fmt: skip
        else:
            out["verdict"] = "keep"
            out["line_text"] = (f"The line holds: headlines just under it ({top['from']:g}-{top['to']:g}) went their way "
                                f"{round(top['hit'] * 100)}% by the close vs {round(just_over['hit'] * 100)}% for alerts just over it.")  # fmt: skip
    return out


def apply_line(engine: Engine) -> float:
    """Apply the stored line (a move made on the evidence) over the configured one. Returns the line."""
    import json

    raw = engine.storage.get_setting("scoring_line")
    v = json.loads(raw).get("medium") if raw else None
    engine.cfg.scoring.medium = float(v) if v is not None else engine.line_default
    return engine.cfg.scoring.medium


def set_line(engine: Engine, medium: float | None, why: str = "", now: float | None = None) -> dict[str, Any]:
    """Move the alert line (None: back to the configured one), logged. It must stay under HIGH and at
    10 or more."""
    import json

    now = now or time.time()
    old = engine.cfg.scoring.medium
    if medium is not None:
        medium = round(float(medium), 1)
        if not (10 <= medium < engine.cfg.scoring.high):
            raise ValueError(
                f"the line must be from 10 up to under the HIGH line ({engine.cfg.scoring.high:g})"
            )
        engine.storage.set_setting(
            "scoring_line", json.dumps({"medium": medium, "at": now, "why": why[:200]})
        )
    else:
        engine.storage.db.execute("DELETE FROM settings WHERE key = 'scoring_line'")
    new = apply_line(engine)
    log = json.loads(engine.storage.get_setting("line_log") or "[]")
    log.append(
        {
            "at": now,
            "from": old,
            "to": new,
            "why": why[:200] or ("back to the configured line" if medium is None else ""),
        }
    )
    engine.storage.set_setting("line_log", json.dumps(log[-50:]))
    return {"line": new, "default": engine.line_default, "log": log[-10:]}
