"""The alert line, per kind of news.

The line test (shadow.py) grades headlines that scored just under the line beside the alerts just
over it, and moves one line for everything. But a score means different things for different news:
contract wins just under the line may run like the alerts above it, while analyst notes just over
it fade. Pooled by kind of news, the same test says where each kind's line belongs:

- **Lower it 5** for a kind whose headlines just under its line (0-5 points) went their way by the
  close as often as its alerts just over it (within 3 points), with a positive median, 15+ graded
  each side.
- **Raise it 5** for a kind whose alerts just over its line went their way 40% or less with a
  negative median (15+ graded): they cost attention and lose.

A kind's line moves in steps of 5 and at most 10 either way from the main line, never to HIGH. Each
move is one tap, logged beside the main line's moves and reversible; headlines are read against
their kind's line when they arrive (a lowered kind's near misses become MEDIUM alerts, a raised
kind's become shadows, graded as before). History, not a forecast.
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
MIN_N = 15
STEP = 5.0
MAX_OFF = 10.0
KEY = "line_kinds"


def kind_of(engine: Engine, title: str, tickers: list[str]) -> str | None:
    """The kind of news a headline is (catalysts.py), or None."""
    from .catalysts import classify

    c = classify({"kind": "news", "title": title, "tickers": tickers}, getattr(engine, "universe", None))
    return c["key"] if c else None


def offsets(engine: Engine) -> dict[str, float]:
    """{kind: points from the main line} as moved on the evidence."""
    raw = engine.storage.get_setting(KEY)
    try:
        return {k: float(v) for k, v in (json.loads(raw) if raw else {}).items()}
    except (ValueError, TypeError, AttributeError):
        return {}


def line_for(engine: Engine, kind: str | None) -> float:
    """The line a headline of this kind is read against."""
    base = engine.cfg.scoring.medium
    return base + (getattr(engine, "line_offsets", None) or {}).get(kind or "", 0.0)


def severity_line(
    engine: Engine, title: str, tickers: list[str], score: float
) -> tuple[bool, float, str | None]:
    """(clears its kind's line, that line, the kind) for a headline under HIGH."""
    kind = kind_of(engine, title, tickers) if getattr(engine, "line_offsets", None) else None
    line = line_for(engine, kind)
    return score >= line, line, kind


def set_kind_line(
    engine: Engine, kind: str, offset: float | None, why: str = "", now: float | None = None
) -> dict[str, Any]:
    """Move one kind's line (None: back to the main line), logged with the main line's moves."""
    from .catalysts import LABELS

    now = now or time.time()
    if kind not in LABELS:
        raise ValueError("unknown kind of news")
    offs = offsets(engine)
    old = offs.get(kind, 0.0)
    if offset is None or float(offset) == 0:
        offs.pop(kind, None)
        new = 0.0
    else:
        new = round(float(offset) / STEP) * STEP
        if abs(new) > MAX_OFF:
            raise ValueError(f"a kind's line moves at most {MAX_OFF:g} points from the main line")
        if engine.cfg.scoring.medium + new >= engine.cfg.scoring.high or engine.cfg.scoring.medium + new < 10:
            raise ValueError(
                f"the line must stay from 10 up to under the HIGH line ({engine.cfg.scoring.high:g})"
            )
        offs[kind] = new
    engine.storage.set_setting(KEY, json.dumps(offs))
    engine.line_offsets = offs
    log = json.loads(engine.storage.get_setting("line_log") or "[]")
    base = engine.cfg.scoring.medium
    log.append({"at": now, "kind": kind, "label": LABELS[kind], "from": base + old, "to": base + new,
                "why": why[:200] or ("back to the main line" if not new else "")})  # fmt: skip
    engine.storage.set_setting("line_log", json.dumps(log[-50:]))
    return {"kind": kind, "offset": new, "line": base + new, "offsets": offs}


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def kind_report(engine: Engine, days: float = 60, now: float | None = None) -> list[dict[str, Any]]:
    """Per kind of news: its line, the headlines just under it and the alerts just over it, graded
    by the close, and a verdict (lower, raise, keep, or too few to say)."""
    from .catalysts import LABELS

    now = now or time.time()
    offs = offsets(engine)
    base = engine.cfg.scoring.medium
    under: dict[str, list[float]] = {}
    days_seen: dict[str, set[Any]] = {}
    for s in engine.storage.shadows(now - days * 86400):
        if s.get("close") is None:
            continue
        kind = s.get("kind") or kind_of(engine, s.get("title") or "", [s["symbol"]])
        if not kind:
            continue
        line = s.get("line", base)
        if 0 <= line - s["score"] < STEP:
            under.setdefault(kind, []).append(s["sign"] * s["close"])
            days_seen.setdefault(kind, set()).add(datetime.fromtimestamp(s["created"], ET).date())
    over: dict[str, list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        sc = (a.get("analysis") or {}).get("score")
        o = (a.get("edge") or {}).get("outcome") or {}
        if a.get("kind") != "news" or sc is None or not o:
            continue
        c = (a.get("edge") or {}).get("catalyst")
        kind = c["key"] if c else kind_of(engine, a.get("title") or "", a.get("tickers") or [])
        if not kind:
            continue
        line = base + offs.get(kind, 0.0)
        row = next(iter(o.values()))
        if line <= sc < line + STEP and row.get("sign") and row.get("close") is not None:
            over.setdefault(kind, []).append(row["sign"] * row["close"])
    rows = []
    for kind in sorted(set(under) | set(over) | set(offs)):
        u, ov = _stats(under.get(kind, [])), _stats(over.get(kind, []))
        off = offs.get(kind, 0.0)
        line = base + off
        r: dict[str, Any] = {"kind": kind, "label": LABELS.get(kind, kind), "offset": off, "line": line,
                             "under": u, "over": ov, "verdict": "thin", "text": ""}  # fmt: skip
        if (
            u
            and ov
            and u["n"] >= MIN_N
            and ov["n"] >= MIN_N
            and u["hit"] >= ov["hit"] - 0.03
            and u["median"] > 0
            and off > -MAX_OFF
            and line - STEP >= 10
        ):
            per_day = u["n"] / max(1, len(days_seen.get(kind, set())))
            r.update(verdict="lower", to=line - STEP,
                     text=(f"{r['label']} headlines just under their line ({line - STEP:g}-{line:g}) went their way {round(u['hit'] * 100)}% "
                           f"by the close vs {round(ov['hit'] * 100)}% for its alerts just over it. Lowering its line to {line - STEP:g} "
                           f"would add about {per_day:.1f} alerts a day like them."))  # fmt: skip
        elif (
            ov
            and ov["n"] >= MIN_N
            and ov["hit"] <= 0.40
            and ov["median"] < 0
            and off < MAX_OFF
            and line + STEP < engine.cfg.scoring.high
        ):
            r.update(verdict="raise", to=line + STEP,
                     text=(f"{r['label']} alerts just over their line ({line:g}-{line + STEP:g}) went their way {round(ov['hit'] * 100)}% "
                           f"by the close (median {ov['median']:+.1f}%, {ov['n']}). Raising its line to {line + STEP:g} would keep them "
                           f"in the shadow, still graded."))  # fmt: skip
        elif u and ov and u["n"] >= MIN_N and ov["n"] >= MIN_N:
            r["verdict"] = "keep"
        rows.append(r)
    order = {"lower": 0, "raise": 1, "keep": 2, "thin": 3}
    rows.sort(
        key=lambda r: (order[r["verdict"]], -((r["under"] or {}).get("n", 0) + (r["over"] or {}).get("n", 0)))
    )
    return rows
