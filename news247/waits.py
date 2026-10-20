"""The wait is over: when the call says wait, say when the wait ends, and grade whether it paid.

The call (thecall.py) says "wait" two ways:
- **for the open:** news from outside the session whose kind kept going after the bell 45% of the
  time or less.
- **for a pullback:** a stock whose first-hour moves usually fade.
Nothing came back to say the wait was over. Now it does:

- **At the open** (9:35-10:00 ET, once the first print is on the alert), each phone that got a wait-for-the-open alert, or watches, holds
  or has a call on its stock, gets one push. It gives:
  - where the stock opened against the alert price;
  - where it is now;
  - how news like it went on from a gap like that (into the open's record, by kind and gap size);
  - a read.
- **At the patient entry** (the winners' median dip, from the plan stamped on the alert), the same
  phones get one push when the price comes back to it during the alert's session. It gives the
  level and, from heat, what the odds read once filled there (adverse selection, in one number).

Every wait is graded against acting at the alert price, by the close:
- waiting for the open means entering at the first print;
- waiting for a pullback means a limit at the patient entry, filled when the alert's worst dip
  reached it, and missed when it didn't (a miss earns nothing, against the alert's move).
Per kind of wait, the record says how often waiting beat acting and what it added on average. If
waiting doesn't pay, the call's wait rule is wrong and it shows. Paper only; history, not a forecast.
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
MIN_N = 8


# --------------------------------------------------------------------------- the record


def graded(a: dict[str, Any]) -> dict[str, Any] | None:
    """One wait, graded by the close: {"for", "filled", "act", "wait", "gain"}, every number its way, in %."""
    e = a.get("edge") or {}
    call = e.get("call") or {}
    if call.get("verb") != "wait" or call.get("wait_for") not in ("open", "pullback"):
        return None
    row = next(iter((e.get("outcome") or {}).values()), None) or {}
    sign, close = row.get("sign"), row.get("close")
    if not sign or close is None:
        return None
    act = round(sign * close, 2)
    if call["wait_for"] == "open":
        if row.get("open") is None:
            return None
        w = round(sign * ((1 + close / 100) / (1 + row["open"] / 100) - 1) * 100, 2)
        return {"for": "open", "filled": True, "act": act, "wait": w, "gain": round(w - act, 2)}
    pt = (e.get("plan") or {}).get("patient") or {}
    mae = (e.get("exc") or {}).get("mae")
    if not pt.get("dip") or mae is None:
        return None
    if mae > -pt["dip"]:  # never came back to the limit: the wait missed the move
        return {"for": "pullback", "filled": False, "act": act, "wait": 0.0, "gain": round(-act, 2)}
    w = round(sign * ((1 + close / 100) / (1 - sign * pt["dip"] / 100) - 1) * 100, 2)
    return {"for": "pullback", "filled": True, "act": act, "wait": w, "gain": round(w - act, 2)}


def wait_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"open"|"pullback": {"n", "filled", "paid", "gain", "gain_median", "wait_hit", "act_hit",
    "missed", "missed_act", "verdict"}}: waiting against acting at the alert price, by the close."""
    now = now or time.time()
    pools: dict[str, list[dict[str, Any]]] = {"open": [], "pullback": []}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        g = graded(a)
        if g:
            pools[g["for"]].append(g)
    out = {}
    for k, xs in pools.items():
        if not xs:
            continue
        filled = [x for x in xs if x["filled"]]
        missed = [x for x in xs if not x["filled"]]
        r: dict[str, Any] = {"for": k, "n": len(xs), "filled": len(filled),
                             "paid": round(sum(x["gain"] > 0 for x in xs) / len(xs), 2),
                             "gain": round(sum(x["gain"] for x in xs) / len(xs), 2),
                             "gain_median": round(st.median(x["gain"] for x in xs), 2),
                             "act_hit": round(sum(x["act"] > 0 for x in xs) / len(xs), 2),
                             "wait_hit": round(sum(x["wait"] > 0 for x in filled) / len(filled), 2) if filled else None,
                             "missed": len(missed),
                             "missed_act": round(st.median(x["act"] for x in missed), 2) if missed else None}  # fmt: skip
        r["verdict"] = (
            "thin" if len(xs) < MIN_N
            else "paid" if r["gain"] > 0 and r["paid"] >= 0.5
            else "cost" if r["gain"] < 0
            else "wash"
        )  # fmt: skip
        out[k] = r
    return out


WHAT = {"open": "Waiting for the open", "pullback": "Waiting for the pullback"}


def wait_line(r: dict[str, Any] | None) -> str:
    """One line for a push: Waiting for the open beat acting at the alert 64% of the time here (+0.9% on average, 25)."""
    if not r or r["n"] < MIN_N:
        return ""
    base = f"{WHAT[r['for']]} beat acting at the alert {round(r['paid'] * 100)}% of the time here ({r['gain']:+.1f}% on average, {r['n']})"
    if r["for"] == "pullback" and r["missed"]:
        base += f"; {r['missed']} never came back to the level"
    return base + "."


# --------------------------------------------------------------------------- the pushes


def _devices(engine: Engine, a: dict[str, Any], sym: str, pref: str = "waits") -> set[str]:
    """Phones the alert reached, and phones that watch, hold or have a call on its stock."""
    wp = engine.webpush
    got = {d for d, q in (getattr(wp, "log", None) or {}).items()
           if any(r.get("id") == str(a["id"])[:60] and r.get("status") == "sent" for r in q)}  # fmt: skip
    got |= set(engine.desk.watchers([sym]))
    got |= {
        d for d, p in engine.desk.profiles.items() if any(t["symbol"] == sym for t in p.get("tracked") or [])
    }
    return {d for d in got if ((engine.desk.profiles.get(d) or {}).get("prefs") or {}).get(pref, True)}


def _session(now: float, holidays: set[str]) -> tuple[float, float] | None:
    dt = datetime.fromtimestamp(now, ET)
    if dt.weekday() >= 5 or dt.date().isoformat() in holidays:
        return None
    op = dt.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
    return op, op + 6.5 * 3600


def prev_close(op: float, holidays: set[str]) -> float:
    """The last session's close before this open, past weekends and market holidays."""
    from datetime import timedelta

    d = datetime.fromtimestamp(op, ET)
    while True:
        d -= timedelta(days=1)
        if d.weekday() < 5 and d.date().isoformat() not in holidays:
            return d.replace(hour=16, minute=0, second=0, microsecond=0).timestamp()


def read_open(r: dict[str, Any] | None) -> str:
    """The read from into the open's record for this gap: act, still wait, or either."""
    if not r:
        return ""
    a = r["after"]
    if a["n"] >= MIN_N and a["hit"] >= 0.55 and a["median"] > 0:
        return "the record from here favors acting"
    if a["n"] >= MIN_N and (a["hit"] <= 0.45 or a["median"] < 0):
        return "the record from here still says no rush"
    return "the record from here is mixed"


def open_due(engine: Engine, now: float | None = None) -> list[dict[str, Any]]:
    """Wait-for-the-open alerts from before today's bell, each with where it opened and stands now."""
    from .catalysts import classify
    from .dossier import _direction
    from .opening import open_for

    now = now or time.time()
    holidays = getattr(engine.calendar, "holidays", set())
    s = _session(now, holidays)
    if not s or not (s[0] + 300 <= now < s[0] + 1800):
        return []
    op = s[0]
    rows = []
    for a in engine.storage.alerts_between(prev_close(op, holidays), op, 2000):
        call = (a.get("edge") or {}).get("call") or {}
        if (
            call.get("verb") != "wait"
            or call.get("wait_for") != "open"
            or not a.get("tickers")
            or not _direction(a)
        ):
            continue
        e = a.get("edge") or {}
        sym = a["tickers"][0]
        ref = (e.get("refs") or {}).get(sym)
        q = engine.price_of(sym, 600, now=now)
        if not ref or not q or not q[1] or q[0] < op:
            continue
        sign = 1 if _direction(a) == "up" else -1
        row = (e.get("outcome") or {}).get(sym) or {}
        gap = sign * row["open"] if row.get("open") is not None else None
        its = round(sign * (q[1] / ref - 1) * 100, 2)
        c = e.get("catalyst") or classify(a, engine.universe)
        rec = open_for(
            getattr(engine, "open_rates", None) or {},
            c["key"] if c else None,
            gap if gap is not None else its,
        )
        rows.append({"alert": a, "symbol": sym, "sign": sign, "ref": ref, "px": q[1], "gap": gap, "its": its,
                     "record": rec, "read": read_open(rec)})  # fmt: skip
    return rows


def pullback_due(engine: Engine, now: float | None = None) -> list[dict[str, Any]]:
    """Wait-for-a-pullback alerts from today's session whose price came back to the patient entry."""
    from .dossier import _direction

    now = now or time.time()
    holidays = getattr(engine.calendar, "holidays", set())
    s = _session(now, holidays)
    if not s or not (s[0] <= now < s[1]):
        return []
    rows = []
    for a in engine.storage.alerts_between(prev_close(s[0], holidays), now, 2000):  # this session's alerts
        e = a.get("edge") or {}
        call = e.get("call") or {}
        pt = (e.get("plan") or {}).get("patient") or {}
        if (
            call.get("verb") != "wait"
            or call.get("wait_for") != "pullback"
            or not pt.get("limit")
            or e.get("wait")
        ):
            continue
        if not a.get("tickers") or not _direction(a):
            continue
        sym = a["tickers"][0]
        sign = 1 if _direction(a) == "up" else -1
        q = engine.price_of(sym, 300, now=now)
        if not q or not q[1] or q[0] < float(a["created"]):
            continue
        if sign * (q[1] - pt["limit"]) <= 0:
            rows.append({"alert": a, "symbol": sym, "sign": sign, "px": q[1], "patient": pt,
                         "ref": (e.get("refs") or {}).get(sym)})  # fmt: skip
    return rows


async def send_waits(engine: Engine, now: float | None = None) -> int:
    """The wait is over: one push per alert per phone, at the open or at the patient entry. Each
    ended wait is stamped on its alert (edge.wait), pushed or not."""
    now = now or time.time()
    rates = getattr(engine, "wait_rates", None) or {}
    key = f"waits:{datetime.fromtimestamp(now, ET).date().isoformat()}"
    done = set(json.loads(engine.storage.get_setting(key) or "[]"))
    sent = 0
    jobs = []
    for r in open_due(engine, now):
        a, sym = r["alert"], r["symbol"]
        opened = f"opened {r['gap']:+.1f}% its way, " if r["gap"] is not None else ""
        lines = [f"The call said wait for the open. {sym} {opened}now {r['its']:+.1f}% from the alert price."]
        if r["record"]:
            from .opening import open_line

            lines.append(open_line(r["record"]) + ".")
        if r["read"]:
            lines.append(f"Now: {r['read']}.")
        if wait_line(rates.get("open")):
            lines.append(wait_line(rates.get("open")))
        jobs.append((a, sym, {"for": "open", "ts": now, "px": r["px"], "its": r["its"]},
                     f"⏰ {sym} at the open: {r['its']:+.1f}% from the alert", lines))  # fmt: skip
    for r in pullback_due(engine, now):
        a, sym, pt = r["alert"], r["symbol"], r["patient"]
        lines = [f"The call said wait for a pullback. {sym} is back to ${pt['limit']:,.2f}, the patient entry "
                 f"({pt['dip']:.1f}% from the alert, the winners' median dip)."]  # fmt: skip
        if pt.get("p_filled") is not None and pt.get("p_now") is not None:
            lines.append(
                f"Filled there, the odds read {round(pt['p_filled'] * 100)}% (from {round(pt['p_now'] * 100)}% at the alert): losers dip too."
            )
        if wait_line(rates.get("pullback")):
            lines.append(wait_line(rates.get("pullback")))
        jobs.append((a, sym, {"for": "pullback", "ts": now, "px": r["px"], "limit": pt["limit"]},
                     f"⏸ {sym} back to the patient entry: ${pt['limit']:,.2f}", lines))  # fmt: skip
    for a, sym, stamp, title, lines in jobs:
        if not (a.get("edge") or {}).get("wait"):
            engine.storage.update_alert_edge(a["id"], {"wait": stamp})
        if engine.webpush is None:
            continue
        body = f"{str(a.get('title') or '')[:110]}\n" + " ".join(lines)
        for dev in sorted(_devices(engine, a, sym)):
            k = f"{dev}:{a['id']}"
            if k in done:
                continue
            done.add(k)
            payload = {"id": f"wt-{a['id']}", "title": title, "body": body[:500], "severity": "MEDIUM",
                       "kind": "system", "tickers": [sym], "tag": f"wt-{sym}", "ts": now,
                       "open": f"/app/#a={a['id']}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    if jobs:
        engine.storage.set_setting(key, json.dumps(sorted(done)))
    return sent
