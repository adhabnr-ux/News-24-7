"""The playbook's circuit breaker: it stands down while it's losing worse than its record has ever seen.

A playbook learned from history can stop working: a market changes, a source changes, a kind of
news gets crowded. Every rule so far sits out one slice of alerts (a kind that doesn't pay, a blind
spot, a thin stock); none of them asks whether the playbook *as a whole* has stopped working. The
breaker does, from the playbook's own track record (pbcurve.py: every stamped, graded alert, taken
the playbook's way, one equal paper call each), read in the order its results came in:

- **It trips** when the playbook's running drawdown is deeper than any drawdown in its record before
  this one, and at least 5 points deep, once 40+ alerts are on the record. From then on the
  playbook sits new alerts out: every alert goes out with the reason, the board and the pre-trade
  check show it, and the playbook autopilot opens nothing.
- **It re-arms** when the drawdown recovers to half of what tripped it. The record it reads is the
  playbook's shadow: the alerts it sits out still count, as if it had taken them, so it can see the
  edge come back (and sitting out never makes it look recovered).

Each trip is graded on its own record: the alerts it sat out and what they would have made. A
breaker whose trips sat out winners is costing money, and the record says so. Policy, not a
forecast: the 5-point floor, 40 alerts and the half-way re-arm are fixed rules, stated as such.
Paper only.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 40  # graded alerts on the record before the breaker can trip
FLOOR = 5.0  # it never trips on a drawdown shallower than this (points: one equal paper call per alert)
REARM = 0.5  # it re-arms when the drawdown recovers to this share of the level that tripped it
KEY = "breaker_pushed"


def _known_at(a: dict[str, Any], calendar: Any = None) -> float:
    """When the playbook's result on this alert was known: its last way out."""
    from .autopilot import exit_time, playbook_out

    out = playbook_out(a) or "close"
    created = float(a["created"])
    return max(exit_time("close" if leg == "bracket" else leg, created, calendar) for leg in out.split("+"))


def rows(engine: Engine, days: float = 365, now: float | None = None) -> list[dict[str, Any]]:
    """Every stamped, graded alert: {"id", "created", "known", "pb"}, the playbook's result as if the
    breaker had never sat it out (its shadow record)."""
    from .pbcurve import taken

    now = now or time.time()
    out = []
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        got = taken(a, strip=("breaker",))
        if got is None:
            continue
        shadow = {**a, "edge": {k: v for k, v in (a.get("edge") or {}).items() if k != "breaker"}}
        known = _known_at(shadow, engine.calendar)
        if known > now:
            continue  # its way out hasn't come yet
        out.append({"id": a["id"], "created": float(a["created"]), "known": known, "pb": got[0]})
    return out


def replay(rs: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk the record in the order its results were known, tripping and re-arming by the rules, and
    note which alerts went out while it was tripped (each is graded on what it would have made)."""
    known = sorted(rs, key=lambda r: r["known"])
    alerts = sorted(rs, key=lambda r: r["created"])
    s: dict[str, Any] = {"n": 0, "cum": 0.0, "peak": 0.0, "worst": 0.0, "prior": 0.0, "on": None, "trips": []}

    def advance(r: dict[str, Any]) -> None:
        s["n"] += 1
        s["cum"] += r["pb"]
        if s["cum"] > s["peak"]:
            s["peak"] = s["cum"]
            s["prior"] = s["worst"]  # a new high: the drawdown before it joins the record
        dd = s["cum"] - s["peak"]
        s["worst"] = min(s["worst"], dd)
        on = s["on"]
        if on is None:
            limit = min(s["prior"], -FLOOR)
            if s["n"] >= MIN_N and dd < limit:
                s["on"] = {"start": r["known"], "dd": round(dd, 2), "prior": round(s["prior"], 2), "limit": round(limit, 2),
                           "rearm": round(limit * REARM, 2), "low": round(dd, 2), "sat": []}  # fmt: skip
        else:
            on["low"] = round(min(on["low"], dd), 2)
            if dd >= on["rearm"]:
                on["end"] = r["known"]
                s["trips"].append(on)
                s["on"] = None

    j = 0
    for a in alerts:
        while j < len(known) and known[j]["known"] <= a["created"]:
            advance(known[j])
            j += 1
        if s["on"] is not None:
            s["on"]["sat"].append(a)
    while j < len(known):
        advance(known[j])
        j += 1
    return s


def _trip(t: dict[str, Any]) -> dict[str, Any]:
    made = round(sum(a["pb"] for a in t["sat"]), 2)
    return {"start": t["start"], "end": t.get("end"), "dd": t["dd"], "prior": t["prior"], "limit": t["limit"],
            "low": t["low"], "rearm": t["rearm"], "n": len(t["sat"]), "made": made}  # fmt: skip


def past(st: dict[str, Any]) -> str:
    """What the drawdown went past: the record's worst before it, or the floor when that was shallower."""
    if st["prior"] < -FLOOR:
        return f"deeper than any in its record ({st['prior']:+.1f}%)"
    return f"past {-FLOOR:+.0f}% with none that deep in its record"


def why(st: dict[str, Any]) -> str:
    """The pass reason while it's tripped."""
    return f"it's standing down: its drawdown ({st['dd']:+.1f}%) is {past(st)}; it re-arms at {st['rearm']:+.1f}%"


def state(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"n", "on", "dd", "prior", "limit", "rearm", "since", "trips": [...], "sat", "made", "line", "why"}."""
    now = now or time.time()
    s = replay(rows(engine, days, now))
    trips = [_trip(t) for t in s["trips"]]
    cur = _trip(s["on"]) if s["on"] else None
    dd = round(s["cum"] - s["peak"], 2)
    out: dict[str, Any] = {"n": s["n"], "need": MIN_N, "floor": FLOOR, "on": cur is not None, "dd": dd,
                           "prior": round(s["prior"], 2), "limit": round(min(s["prior"], -FLOOR), 2),
                           "trips": (trips + ([cur] if cur else []))[-10:]}  # fmt: skip
    graded = trips + ([cur] if cur else [])
    out["sat"] = sum(t["n"] for t in graded)
    out["made"] = round(sum(t["made"] for t in graded), 2)
    if cur:
        out.update(
            {"since": cur["start"], "rearm": cur["rearm"], "prior": cur["prior"], "limit": cur["limit"]}
        )
        out["why"] = why(out)
    out["line"] = line(out)
    return out


def line(st: dict[str, Any]) -> str:
    if st["n"] < MIN_N:
        return f"The breaker arms once {MIN_N} alerts are on the playbook's record ({st['n']} so far)."
    if st["on"]:
        head = (
            f"⛔ The playbook is standing down: its drawdown ({st['dd']:+.1f}%) is {past(st)}. It sits new alerts "
            f"out until it's back to {st['rearm']:+.1f}% on its shadow record (the alerts it sits out still count, "
            "as if taken)."
        )
    else:
        head = f"The breaker is armed: it trips below {st['limit']:+.1f}% (the playbook's drawdown now {st['dd']:+.1f}%)."
    if st["sat"]:
        times = len(st["trips"])
        verdict = (
            "it saved that" if st["made"] < 0 else "sitting out cost that" if st["made"] > 0 else "a wash"
        )
        head += (
            f" It has tripped {times} time{'s' if times != 1 else ''}; the {st['sat']} alert{'s' if st['sat'] != 1 else ''} "
            f"it sat out would have made {st['made']:+.1f}% ({verdict})."
        )
    return head


def stamp(engine: Engine) -> dict[str, Any] | None:
    """The breaker's stamp for a new alert while it's tripped (autopilot.playbook_pass reads it)."""
    st = getattr(engine, "breaker_state", None) or {}
    if not st.get("on"):
        return None
    return {"why": st["why"], "since": st["since"], "dd": st["dd"]}


async def send(engine: Engine, now: float | None = None) -> int:
    """One push when the breaker trips and one when it re-arms, to every phone with the playbook's
    moves switched on (Settings → The playbook's moves)."""
    st = getattr(engine, "breaker_state", None) or {}
    if "on" not in st or st.get("n", 0) < MIN_N or engine.webpush is None or engine.dispatcher.paused:
        return 0
    now = now or time.time()
    cur = "on" if st["on"] else "off"
    was = engine.storage.get_setting(KEY)
    if was == cur:
        return 0
    engine.storage.set_setting(KEY, cur)
    if was is None and cur == "off":
        return 0  # the first reading, armed: nothing happened
    if cur == "on":
        title = f"⛔ The playbook stands down ({st['dd']:+.1f}%)"
        body = (
            f"Its drawdown is {past(st)}. It sits new alerts out (the autopilot too) until it's back to "
            f"{st['rearm']:+.1f}%. Paper only."
        )
    else:
        last = (st.get("trips") or [{}])[-1]
        title = "✅ The playbook is back"
        body = f"Its drawdown recovered to {st['dd']:+.1f}%; it takes new alerts again." + (
            f" The {last['n']} it sat out would have made {last['made']:+.1f}%." if last.get("n") else ""
        )
    payload = {"id": f"bk-{cur}-{int(now)}", "title": title, "body": body, "severity": "MEDIUM", "kind": "system",
               "tickers": [], "tag": "breaker", "ts": now, "open": "/app/#a=today"}  # fmt: skip
    sent = 0
    for device in {s.device for s in engine.webpush.subs.values() if s.device}:  # type: ignore[attr-defined]
        prof = engine.desk.profiles.get(device) or {}
        if not (prof.get("prefs") or {}).get("pb_push", True):
            continue
        if await engine._to_device(device, dict(payload), urgency="normal"):
            sent += 1
    return sent
