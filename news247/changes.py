"""Your changes, graded: did each change made on the evidence hold up afterwards?

The advice, the line test and the line per kind each recommend a change, and a tap makes it. Until
now nothing checked the change afterwards. Every change is now graded on what came after it:

- **A kind of news switched off on this phone** (from the advice): its alerts since the switch,
  graded by the close. 45% or less went their way: the change holds. 55%+ on 10+: it backfired
  (winners are being kept off this phone) and a tap switches the kind back on.
- **The main line or a kind's line moved down:** the alerts that only exist because of the move
  (scored between the new line and the old) graded by the close. As good as the alerts above the
  old line, within 5 points: it holds. Worse: it backfired, and a tap puts the line back.
- **A line moved up:** the headlines it kept in the shadow (scored between the old line and the
  new) graded the same way. If they went their way 55%+ on 10+, the raise is keeping winners back.

A change needs 10 graded before a verdict; until then it shows its count. History, not a forecast.
"""

from __future__ import annotations

import json
import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 10


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def _its(a: dict[str, Any]) -> float | None:
    row = next(iter(((a.get("edge") or {}).get("outcome") or {}).values()), None) or {}
    if not row.get("sign") or row.get("close") is None:
        return None
    return row["sign"] * row["close"]


def _kind(engine: Engine, a: dict[str, Any]) -> str | None:
    c = (a.get("edge") or {}).get("catalyst")
    if c:
        return c["key"]
    from .linekinds import kind_of

    return kind_of(engine, a.get("title") or "", a.get("tickers") or [])


def kind_offs(engine: Engine, device: str, now: float | None = None) -> list[dict[str, Any]]:
    """Kinds of news this phone switched off from the advice, each graded since the switch."""
    from .catalysts import LABELS

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    applied = p.get("advice_applied") or {}
    off = set(p.get("cat_off") or [])
    rows = []
    for key, at in applied.items():
        if not key.startswith("off:"):
            continue
        kind = key[4:]
        xs = [x for a in engine.storage.alerts_between(float(at), now, 4000)
              if a.get("kind") == "news" and _kind(engine, a) == kind and (x := _its(a)) is not None]  # fmt: skip
        s = _stats(xs)
        verdict = "thin"
        if s and s["n"] >= MIN_N:
            verdict = "backfired" if s["hit"] >= 0.55 else "holds" if s["hit"] <= 0.45 else "even"
        rows.append({"key": key, "kind": kind, "what": f"Switched off {LABELS.get(kind, kind).lower()} news", "at": at,
                     "still": kind in off, "since": s, "verdict": verdict,
                     "undo": {"action": "cat_mute", "body": {"key": kind, "on": False}, "label": "Switch it back on"}
                     if verdict == "backfired" and kind in off else None})  # fmt: skip
    return rows


def line_moves(engine: Engine, now: float | None = None) -> list[dict[str, Any]]:
    """Each move of the main line or a kind's line, graded by what it added or kept back since."""
    now = now or time.time()
    log = json.loads(engine.storage.get_setting("line_log") or "[]")
    rows = []
    for i, m in enumerate(log):
        lo, hi = sorted((float(m["from"]), float(m["to"])))
        if lo == hi:
            continue
        kind = m.get("kind")
        end = next(
            (n["at"] for n in log[i + 1 :] if n.get("kind") == kind), now
        )  # until the next move of the same line
        lowered = m["to"] < m["from"]
        added: list[float] = []
        above: list[float] = []
        if lowered:  # the alerts that exist because of the move, against those over the old line
            for a in engine.storage.alerts_between(float(m["at"]), float(end), 6000):
                sc = (a.get("analysis") or {}).get("score")
                if a.get("kind") != "news" or sc is None or (kind and _kind(engine, a) != kind):
                    continue
                x = _its(a)
                if x is None:
                    continue
                if lo <= sc < hi:
                    added.append(x)
                elif hi <= sc < hi + 5:
                    above.append(x)
        else:  # the headlines the raise kept in the shadow
            for s in engine.storage.shadows(float(m["at"])):
                if s["created"] > end or s.get("close") is None or (kind and s.get("kind") != kind):
                    continue
                if lo <= s["score"] < hi:
                    added.append(s["sign"] * s["close"])
        got, ref = _stats(added), _stats(above)
        verdict = "thin"
        if got and got["n"] >= MIN_N:
            if lowered:
                verdict = "holds" if not ref or got["hit"] >= ref["hit"] - 0.05 else "backfired"
            else:
                verdict = "backfired" if got["hit"] >= 0.55 else "holds"
        who = f"the line for {m.get('label', kind).lower()} news" if kind else "the alert line"
        undo = None
        if verdict == "backfired" and end == now:  # only the line as it stands can be put back
            undo = ({"api": "/api/line/kind", "body": {"kind": kind, "offset": None, "why": "the move backfired"}} if kind
                    else {"api": "/api/line", "body": {"medium": m["from"], "why": "the move backfired"}})  # fmt: skip
            undo["label"] = f"Put it back to {m['from']:g}"
        rows.append({"what": f"{'Lowered' if lowered else 'Raised'} {who} {m['from']:g} → {m['to']:g}", "at": m["at"],
                     "kind": kind, "lowered": lowered, "since": got, "ref": ref, "verdict": verdict, "undo": undo,
                     "current": end == now})  # fmt: skip
    return rows[::-1]


def changes(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    return {"mine": kind_offs(engine, device, now), "lines": line_moves(engine, now)[:10], "need": MIN_N}
