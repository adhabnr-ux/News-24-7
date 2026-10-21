"""Early exits on the record: the playbook's exit rules stamped on every alert they cut, and graded.

Three rules cut a playbook call before its pair's own way out:

- **the stall rule:** a call entered at the alert that's still stalled 30 minutes in, on a kind whose
  stalled alerts lost the rest of the day (stall.py);
- **the give-back exit:** a hold into tomorrow whose day gave back the kind of run that lost
  overnight here, out at the close (giveback.py);
- **the event exit:** no overnight hold into the stock's own earnings or binary event until the
  record of such nights says it pays (eventrisk.py).

Until now they only acted on the autopilots' calls, and the playbook's track record ignored them:
it counted every alert held to its pair's exit. Now each rule is checked at its moment on **every**
stamped alert the playbook would hold, from what was known then, and stamped on the alert where it
cuts, with the price at that moment. The track record takes the cut, as the playbook would have.

Each rule is graded on the alerts it cut: what the cut made against holding to the pair's own exit
(from the alert's graded record), with the older and the newer half apart. A rule whose cuts did
worse than holding in both halves, on 20+, **costs**: it's muted. New alerts still get its stamp,
marked muted, and the playbook holds them. A muted rule goes on being graded and comes back once
its cuts stop costing. History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 20
RULES = {
    "stall": "The stall rule (a time stop 30 minutes in)",
    "giveback": "Out at the close: the day gave back its run",
    "event": "Out before the stock's own event",
}


def _ref_move(engine: Engine, a: dict[str, Any], now: float, max_age: float) -> float | None:
    sym = next(iter((a.get("edge") or {}).get("refs") or {}), None)
    ref = ((a.get("edge") or {}).get("refs") or {}).get(sym) if sym else None
    q = engine.price_of(sym, max_age, now=now) if sym else None
    if not ref or not q or not q[1]:
        return None
    return (q[1] / float(ref) - 1) * 100


def _stamp(engine: Engine, a: dict[str, Any], rule: str, move: float, line: str, now: float) -> None:
    muted = rule in ((getattr(engine, "early_rates", None) or {}).get("muted") or [])
    engine.storage.update_alert_edge(a["id"], {"early": {"rule": rule, "at": now, "move": round(move, 2), "line": line,
                                                         "muted": muted}})  # fmt: skip


def stamp_stalls(engine: Engine, now: float | None = None) -> int:
    """Alerts 30-40 minutes old the playbook entered at the alert: stamp the ones the stall rule cuts."""
    from .autopilot import playbook_in
    from .stall import live

    now = now or time.time()
    n = 0
    for a in engine.storage.alerts_between(now - 2400, now - 1800, 400):
        e = a.get("edge") or {}
        if "early" in e or not (e.get("combo_rec") or {}).get("best") or playbook_in(a) != "alert":
            continue
        move = _ref_move(engine, a, now, 300)
        v = live(engine, a, move)
        if v and v.get("exit") and move is not None:
            _stamp(engine, a, "stall", move, v["line"], now)
            n += 1
    return n


def stamp_closes(engine: Engine, now: float | None = None) -> int:
    """10 minutes before the close: today's alerts the playbook would hold overnight, cut by the event
    exit (its stock's own event tonight) or the give-back exit."""
    from .autopilot import playbook_in, playbook_out
    from .desk import session_edge
    from .eventrisk import verdict as event_verdict
    from .giveback import live

    now = now or time.time()
    close_ts = session_edge("close", now - 31, engine.calendar)
    if not 0 < close_ts - now <= 600:
        return 0
    n = 0
    for a in engine.storage.alerts_between(close_ts - 86400, now, 800):
        e = a.get("edge") or {}
        if "early" in e or not (e.get("combo_rec") or {}).get("best") or not playbook_in(a):
            continue
        if (playbook_out(a) or "").split("+")[-1] not in ("d1", "d5"):
            continue
        if session_edge("close", float(a["created"]) - 31, engine.calendar) != close_ts:
            continue  # only on the alert's own session: the hold is decided there
        sym = next(iter(e.get("refs") or {}), None)
        move = _ref_move(engine, a, now, 600)
        if not sym or move is None:
            continue
        cands = []
        v = event_verdict(engine, a, sym)
        if v and v.get("exit"):
            cands.append(("event", v))
        g = live(engine, a, move)
        if g and g.get("exit"):
            cands.append(("giveback", g))
        if not cands:
            continue
        muted = (getattr(engine, "early_rates", None) or {}).get("muted") or []
        rule, v = next(
            (c for c in cands if c[0] not in muted), cands[0]
        )  # a muted rule only when it's all there is
        _stamp(engine, a, rule, move, v["line"], now)
        n += 1
    return n


def cut(a: dict[str, Any], calendar: Any = None) -> float | None:
    """What the playbook's pair made on this graded alert with its early exit taken (legs due before
    the cut keep their own exits), its way, %; None when there's no cut to read."""
    from .autopilot import exit_time, playbook_in, playbook_out
    from .entries import _from, entry_moves

    e = a.get("edge") or {}
    early = e.get("early")
    got = entry_moves(a)
    if not early or not got:
        return None
    sign, row, moves = got
    pin, pout = playbook_in(a), playbook_out(a)
    if not pin or not pout or pin not in moves:
        return None
    created = float(a["created"])
    legs = []
    for leg in pout.split("+"):
        if leg != "bracket" and exit_time(leg, created, calendar) <= early["at"] and row.get(leg) is not None:
            legs.append(_from(sign, row[leg], moves[pin]))  # this half was out before the cut
        else:
            legs.append(_from(sign, early["move"], moves[pin]))
    return round(sum(legs) / len(legs), 2)


def early_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{rule: {"rule", "label", "n", "cut", "hold", "added", "older", "newer", "verdict"}}, "muted": [...]."""
    from .pbcurve import taken

    now = now or time.time()
    pools: dict[str, list[tuple[float, float, float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        early = (a.get("edge") or {}).get("early")
        if not early:
            continue
        held = taken({**a, "edge": {k: v for k, v in a["edge"].items() if k != "early"}})
        c = cut(a, engine.calendar)
        if held is None or c is None or held[0] == 0.0 and c == 0.0:
            continue
        pools.setdefault(early["rule"], []).append((float(a["created"]), c, held[0]))
    out: dict[str, Any] = {}
    for rule, rows in pools.items():
        rows.sort()
        added = [c - h for _, c, h in rows]
        mid = len(added) // 2
        older = round(sum(added[:mid]) / mid, 2) if mid else None
        newer = round(sum(added[mid:]) / (len(added) - mid), 2)
        avg = round(sum(added) / len(added), 2)
        if len(added) >= MIN_N and older is not None and older < 0 and newer < 0:
            verdict = "costs"
        elif avg > 0:
            verdict = "earns"
        else:
            verdict = "unclear"
        out[rule] = {"rule": rule, "label": RULES.get(rule, rule), "n": len(rows),
                     "cut": round(sum(c for _, c, _ in rows) / len(rows), 2), "hold": round(sum(h for _, _, h in rows) / len(rows), 2),
                     "added": avg, "older": older, "newer": newer, "verdict": verdict}  # fmt: skip
    out["muted"] = sorted(r for r, v in out.items() if v["verdict"] == "costs")
    return out


def line(r: dict[str, Any]) -> str:
    head = (
        f"{r['label']}: on the {r['n']} alerts it cut, the cut made {r['cut']:+.2f}% on average against "
        f"{r['hold']:+.2f}% holding to the pair's own exit"
    )
    if r["verdict"] == "earns":
        return head + f": it adds {r['added']:+.2f}% a cut."
    if r["verdict"] == "costs":
        return (
            head + f", worse in both halves ({r['older']:+.2f}%, then {r['newer']:+.2f}% a cut): it's muted."
        )
    if r["n"] < MIN_N:
        return head + f": too few to judge ({MIN_N} needed)."
    return head + ": not a cost in both halves, so it stays."


def board(rates: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**rates[k], "line": line(rates[k])} for k in RULES if k in rates]
