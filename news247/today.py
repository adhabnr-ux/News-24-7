"""The playbook today: what the playbook is doing on every live alert, right now, on paper.

Runs 58-63 taught the desk how to trade each kind of news: a way in and a way out, picked together,
checked out of sample. This puts the whole thing on one live board. Every directional alert since
yesterday's close (and older ones the playbook is still holding into the next day or the week) gets
its playbook pair and where it stands, from the facts the desk records live:

- **Waiting:** for the open, for its patient limit, for a second leg, for volume 15 minutes in, or
  for the call to turn.
- **Skipped:** the entry never came (no second leg by the close, volume didn't confirm, the limit
  never filled).
- **In:** entered at its way in's price; marked to the live price.
- **Pass:** no way in and out made money on its kind of news here, so the playbook sits it out.
- **Out:** exited its way out (an hour in, at the close, the next day, five sessions on, or the
  bracket), with the result.

Beside each, the plain way (in at the alert, out at the close) on the same alert, so the board shows
what the playbook is adding today. Paper only: nothing is traded, and the prices are the desk's own
live quotes.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

LOOKBACK = 8 * 86400  # d5 holds started up to a week ago


def _from(sign: int, exit_move: float, entry_move: float) -> float:
    return round(sign * ((1 + exit_move / 100) / (1 + entry_move / 100) - 1) * 100, 2)


def state(a: dict[str, Any], live: float | None, now: float, calendar: Any = None) -> dict[str, Any] | None:
    """Where the playbook stands on one alert: {"in", "out", "status", "why", "entry", "pnl", "plain"}.
    ``live`` is the stock's move now from the alert price (raw %)."""
    from .autopilot import playbook_in, playbook_out
    from .desk import session_edge
    from .dossier import _direction

    e = a.get("edge") or {}
    pin, pout = playbook_in(a), playbook_out(a)
    d = _direction(a)
    cr = e.get("combo_rec") or {}
    if cr.get("pass") and d in ("up", "down"):  # the playbook passes on its kind of news here
        best = ((cr.get("styles") or {}).get(cr.get("best") or "") or {}).get("avg")
        sym = (
            next(iter(e.get("outcome") or {}), None)
            or next(iter(e.get("refs") or {}), None)
            or ((a.get("tickers") or [""])[0])
        )
        return {"in": cr.get("in"), "out": cr.get("out"), "symbol": sym, "sign": 1 if d == "up" else -1, "status": "pass",
                "why": "no way in and out made money on its kind here" + (f" (the best {best:+.1f}%)" if best is not None else "")}  # fmt: skip
    if not pin or not pout or d not in ("up", "down"):
        return None
    sign = 1 if d == "up" else -1
    created = float(a["created"])
    close_ts = session_edge("close", created - 31, calendar)
    open_ts = close_ts - 6.5 * 3600
    o = e.get("outcome") or {}
    row = next(iter(o.values()), {}) if o else {}
    exc = e.get("exc") or {}
    plan = e.get("plan") or {}
    refs = e.get("refs") or {}
    sym = next(iter(o), None) or next(iter(refs), None) or ((a.get("tickers") or [""])[0])
    ref = refs.get(sym) or plan.get("entry")
    out: dict[str, Any] = {"in": pin, "out": pout, "symbol": sym, "sign": sign}
    # ---- the way in
    mv: float | None = None
    if pin == "alert":
        mv = 0.0
    elif pin == "open":
        if row.get("open") is not None:
            mv = row["open"]
        elif created >= open_ts:
            out.update(status="skipped", why="the news came during the session: no open to wait for")
        elif now < open_ts + 900:
            out.update(status="waiting", why="for the open")
        else:
            out.update(status="skipped", why="no print at the open was recorded")
    elif pin == "patient":
        pt = plan.get("patient") or {}
        if pt.get("dip") and exc.get("mae") is not None and exc["mae"] <= -pt["dip"]:
            mv = -sign * pt["dip"]
        elif not pt.get("dip"):
            out.update(status="skipped", why="no patient entry in its plan")
        elif now < close_ts:
            out.update(
                status="waiting",
                why=f"for its limit, {pt['dip']:.1f}% against"
                + (f" (${pt['limit']:,.2f})" if pt.get("limit") else ""),
            )
        else:
            out.update(status="skipped", why="the limit never filled")
    elif pin in ("leg2", "turn"):
        px = (
            (e.get("leg2") or {}).get("px")
            if pin == "leg2"
            else (
                (e.get("call_now") or {}).get("px")
                if (e.get("call_now") or {}).get("verb") == "act"
                else None
            )
        )
        if px and ref:
            mv = (px / ref - 1) * 100
        elif now < close_ts:
            out.update(
                status="waiting", why="for a second leg" if pin == "leg2" else "for the call to turn to act"
            )
        else:
            out.update(
                status="skipped",
                why="no second leg by the close" if pin == "leg2" else "the call never turned",
            )
    elif pin == "vol15":
        r15 = (e.get("rvol") or {}).get("r15")
        if r15 is not None and r15 >= 5 and row.get("m15") is not None:
            mv = row["m15"]
        elif r15 is not None:
            out.update(status="skipped", why=f"volume didn't confirm ({r15:.1f}x normal 15 minutes in)")
        elif now < created + 1800:
            out.update(status="waiting", why="for volume 15 minutes in")
        else:
            out.update(status="skipped", why="no volume read 15 minutes in")
    if mv is None:
        out.setdefault("status", "skipped")
        out.setdefault("why", "")
        return out
    out["entry"] = round(ref * (1 + mv / 100), 2) if ref else None
    # ---- the way out
    exit_move: float | None = None
    if pout == "bracket" and ref and plan.get("stop") and plan.get("target"):
        stop, target = sign * (plan["stop"] / ref - 1) * 100, sign * (plan["target"] / ref - 1) * 100
        if exc.get("mae") is not None and exc["mae"] <= stop:
            out["pnl"], out["status"], out["why"] = round(stop, 2), "out", "at the plan's stop"
        elif exc.get("mfe") is not None and exc["mfe"] >= target:
            out["pnl"], out["status"], out["why"] = round(target, 2), "out", "at the plan's target"
        elif row.get("close") is not None:
            exit_move = row["close"]
    elif (
        pout in ("h1", "close", "d1", "d5", "bracket")
        and row.get("close" if pout == "bracket" else pout) is not None
    ):
        exit_move = row["close" if pout == "bracket" else pout]
    if "pnl" not in out:
        if exit_move is not None:
            out["pnl"], out["status"], out["why"] = _from(sign, exit_move, mv), "out", ""
        elif live is not None:
            out["pnl"], out["status"], out["why"] = _from(sign, live, mv), "in", ""
        else:
            out["status"], out["why"] = "in", "no live price"
    plain = row.get("close") if row.get("close") is not None else live
    if plain is not None:
        out["plain"] = round(sign * plain, 2)
    return out


def board(engine: Engine, now: float | None = None) -> dict[str, Any]:
    """Every live alert's playbook state, newest first, and today's tally against the plain way."""
    from .combos import NAMES
    from .desk import session_edge

    now = now or time.time()
    last_close = session_edge("close", now - 86400 * 4, engine.calendar)
    while True:  # the last regular close before now
        nxt = session_edge("close", last_close, engine.calendar)
        if nxt > now:
            break
        last_close = nxt
    rows = []
    for a in engine.storage.recent_alerts(600, since=now - LOOKBACK):
        if a.get("kind") == "system":
            continue
        e = a.get("edge") or {}
        o = e.get("outcome") or {}
        refs = e.get("refs") or {}
        sym = next(iter(o), None) or next(iter(refs), None) or ((a.get("tickers") or [""])[0])
        ref = refs.get(sym) or (e.get("plan") or {}).get("entry")
        q = engine.price_of(sym, 3600, now=now) if sym else None
        live = (q[1] / ref - 1) * 100 if q and q[1] and ref else None
        s = state(a, live, now, engine.calendar)
        if not s:
            continue
        fresh = float(a["created"]) >= last_close - 8 * 3600  # since yesterday's close (and its after-hours)
        if not fresh and not (s["status"] == "in" and s["out"] in ("d1", "d5")):
            continue  # older alerts only while the playbook still holds them
        if (
            s["status"] == "in"
            and s["out"] in ("d1", "d5")
            and now < session_edge("close", float(a["created"]) - 31, engine.calendar)
        ):
            from .giveback import live as gb_live

            gb = gb_live(engine, a, live)
            if gb:  # tonight's hold, given how much of its run it kept today (giveback.py)
                s["gb"] = gb
        if s["status"] == "in" and s["in"] == "alert" and 1800 <= now - float(a["created"]) <= 3600:
            from .stall import live as stall_live

            sv = stall_live(engine, a, live)
            if sv:  # how its first half hour went, and what such alerts did the rest of the day (stall.py)
                s["stall"] = sv
        if e.get("denied"):  # a later headline denied or walked back the news (denials.py)
            s["denied"] = e["denied"].get("title") or "denied"
        cr = e.get("combo_rec") or {}
        rows.append({**s, "id": a["id"], "title": (a.get("title") or "")[:140], "created": a["created"],
                     "kind": cr.get("label") or (e.get("entry_rec") or {}).get("label") or (e.get("exit_rec") or {}).get("label"),
                     "pair": NAMES.get(f"{s['in']}|{s['out']}", f"{s['in']}, {s['out']}"), "held": cr.get("held")})  # fmt: skip
    rows.sort(key=lambda r: -r["created"])
    done = [r for r in rows if r.get("pnl") is not None]
    both = [r for r in done if r.get("plain") is not None]
    tally = {s: sum(1 for r in rows if r["status"] == s) for s in ("waiting", "in", "out", "skipped", "pass")}
    if done:
        tally["avg"] = round(sum(r["pnl"] for r in done) / len(done), 2)
    if both:
        tally["vs_plain"] = round(sum(r["pnl"] - r["plain"] for r in both) / len(both), 2)
        tally["vs_n"] = len(both)
    return {"rows": rows[:40], "tally": tally}
