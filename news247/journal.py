"""The playbook's day, written up: one paragraph on what it did with today's news, and how it did.

The playbook makes many small decisions in a day: it takes some alerts, waits on others, passes on
kinds of news that don't pay here, cuts a stalled call, gets out at the close instead of holding
overnight, drops a call whose news was denied. Each is on the live board, and none of them adds up
to a picture there. After the close the desk writes the day up in a few sentences:

    "The playbook's day: 14 directional alerts. It took 6 (3 at the alert, 2 on second legs, 1 at
    its limit), passed on 4 (kinds that don't pay here) and 4 entries never came. Cut early: 1 by
    the time stop, 1 at the close instead of overnight, 1 denied. On paper it made +0.8% a call
    against +0.3% the plain way on the same alerts (6). Best ACME +3.1%, worst BETA -1.2%."

It tops the playbook today board, and a push carries it at 16:10 ET on market days (Settings →
*The playbook's day*). Paper only; the numbers are the board's own.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
IN_WORDS = {"alert": "at the alert", "open": "at the open", "patient": "at its limit", "leg2": "on second legs",
            "vol15": "on volume", "turn": "when the call turned"}  # fmt: skip


def day(board: dict[str, Any]) -> dict[str, Any]:
    """The day's tally from the playbook today board (today's session's alerts only)."""
    rows = [r for r in board.get("rows") or [] if r.get("fresh", True)]
    took = [r for r in rows if r["status"] in ("in", "out")]
    ways: dict[str, int] = {}
    for r in took:
        ways[r["in"]] = ways.get(r["in"], 0) + 1
    done = [r for r in took if r.get("pnl") is not None]
    both = [r for r in done if r.get("plain") is not None]
    out: dict[str, Any] = {
        "n": len(rows), "took": len(took), "ways": ways,
        "passed": sum(1 for r in rows if r["status"] == "pass"),
        "never": sum(1 for r in rows if r["status"] == "skipped"),
        "waiting": sum(1 for r in rows if r["status"] == "waiting"),
        "stalled": sum(1 for r in rows if (r.get("stall") or {}).get("exit")),
        "gave_back": sum(1 for r in rows if (r.get("gb") or {}).get("exit")),
        "denied": sum(1 for r in rows if r.get("denied")),
    }  # fmt: skip
    if done:
        out["avg"] = round(sum(r["pnl"] for r in done) / len(done), 2)
    if len(done) >= 2:  # a best and a worst need two
        best = max(done, key=lambda r: r["pnl"])
        worst = min(done, key=lambda r: r["pnl"])
        out["best"] = {"symbol": best["symbol"], "pnl": best["pnl"]}
        if worst is not best:
            out["worst"] = {"symbol": worst["symbol"], "pnl": worst["pnl"]}
    if both:
        out["plain"] = round(sum(r["plain"] for r in both) / len(both), 2)
        out["plain_n"] = len(both)
        out["edge"] = round(out["avg"] - out["plain"], 2) if "avg" in out else None
    out["text"] = story(out)
    return out


def story(d: dict[str, Any]) -> str:
    if not d["n"]:
        return ""
    s = f"The playbook's day: {d['n']} directional alert{'s' if d['n'] != 1 else ''}."
    if d["took"]:
        ways = ", ".join(
            f"{n} {IN_WORDS.get(w, w)}" for w, n in sorted(d["ways"].items(), key=lambda kv: -kv[1])
        )
        s += f" It took {d['took']} ({ways})"
    else:
        s += " It took none"
    rest = []
    if d["passed"]:
        rest.append(f"passed on {d['passed']} (kinds that don't pay here)")
    if d["never"]:
        rest.append(f"{d['never']} entr{'ies' if d['never'] != 1 else 'y'} never came")
    if d["waiting"]:
        rest.append(f"{d['waiting']} still waiting")
    s += (", " + ", ".join(rest[:-1]) + (" and " if len(rest) > 1 else "") + rest[-1] if rest else "") + "."
    cuts = []
    if d["stalled"]:
        cuts.append(f"{d['stalled']} by the time stop")
    if d["gave_back"]:
        cuts.append(f"{d['gave_back']} at the close instead of overnight")
    if d["denied"]:
        cuts.append(f"{d['denied']} denied")
    if cuts:
        s += " Cut early: " + ", ".join(cuts) + "."
    if d.get("avg") is not None:
        s += f" On paper it made {d['avg']:+.1f}% a call"
        if d.get("plain") is not None:
            s += f" against {d['plain']:+.1f}% the plain way on the same alerts ({d['plain_n']})"
        s += "."
    if d.get("best"):
        s += f" Best {d['best']['symbol']} {d['best']['pnl']:+.1f}%" + (
            f", worst {d['worst']['symbol']} {d['worst']['pnl']:+.1f}%." if d.get("worst") else "."
        )
    return s


async def send_day(engine: Engine, now: float | None = None) -> int:
    """16:10-16:40 ET on a market day: the playbook's day, once, to every phone that keeps it on."""
    from .today import board

    now = now or time.time()
    dt = datetime.fromtimestamp(now, ET)
    holidays = getattr(engine.calendar, "holidays", set())
    if (
        dt.weekday() >= 5
        or dt.date().isoformat() in holidays
        or not ((16, 10) <= (dt.hour, dt.minute) < (16, 40))
    ):
        return 0
    if engine.webpush is None or not engine._once(f"pb_day:{dt.date().isoformat()}"):
        return 0
    d = day(board(engine, now))
    if not d["n"]:
        return 0
    title = "📓 The playbook's day" + (f": {d['avg']:+.1f}% a call" if d.get("avg") is not None else "")
    payload = {"id": f"pbd-{dt.date().isoformat()}", "title": title, "body": d["text"][:600], "severity": "MEDIUM",
               "kind": "system", "tickers": [], "tag": "pbd", "ts": now, "open": "/app/#a=today"}  # fmt: skip
    sent = 0
    for dev in {s.device for s in engine.webpush.subs.values() if s.device}:  # type: ignore[attr-defined]
        if not ((engine.desk.profiles.get(dev) or {}).get("prefs") or {}).get("pb_day", True):
            continue
        if await engine._to_device(dev, dict(payload), urgency="normal"):
            sent += 1
    return sent
