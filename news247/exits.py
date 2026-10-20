"""Exit autopsy: what each paper call's stock did after you closed it.

Entries get all the attention; exits decide most of the result. Each closed call is graded after
the fact: the stock's move from the exit price at the close of the session it was closed in, the
next session's close (d1) and five sessions on (d5), in the call's direction (+ = it kept going the
call's way after you left; − = it turned, and the exit saved that much). Calls closed from now on are
graded from live quotes as those closes pass. Older calls opened from an alert are graded from the
alert's own graded closes when they fall on the same session closes, so nothing is estimated.

Pooled, the grades answer the questions a trader rarely gets numbers for:

- **Winning exits:** after you took a gain, did the stock keep going your way (selling winners
  early), or turn (well timed)?
- **Losing exits:** after you cut, did it keep going against the call (the cut paid), or come back?
- **Stops:** how many stopped calls were back at their entry by the next close (stops inside the
  noise), beside how far this server's winners usually dipped before working (heat.py).
- **By how you exited:** stop, target, a management nudge, the autopilot's clock, or by hand.
- **Exit edge:** what the exits added per call against holding one more session.

Paper only. History, not a forecast: a few dozen exits is a small sample, and counts are shown.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

HORIZONS = ("close", "d1", "d5")
MIN_N = 5  # graded exits in a group before it gets a verdict
MIN_STOPS = 4
NUDGES = ("gave back the gain", "dead money", "cut on the live odds")
KIND_LABEL = {"stop": "Stopped out", "target": "At the target", "nudge": "On a nudge", "time": "On the clock",
              "manual": "By hand"}  # fmt: skip
WORDS = {"close": "by that close", "d1": "by the next close", "d5": "five sessions on"}


def _sign(c: dict[str, Any]) -> int:
    return 1 if c.get("direction", "long") == "long" else -1


def exit_kind(c: dict[str, Any]) -> str:
    """How a call was closed: "stop", "target", "nudge", "time" (the autopilot's clock) or "manual"."""
    lesson = str(c.get("lesson") or "")
    if lesson.startswith("autopilot: stopped"):
        return "stop"
    if lesson.startswith("autopilot: target"):
        return "target"
    if lesson.startswith("autopilot: out"):
        return "time"
    if lesson in NUDGES:
        return "nudge"
    x, s = c.get("exit"), _sign(c)
    if x:
        if c.get("stop") and s * (x - c["stop"]) <= 0.001 * x:
            return "stop"
        if c.get("target") and s * (x - c["target"]) >= -0.001 * x:
            return "target"
    return "manual"


def grade_exits(engine: Engine, now: float | None = None) -> int:
    """Stamp each recently closed call with its stock's move from the exit price at each horizon
    (c["after"], raw %), from a quote within 3 hours of that close. Returns calls stamped."""
    from .dossier import session_closes

    now = now or time.time()
    holidays = getattr(engine.calendar, "holidays", set())
    made = 0
    for dev, p in list(engine.desk.profiles.items()):
        changed = False
        for c in p.get("closed") or []:
            after = c.get("after") or {}
            if not c.get("exit") or not c.get("closed") or now - c["closed"] > 10 * 86400:
                continue
            if all(h in after for h in HORIZONS):
                continue
            q = engine.price_of(c["symbol"], now=now)
            if not q or not q[1] or q[0] < c["closed"]:
                continue
            closes = session_closes(c["closed"], 6, holidays)
            new = dict(after)
            for h, ts in zip(HORIZONS, (closes[0], closes[1], closes[5]), strict=True):
                if h not in new and now >= ts and abs(q[0] - ts) < 3 * 3600:
                    new[h] = round((q[1] / c["exit"] - 1) * 100, 2)
            if new != after:
                c["after"] = new
                changed = True
                made += 1
        if changed:
            engine.desk.save(dev)
    return made


def from_alert(engine: Engine, c: dict[str, Any], a: dict[str, Any] | None) -> dict[str, float]:
    """The same grades for a call opened from an alert, from that alert's graded closes, wherever
    they fall on the call's own horizons (the same session closes)."""
    from .dossier import session_closes

    if not a or not c.get("exit") or not c.get("closed"):
        return {}
    e = a.get("edge") or {}
    ref = (e.get("refs") or {}).get(c["symbol"])
    row = (e.get("outcome") or {}).get(c["symbol"]) or {}
    if not ref or not row:
        return {}
    holidays = getattr(engine.calendar, "holidays", set())
    ac = session_closes(float(a["created"]), 6, holidays)
    alert_at = {ac[0]: row.get("close"), ac[1]: row.get("d1"), ac[5]: row.get("d5")}
    cc = session_closes(c["closed"], 6, holidays)
    out = {}
    for h, ts in zip(HORIZONS, (cc[0], cc[1], cc[5]), strict=True):
        mv = alert_at.get(ts)
        if mv is not None:
            price = float(ref) * (1 + mv / 100)
            out[h] = round((price / c["exit"] - 1) * 100, 2)
    return out


def _after(engine: Engine, c: dict[str, Any], alerts: dict[str, Any]) -> dict[str, float]:
    """Live grades first; the alert's fill in what they don't have."""
    got = dict(c.get("after") or {})
    if c.get("alert_id") and not all(h in got for h in HORIZONS):
        for h, v in from_alert(engine, c, alerts.get(c["alert_id"])).items():
            got.setdefault(h, v)
    return got


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "kept": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {h: _stats([r["its"][h] for r in rows if h in r["its"]]) for h in HORIZONS}


def _far(g: dict[str, Any], min_n: int = MIN_N) -> tuple[str, dict[str, Any]] | None:
    """The furthest horizon with enough graded: five sessions on, else the next close, else that close."""
    for h in ("d5", "d1", "close"):
        x = g.get(h)
        if x and x["n"] >= min_n:
            return h, x
    return None


def exit_report(engine: Engine, device: str, days: float = 120, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    closed = [
        c
        for c in p.get("closed") or []
        if c.get("exit") and c.get("pnl_pct") is not None and (c.get("closed") or 0) >= now - days * 86400
    ]
    alerts = {}
    for c in closed:
        if c.get("alert_id") and c["alert_id"] not in alerts:
            alerts[c["alert_id"]] = engine.storage.get_alert(c["alert_id"])
    rows = []
    for c in closed:
        after = _after(engine, c, alerts)
        s = _sign(c)
        its = {h: round(s * v, 2) for h, v in after.items()}
        row = {"id": c["id"], "symbol": c["symbol"], "direction": c.get("direction", "long"), "pnl": c["pnl_pct"],
               "closed": c["closed"], "kind": exit_kind(c), "its": its, "auto": bool(c.get("auto"))}  # fmt: skip
        if c.get("entry") and "d1" in after:  # back at the entry or better a session on: the exit was early
            px = c["exit"] * (1 + after["d1"] / 100)
            row["back_d1"] = s * (px / c["entry"] - 1) >= 0
        far = next((h for h in ("d5", "d1", "close") if h in its), None)
        if far:
            v = its[far]
            row["verdict"] = "left" if v >= 0.5 else "saved" if v <= -0.5 else "even"
            row["at"] = far
        rows.append(row)
    graded = [r for r in rows if r["its"]]
    winners = [r for r in graded if r["pnl"] > 0]
    losers = [r for r in graded if r["pnl"] <= 0]
    out: dict[str, Any] = {"days": days, "closed": len(rows), "graded": len(graded), "need": MIN_N,
                           "all": _group(graded), "winners": _group(winners), "losers": _group(losers),
                           "kinds": [], "insights": [], "rows": rows[:20]}  # fmt: skip
    for k, label in KIND_LABEL.items():
        g = [r for r in graded if r["kind"] == k]
        if g:
            out["kinds"].append({"kind": k, "label": label, "n": len(g), **_group(g)})
    # exit edge: what the exit added per call against holding one more session (− of the move after)
    d1 = [r["its"]["d1"] for r in graded if "d1" in r["its"]]
    if len(d1) >= MIN_N:
        out["edge"] = {"n": len(d1), "median": round(-st.median(d1), 2), "mean": round(-sum(d1) / len(d1), 2)}
    # stops that cost (a stop trailed into profit is a protected gain, not a stop-out)
    stops = [r for r in rows if r["kind"] == "stop" and r["pnl"] <= 0 and "back_d1" in r]
    if stops:
        out["stops"] = {"n": len(stops), "back": sum(r["back_d1"] for r in stops)}
        dists = [
            abs(c["exit"] / c["entry"] - 1) * 100
            for c in closed
            if exit_kind(c) == "stop" and c.get("entry") and c["id"] in {r["id"] for r in stops}
        ]
        if dists:
            out["stops"]["distance"] = round(st.median(dists), 2)
        heat = (getattr(engine, "heat_rates", None) or {}).get("*")
        if heat:
            out["stops"]["dip80"] = heat["dip80"]
    out["insights"] = _insights(out)
    return out


def _insights(r: dict[str, Any]) -> list[dict[str, str]]:
    """The few plain sentences the numbers support, each with its count."""
    out = []
    w = _far(r["winners"])
    if w:
        h, x = w
        if x["kept"] >= 0.6 and x["median"] > 0.3:
            out.append({"key": "early", "tone": "warn",
                        "text": f"You sell winners early: after {round(x['kept'] * 100)}% of your winning exits the stock kept going your way, "
                                f"median {x['median']:+.1f}% {WORDS[h]} ({x['n']})"})  # fmt: skip
        elif x["kept"] <= 0.4 and x["median"] < 0:
            out.append({"key": "timed", "tone": "good",
                        "text": f"Your winning exits were well timed: the stock turned after {round((1 - x['kept']) * 100)}% of them, "
                                f"median {x['median']:+.1f}% {WORDS[h]} ({x['n']})"})  # fmt: skip
    lo = _far(r["losers"])
    if lo:
        h, x = lo
        if x["kept"] <= 0.4 and x["median"] < 0:
            out.append({"key": "cut", "tone": "good",
                        "text": f"Cutting losers paid: after {round((1 - x['kept']) * 100)}% of your losing exits it kept going against the call, "
                                f"median {x['median']:+.1f}% {WORDS[h]} ({x['n']})"})  # fmt: skip
        elif x["kept"] >= 0.6 and x["median"] > 0.3:
            out.append({"key": "bottom", "tone": "warn",
                        "text": f"You cut near the turn: after {round(x['kept'] * 100)}% of your losing exits the stock came back your way, "
                                f"median {x['median']:+.1f}% {WORDS[h]} ({x['n']})"})  # fmt: skip
    s = r.get("stops")
    if s and s["n"] >= MIN_STOPS and s["back"] / s["n"] >= 0.5:
        heat = ""
        if s.get("dip80") and s.get("distance") is not None and s["distance"] < s["dip80"]:
            heat = f"; they sat {s['distance']:.1f}% away, inside the {s['dip80']:.1f}% that 80% of winners here dipped before working"
        out.append({"key": "stops", "tone": "warn",
                    "text": f"Your stops sit inside the noise: {s['back']} of {s['n']} stopped calls were back at their entry by the next close{heat}"})  # fmt: skip
    e = r.get("edge")
    if e:
        out.append({"key": "edge", "tone": "good" if e["median"] > 0 else "bad" if e["median"] < -0.3 else "info",
                    "text": f"Exit edge: {e['median']:+.1f}% a call (median) against holding one more session ({e['n']})"})  # fmt: skip
    return out
