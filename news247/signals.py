"""Which signals work here: every live signal's forward record in one ranked table, with its switch.

The desk now pushes a dozen kinds of signal beyond the alerts themselves:
- the wait is over;
- the mainstream caught up;
- a second leg;
- volume 15 minutes in;
- the call turning;
- a run-up into an event;
- a sympathy flag;
- and more.
Each is graded in its own corner. This reads them all for one phone and ranks them by how their
signals did, each with its numbers and the push setting that controls it:
- **Strong:** 60%+ went their way with a positive median, 8+ graded.
- **Weak:** 45% or less, or a negative median.
- **Too few:** under 8 graded.

The advice offers to switch off a weak signal this phone still gets, and back on a strong one it
switched off. History, not a forecast: a signal's record is what it did here, so far.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8


def _row(
    key: str, label: str, pref: str | None, rec: dict[str, Any] | None, what: str, hit_key: str = "hit"
) -> dict[str, Any]:
    n = (rec or {}).get("n", 0)
    hit = (rec or {}).get(hit_key)
    med = (rec or {}).get("median")
    if not rec or n < MIN_N or hit is None:
        verdict = "thin"
    elif hit >= 0.6 and (med is None or med > 0):
        verdict = "strong"
    elif hit <= 0.45 or (med is not None and med < 0):
        verdict = "weak"
    else:
        verdict = "fair"
    return {
        "key": key,
        "label": label,
        "pref": pref,
        "n": n,
        "hit": hit,
        "median": med,
        "what": what,
        "verdict": verdict,
    }


def scoreboard(engine: Engine, device: str = "") -> dict[str, Any]:
    """Every live signal with a record, strongest first, and whether this phone gets its pushes."""
    from .dossier import sympathy_record

    e = engine
    turns = (getattr(e, "turn_rates", None) or {}).get("act")
    conf = [
        r for k, r in (getattr(e, "rvol_rates", None) or {}).items() if k in ("r15:heavy|*", "r15:frenzy|*")
    ]
    vol = max(conf, key=lambda r: r["n"]) if conf else None
    cu = (getattr(e, "catchup_rates", None) or {}).get("*|*")
    ru = (getattr(e, "runup_rates", None) or {}).get("*|*")
    w_open = (getattr(e, "wait_rates", None) or {}).get("open")
    w_pull = (getattr(e, "wait_rates", None) or {}).get("pullback")
    sym = sympathy_record(e).get("close")
    rows = [
        _row(
            "leg2",
            "Second legs",
            "leg2",
            (getattr(e, "leg_rates", None) or {}).get("*"),
            "from the breakout to the close",
        ),
        _row("turn", "The call turning to act", "callnow", turns, "from the turn to the close"),
        _row("volconf", "Confirmed by volume", "volconf", vol, "heavy volume 15 minutes in, by the close"),
        _row(
            "catchup",
            "After the mainstream catches up",
            "catchup",
            {**cu, **cu["after"]} if cu else None,
            "went further by the close",
        ),
        _row(
            "runup",
            "Run-ups into events",
            "runup",
            {**ru, "hit": ru["run"]["up"], "median": ru["run"]["median"]} if ru else None,
            "rose in the ten sessions before",
        ),  # fmt: skip
        _row(
            "wait_open",
            "Waiting for the open",
            "waits",
            {**w_open, "hit": w_open.get("paid"), "median": w_open.get("gain")} if w_open else None,
            "beat acting at the alert",
        ),  # fmt: skip
        _row(
            "wait_pull",
            "Waiting for the pullback",
            "waits",
            {**w_pull, "hit": w_pull.get("paid"), "median": w_pull.get("gain")} if w_pull else None,
            "beat acting at the alert",
        ),  # fmt: skip
        _row("sympathy", "Sympathy flags", "sympathy", sym, "the peer caught up by the close"),
    ]
    prefs = ((e.desk.profiles.get(device) or {}).get("prefs") or {}) if device else {}
    for r in rows:
        r["on"] = prefs.get(r["pref"], True) if r["pref"] else None
    order = {"strong": 0, "fair": 1, "weak": 2, "thin": 3}
    rows.sort(key=lambda r: (order[r["verdict"]], -(r["hit"] or 0)))
    return {"rows": rows, "need": MIN_N}


def advice_items(engine: Engine, device: str) -> list[dict[str, Any]]:
    """Switch off a weak signal this phone gets; switch back on a strong one it turned off."""
    out = []
    for r in scoreboard(engine, device)["rows"]:
        if not r["pref"] or r["on"] is None:
            continue
        rec = f"{round(r['hit'] * 100)}% {r['what']} ({r['n']} graded)" if r["hit"] is not None else ""
        if r["verdict"] == "weak" and r["on"]:
            out.append({"key": f"sig-off:{r['pref']}", "title": f"Turn off {r['label'].lower()} pushes", "why": f"They're weak here: {rec}.",
                        "weight": r["n"] * 0.3, "action": "prefs", "body": {r["pref"]: False}, "label": "Turn off"})  # fmt: skip
        elif r["verdict"] == "strong" and not r["on"]:
            out.append({"key": f"sig-on:{r['pref']}", "title": f"Turn {r['label'].lower()} pushes back on", "why": f"They're strong here: {rec}.",
                        "weight": r["n"] * 0.3, "action": "prefs", "body": {r["pref"]: True}, "label": "Turn on"})  # fmt: skip
    return out
