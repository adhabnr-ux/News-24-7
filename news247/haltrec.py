"""Halts, graded: what a stock did after trading resumed, by why it was halted and which way it was going.

A trading halt is the most decisive moment a small cap has: a volatility pause (LUDP) on the way up,
news pending (T1), news released (T2). Halts reach watchers' phones, and so do resumptions, but what
followed was never recorded. Now every halt notes its stock's price and the day's move into it (the
direction it was going). From the resumption, the stock is graded 5 and 30 minutes later and at the
close, against the halt price, "its way" meaning the way it was going.

Pooled by halt code and direction ("LUDP|up"), the record says whether halts like it keep going:
"Volatility pauses on the way up: kept going 5 minutes after the resumption 58%, by the close 44%
(31)". The resumption push carries its halt's record (8+ graded), and the Watch tab has the board.
History, not a forecast; halts in thinly quoted stocks can't be graded and are left out.
"""

from __future__ import annotations

import json
import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

KEY = "halts:log"
KEEP_DAYS = 180
MIN_N = 8
STEPS = (("m5", 300), ("m30", 1800))


def _log(engine: Engine) -> list[dict[str, Any]]:
    return json.loads(engine.storage.get_setting(KEY) or "[]")


def _save(engine: Engine, rows: list[dict[str, Any]], now: float) -> None:
    rows = [r for r in rows if now - r["ts"] < KEEP_DAYS * 86400][-4000:]
    engine.storage.set_setting(KEY, json.dumps(rows))


def on_halt(engine: Engine, item: Any, now: float | None = None) -> dict[str, Any] | None:
    """A halt came in: note its stock's price and the day's move into it. Once per halt."""
    now = now or time.time()
    code = item.extra.get("halt_code") or ""
    if not code or not item.tickers or code.startswith("MWC"):
        return None
    sym = item.tickers[0]
    q = engine.price_of(sym, 900, now=now)
    if not q or not q[1] or q[2] is None or abs(q[2]) < 0.5:
        return None  # no price, or no direction to grade it against
    rows = _log(engine)
    if any(r["id"] == item.uid for r in rows):
        return None
    r = {"id": item.uid, "sym": sym, "code": code, "ts": float(item.published or now), "px": q[1],
         "into": round(q[2], 2), "sign": 1 if q[2] > 0 else -1}  # fmt: skip
    rows.append(r)
    _save(engine, rows, now)
    return r


def on_resume(engine: Engine, item: Any, now: float | None = None) -> dict[str, Any] | None:
    """Its resumption came in: note when it trades again (the newest open halt on that stock)."""
    now = now or time.time()
    if not item.tickers or not item.extra.get("resume_at"):
        return None
    rows = _log(engine)
    for r in reversed(rows):
        if r["sym"] == item.tickers[0] and "resume" not in r and now - r["ts"] < 86400:
            r["resume"] = float(item.extra["resume_at"])
            _save(engine, rows, now)
            return r
    return None


def grade(engine: Engine, now: float | None = None) -> int:
    """5 and 30 minutes after the resumption, and at the close, from quotes on the live stamp's clock."""
    from .dossier import session_closes

    now = now or time.time()
    rows = _log(engine)
    made = 0
    for r in rows:
        if "resume" not in r or "close" in r or now - r["ts"] > 3 * 86400:
            continue
        q = engine.price_of(r["sym"], now=now)
        if not q or not q[1] or q[0] < r["resume"]:
            continue
        move = round(r["sign"] * (q[1] / r["px"] - 1) * 100, 2)
        for k, after in STEPS:
            cp = r["resume"] + after
            if k not in r and now >= cp and cp - 60 <= q[0] <= cp + 150:
                r[k] = move
                made += 1
        close_ts = session_closes(r["resume"], 1, engine.calendar.holidays)[0]
        if now >= close_ts and abs(q[0] - close_ts) < 3 * 3600:
            r["close"] = move
            made += 1
    if made:
        _save(engine, rows, now)
    return made


def halt_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"code|up": {"key", "code", "dir", "label", "n", "m5", "m30", "close"}} ("*" for any code)."""
    from .sources.halts import REASONS

    now = now or time.time()
    pools: dict[str, list[dict[str, Any]]] = {}
    for r in _log(engine):
        if now - r["ts"] > days * 86400 or "resume" not in r:
            continue
        d = "up" if r["sign"] > 0 else "down"
        for code in (r["code"], "*"):
            pools.setdefault(f"{code}|{d}", []).append(r)
    out = {}
    for key, rs in pools.items():
        code, d = key.split("|")

        def stats(k: str, rs: list[dict[str, Any]] = rs) -> dict[str, Any] | None:
            xs = [r[k] for r in rs if r.get(k) is not None]
            if len(xs) < MIN_N:
                return None
            return {
                "n": len(xs),
                "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                "median": round(st.median(xs), 2),
            }

        row = {k: stats(k) for k in ("m5", "m30", "close")}
        if not any(row.values()):
            continue
        what = "Halts" if code == "*" else REASONS.get(code, (code, 0))[0]
        out[key] = {
            "key": key,
            "code": code,
            "dir": d,
            "label": f"{what} on the way {d}",
            "n": len(rs),
            **row,
        }
    return out


def halt_for(rates: dict[str, Any], code: str, sign: int) -> dict[str, Any] | None:
    d = "up" if sign > 0 else "down"
    for k in (f"{code}|{d}", f"*|{d}"):
        if k in rates:
            return rates[k]
    return None


def halt_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    parts = [f"{lbl} {round(r[k]['hit'] * 100)}%" for k, lbl in (("m5", "5 minutes after the resumption"),
             ("m30", "30 minutes after"), ("close", "by the close")) if r.get(k)]  # fmt: skip
    n = max(r[k]["n"] for k in ("m5", "m30", "close") if r.get(k))
    return f"{r['label']} kept going: " + ", ".join(parts) + f" ({n} graded)"


def resume_line(engine: Engine, sym: str, now: float | None = None) -> str:
    """For a resumption push: the halt it ends, and how halts like it went after resuming."""
    now = now or time.time()
    rows = [r for r in _log(engine) if r["sym"] == sym and now - r["ts"] < 86400]
    if not rows:
        return ""
    r = max(rows, key=lambda x: x["ts"])  # the halt this resumption ends: its newest
    return halt_line(halt_for(getattr(engine, "halt_rates", None) or {}, r["code"], r["sign"]))


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = sorted(rates.values(), key=lambda r: (r["code"] != "*", -r["n"]))[:14]
    return {"rows": rows, "need": MIN_N}
