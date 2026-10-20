"""You against the playbook: your paper calls, set against what the playbook did on the same alerts.

The coach (run 16) reads your habits from your own calls; your reaction time (run 59) prices your
delay. Neither says what you'd have made doing it the record's way. Every closed paper call you
opened from an alert (not the autopilots' calls) is now set beside, on the same alert:

- **The playbook:** the alert's graded pair, its kind's way in and way out as stamped when it went
  out (combos.py; its kind's separate picks when no pair was graded yet), replayed from the alert's
  own graded prices. An alert whose playbook entry never came (no second leg, the limit never
  filled) counts as a pass: 0%.
- **The plain way:** in at the alert, out at the close.

With 10+ such calls the panel says which way the gap runs: "Following the playbook on your 24
calls would have made +0.8% a call more" or "you beat the playbook by +0.4% a call". It shows the
calls with the widest gaps, and the same split per kind of news, so the habit behind the gap shows
(selling the next-day winners at noon, chasing the second leg the record says to skip). Paper only.
History, not a forecast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 10
EDGE = 0.25


def playbook_result(a: dict[str, Any]) -> tuple[float, str] | None:
    """(what the playbook made on this graded alert, the pair) — 0 when its entry never came."""
    from .autopilot import playbook_in, playbook_out
    from .combos import DEFAULT, combos

    got = combos(a)
    if DEFAULT not in got:
        return None
    if ((a.get("edge") or {}).get("combo_rec") or {}).get("pass"):
        return 0.0, "pass"  # the playbook sat it out
    pin, pout = playbook_in(a), playbook_out(a)
    if not pin or not pout:
        return got[DEFAULT], DEFAULT
    pair = f"{pin}|{pout}"
    return got.get(pair, 0.0), pair


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def compare(engine: Engine, device: str) -> dict[str, Any]:
    """This phone's graded manual calls against the playbook and the plain way on the same alerts."""
    from .catalysts import classify
    from .combos import DEFAULT, NAMES

    p = engine.desk.profiles.get(device) or {}
    calls = []
    for c in p.get("closed") or []:
        if c.get("auto") or c.get("pnl_pct") is None or not c.get("alert_id"):
            continue
        a = engine.storage.get_alert(c["alert_id"])
        if not a:
            continue
        pb = playbook_result(a)
        if not pb:
            continue
        o = (a.get("edge") or {}).get("outcome") or {}
        row = o.get(c["symbol"]) or next(iter(o.values()))
        # the alert's direction: a call against it is graded against the playbook taken its own way
        same = (1 if c.get("direction", "long") == "long" else -1) == row["sign"]
        cat = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        calls.append({"symbol": c["symbol"], "you": round(float(c["pnl_pct"]), 2), "playbook": pb[0] if same else -pb[0] or 0.0,
                      "plain": row["sign"] * row["close"] if same else -row["sign"] * row["close"],
                      "pair": "The playbook passes" if pb[1] == "pass" else NAMES.get(pb[1], pb[1]), "kind": cat["key"] if cat else None,
                      "label": cat["label"] if cat else "Other news", "closed": c.get("closed"), "title": (a.get("title") or "")[:120]})  # fmt: skip
    out: dict[str, Any] = {"n": len(calls), "need": MIN_N, "default": NAMES[DEFAULT]}
    if not calls:
        return out
    out["you"] = _avg([c["you"] for c in calls])
    out["playbook"] = _avg([c["playbook"] for c in calls])
    out["plain"] = _avg([c["plain"] for c in calls])
    out["gap"] = round(out["playbook"] - out["you"], 2)
    if len(calls) >= MIN_N:
        out["verdict"] = "playbook" if out["gap"] >= EDGE else "you" if out["gap"] <= -EDGE else "even"
    kinds: dict[str, list[dict[str, Any]]] = {}
    for c in calls:
        kinds.setdefault(c["label"], []).append(c)
    out["kinds"] = sorted(
        ({"label": k, "n": len(v), "you": _avg([c["you"] for c in v]), "playbook": _avg([c["playbook"] for c in v])}
         for k, v in kinds.items() if len(v) >= 3),
        key=lambda r: r["you"] - r["playbook"],
    )  # fmt: skip
    out["widest"] = sorted(calls, key=lambda c: -abs(c["playbook"] - c["you"]))[:5]
    out["line"] = line(out)
    return out


def line(r: dict[str, Any]) -> str:
    if not r.get("n"):
        return ""
    if r.get("verdict") == "playbook":
        return f"Following the playbook on your {r['n']} calls would have made {r['gap']:+.2f}% a call more ({r['playbook']:+.2f}% against your {r['you']:+.2f}%)."
    if r.get("verdict") == "you":
        return f"You beat the playbook by {-r['gap']:+.2f}% a call on your {r['n']} calls ({r['you']:+.2f}% against {r['playbook']:+.2f}%)."
    if r.get("verdict") == "even":
        return f"You and the playbook are about even on your {r['n']} calls ({r['you']:+.2f}% against {r['playbook']:+.2f}% a call)."
    return f"{r['n']} of the {r['need']} graded calls from alerts it needs: you {r['you']:+.2f}% a call, the playbook {r['playbook']:+.2f}%."
