"""The playbook's promises against its results: does the learning pay, live, on paper?

Everything the desk learns is graded on history. The paper autopilots are its forward test: they act
on alerts nobody had seen when the records were fit. This grades each autopilot's closed calls on
the same alerts two ways:

- **Against the plain way:** the same alert taken at the alert price and out at that session's close,
  in the call's direction (the alert's own graded close). The difference is what the rule's choices
  (when to get in, when to get out, where to stop) added or cost, alert for alert, so a good or bad
  week for the market cancels out.
- **Against its promise** (the playbook autopilot): when it opened each call, the record said what
  its kind's chosen way had made on average (the exit's average when entered at the alert, else the
  entry's average to the close). Live against promised says whether the record's picks keep their
  word out of sample.

With 10+ graded calls a rule gets a verdict: the choices **add** (+0.25% a call or more against the
plain way), **cost** (-0.25% or worse), or come out **even**. Per kind of news for the playbook.
Paper only. History, not a forecast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 10
EDGE = 0.25  # % a call either way for a verdict


def promise(edge: dict[str, Any]) -> dict[str, Any] | None:
    """What the record said a playbook call would make, from the alert's stamps: {kind, in, out, stop, promise}."""
    en, ex, so = edge.get("entry_rec") or {}, edge.get("exit_rec") or {}, edge.get("stop_rec") or {}
    cr = edge.get("combo_rec") or {}
    if cr.get("in") and cr.get("smooth"):  # the smoother split it takes instead (combos.py)
        sm = cr["smooth"]
        return {
            "kind": cr.get("kind"),
            "label": cr.get("label"),
            "in": cr["in"],
            "out": sm["out"],
            "stop": None,
            "promise": sm["avg"],
        }
    if cr.get("in"):  # the pair, backtested together: its average is the exact promise (combos.py)
        return {"kind": cr.get("kind"), "label": cr.get("label"), "in": cr["in"], "out": cr["out"],
                "stop": so.get("best") if so.get("width") else None,
                "promise": (cr.get("styles") or {}).get(cr["best"], {}).get("avg")}  # fmt: skip
    if not en.get("best") or not ex.get("best"):
        return None
    if en["best"] == "alert":
        avg = (ex.get("styles") or {}).get(ex["best"], {}).get("avg")
    else:
        avg = (en.get("styles") or {}).get(en["best"], {}).get("avg")
    return {"kind": en.get("kind") or ex.get("kind"), "label": en.get("label") or ex.get("label"), "in": en["best"],
            "out": ex["best"], "stop": so.get("best") if so.get("width") else None, "promise": avg}  # fmt: skip


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def _summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    live, plain = _avg([c["live"] for c in calls]), _avg([c["plain"] for c in calls])
    out: dict[str, Any] = {"n": len(calls), "live": live, "plain": plain, "need": MIN_N}
    out["edge"] = round(live - plain, 2) if live is not None and plain is not None else None
    pr = [c for c in calls if c.get("promise") is not None]
    if pr:
        out["promised"] = _avg([c["promise"] for c in pr])
        out["promised_live"] = _avg([c["live"] for c in pr])
        out["promised_n"] = len(pr)
    if len(calls) >= MIN_N and out["edge"] is not None:
        out["verdict"] = "adds" if out["edge"] >= EDGE else "costs" if out["edge"] <= -EDGE else "even"
    return out


def calls(engine: Engine, device: str) -> dict[str, list[dict[str, Any]]]:
    """{rule id: [{symbol, live, plain, promise, kind, label, closed}]} for this phone's graded autopilot calls."""
    p = engine.desk.profiles.get(device) or {}
    out: dict[str, list[dict[str, Any]]] = {}
    for c in p.get("closed") or []:
        if not c.get("auto") or c.get("pnl_pct") is None or not c.get("alert_id"):
            continue
        a = engine.storage.get_alert(c["alert_id"])
        o = ((a or {}).get("edge") or {}).get("outcome") or {}
        row = o.get(c["symbol"]) or (next(iter(o.values())) if o else None)
        if not row or row.get("close") is None:
            continue
        sign = 1 if c.get("direction", "long") == "long" else -1
        pb = c.get("pb") or {}
        out.setdefault(c["auto"], []).append({"symbol": c["symbol"], "live": round(float(c["pnl_pct"]), 2),
                                              "plain": round(sign * row["close"], 2), "promise": pb.get("promise"),
                                              "kind": pb.get("kind"), "label": pb.get("label"), "closed": c.get("closed")})  # fmt: skip
    return out


def forward(engine: Engine, device: str) -> dict[str, Any]:
    """Each autopilot against the plain way on the same alerts, and the playbook against its promise."""
    p = engine.desk.profiles.get(device) or {}
    rules = {r["id"]: r for r in p.get("autopilots") or []}
    out = {}
    for rid, cs in calls(engine, device).items():
        s = _summary(cs)
        s["label"] = (rules.get(rid) or {}).get("label") or "A removed autopilot"
        s["last"] = sorted(cs, key=lambda c: -(c["closed"] or 0))[:5]
        kinds = {}
        for c in cs:
            if c.get("kind"):
                kinds.setdefault(c["kind"], []).append(c)
        s["kinds"] = sorted(({**_summary(v), "kind": k, "label": v[0]["label"]} for k, v in kinds.items() if len(v) >= 3),
                            key=lambda r: -(r["edge"] or 0))  # fmt: skip
        s["line"] = line(s)
        out[rid] = s
    return {"rules": out, "need": MIN_N}


def line(s: dict[str, Any]) -> str:
    if not s.get("n"):
        return ""
    head = f"Live {s['live']:+.2f}% a call against {s['plain']:+.2f}% the plain way on the same alerts ({s['n']})"
    if s.get("verdict"):
        head += {"adds": ": its choices add", "costs": ": its choices cost", "even": ": about even"}[
            s["verdict"]
        ] + f" {s['edge']:+.2f}% a call"
    if s.get("promised") is not None:
        head += f"; the record promised {s['promised']:+.2f}%, live {s['promised_live']:+.2f}% ({s['promised_n']})"
    return head + "."
