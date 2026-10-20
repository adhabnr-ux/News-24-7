"""The playbook in the push, and the playbook's morning in the Brief.

**In the push.** Every alert push already leads with the call and the plan. It now also carries the
playbook's one line, the record's way to trade it or the reason it sits out:

    "🧭 The playbook: on the second leg, out the next day (+1.4% on average, 20)"
    "🚫 The playbook passes: a thin stock: thin names here lost even 5 minutes late (-1.9%, 16)"

**In the Brief.** The 08:15 ET Brief gains the playbook's morning, the day's operating notes in a
few lines:

- the overnight alerts it enters at the open, and the ones it's waiting on;
- what it passes on this morning;
- blind spots that apply today (a risk-off market, a Fed day, today's weekday);
- kinds of news drifting from their promise;
- yesterday's result against the plain way, and the account in dollars.

Paper only; everything here is the playbook's own record, as the boards show it.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")


def playbook_line(edge: dict[str, Any]) -> str:
    """The playbook's one line for an alert's push: its way, or why it passes ("" when it has no say)."""
    from .autopilot import playbook_in, playbook_out, playbook_pass
    from .combos import NAMES

    a = {"edge": edge}
    why = playbook_pass(a)
    if why:
        return f"🚫 The playbook passes: {why}"
    cr = edge.get("combo_rec") or {}
    if not cr.get("best"):
        return ""
    pin, pout = playbook_in(a), playbook_out(a)
    name = NAMES.get(f"{pin}|{pout}", "")
    if not name:
        return ""
    sm = cr.get("smooth") if pout and "+" in pout else None
    rec = sm or (cr.get("styles") or {}).get(cr["best"]) or {}
    tail = (
        f" ({rec['avg']:+.1f}% on average, {rec['n']})" if rec.get("avg") is not None and rec.get("n") else ""
    )
    return f"🧭 The playbook: {name[:1].lower() + name[1:]}{tail}"


def morning(engine: Engine, now: float | None = None) -> dict[str, Any]:
    """The playbook's morning: {"lines": [...], "open": [...], "waiting": [...], "passes", "blind": [...],
    "drifting": [...], "yesterday": {...} | None, "money_line"}, led by the breaker when it's tripped."""
    from .blindspots import slices
    from .desk import session_edge
    from .journal import day
    from .today import board

    now = now or time.time()
    b = board(engine, now)
    fresh = [r for r in b["rows"] if r.get("fresh", True)]
    at_open = sorted({r["symbol"] for r in fresh if r["in"] == "open" and r["status"] == "waiting"})
    waiting = sorted({r["symbol"] for r in fresh if r["in"] != "open" and r["status"] == "waiting"})
    passes = sum(1 for r in fresh if r["status"] == "pass")
    # blind spots that apply to anything today: the market, a macro day, the weekday
    today_like: dict[str, Any] = {"created": now, "edge": {"event_night": False}}
    with contextlib.suppress(Exception):
        rg = engine.regime_label(now)
        if rg:
            today_like["edge"]["regime"] = rg
    with contextlib.suppress(Exception):
        from .macrodays import today as macro_today

        if macro_today(engine, now):
            today_like["edge"]["macro"] = True
    sl = slices(today_like)
    blind = [s["label"] for s in (getattr(engine, "blind_rates", None) or {}).get("spots") or []
             if s["dim"] in ("regime", "macro", "day") and sl.get(s["dim"]) == s["value"]]  # fmt: skip
    drifting = sorted(
        r["label"]
        for r in (getattr(engine, "promise_rates", None) or {}).values()
        if r.get("verdict") == "drifting"
    )
    # yesterday: the board as it stood after yesterday's close
    yesterday = None
    with contextlib.suppress(Exception):
        close = session_edge("close", now - 86400 * 4, engine.calendar)
        while True:
            nxt = session_edge("close", close, engine.calendar)
            if nxt > now:
                break
            close = nxt
        y = day(board(engine, close + 600))
        if y["n"]:
            yesterday = y
    money_line = ""
    with contextlib.suppress(Exception):
        from .pbcurve import curve
        from .pbcurve import money_line as ml

        c = curve(engine, capital=10_000.0, now=now)
        if c.get("money"):
            money_line = ml(c["money"])
    lines = []
    bk = getattr(engine, "breaker_state", None) or {}
    if bk.get("on"):  # the whole playbook stands down (breaker.py): that comes first
        from .breaker import past

        lines.append(
            f"Standing down: its drawdown ({bk['dd']:+.1f}%) is {past(bk)}; it re-arms at {bk['rearm']:+.1f}%"
        )
    if at_open:
        lines.append(
            f"At the open it enters {', '.join(at_open[:5])}"
            + (f" (+{len(at_open) - 5})" if len(at_open) > 5 else "")
        )
    if waiting:
        lines.append(f"Waiting on its way in: {', '.join(waiting[:5])}")
    if passes:
        lines.append(f"Passing on {passes} overnight alert{'s' if passes != 1 else ''}")
    if blind:
        lines.append(
            "Blind spots today: " + "; ".join(x[:1].lower() + x[1:] for x in blind) + " (it sits those out)"
        )
    if drifting:
        lines.append("Drifting from its promise: " + ", ".join(drifting[:4]))
    if yesterday and yesterday.get("avg") is not None:
        y = yesterday
        lines.append(
            f"Yesterday: {y['avg']:+.1f}% a call on {y['took']}"
            + (f", the plain way {y['plain']:+.1f}%" if y.get("plain") is not None else "")
        )
    return {"lines": lines, "open": at_open, "waiting": waiting, "passes": passes, "blind": blind, "drifting": drifting,
            "yesterday": yesterday, "money_line": money_line}  # fmt: skip
