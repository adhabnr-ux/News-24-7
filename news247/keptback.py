"""What your settings cost you (and saved you): every alert this phone's own settings kept off its
lock screen, graded by what the stock did next, beside the alerts that came through.

A phone's settings are bets: an edge bar, a severity floor, a kind of news turned off, quiet hours,
a muted source, never-push words, a stock set to HIGH only. Each bet has a record here. For the
last two weeks of graded alerts that would have reached this phone on severity alone, it walks the
same gates the push does (desk.block) and pools what each setting held back: how often those
alerts went their way by the close and by how much, against the ones that reached you. A setting
that keeps holding back better alerts than you get is flagged, with the one tap that loosens it;
one that holds back losers is shown working.

Judged by the settings the phone keeps now. Passing states (focus, a snooze, a mute) are left out:
they say nothing about a setting, and today's would wrongly cover last week.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .models import Severity

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 5  # alerts a setting must have kept back before it is judged
EDGE = 0.10  # this much better (or worse) a hit rate than what came through, to call it
BIG = 3.0  # % its way by the close: a big winner


def _setting(b: dict[str, Any]) -> str:
    code, body = b["code"], b["fix"].get("body") or {}
    if code == "category":
        return f"{next(iter(body.get('cats') or {'these': 1})).capitalize()} alerts turned off"
    if code == "stock_off":
        return f"{body.get('symbol')} set to never push"
    if code == "stock_high":
        return f"{body.get('symbol')} set to HIGH only"
    if code == "fades":
        return "Skipping alerts that usually fade"
    if code == "source":
        return f"Muted source: {body.get('mute_source')}"
    if code == "catalyst_off":
        return b["fix"]["label"].replace("Push ", "").replace(" again", "").capitalize() + " turned off"
    if code == "edge":
        return "Your edge bar"
    if code == "word":
        return f"Never-push word “{body.get('keyword')}”"
    if code == "floor":
        return "Your severity floor"
    if code == "quiet":
        return "Your quiet hours"
    if code == "budget":
        return "Your daily budget"
    return b["text"]


def _stats(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {"n": 0, "hit": None, "avg": None, "big": 0}
    return {
        "n": len(rets),
        "hit": round(sum(r > 0 for r in rets) / len(rets), 2),
        "avg": round(sum(rets) / len(rets), 2),
        "big": sum(r >= BIG for r in rets),
    }


def settings_cost(engine: Engine, device: str, days: float = 14, now: float | None = None) -> dict[str, Any]:
    """{"came", "kept", "gates": [{setting, code, n, hit, avg, big, verdict, fix, examples}], "misses"}."""
    from .dossier import _direction
    from .pushcheck import phone_floor

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    floor = phone_floor(engine) or Severity.HIGH
    mine = set(p.get("watchlist") or []) | set(p.get("holdings") or {})
    came: list[float] = []
    groups: dict[str, dict[str, Any]] = {}
    for a in engine.storage.recent_alerts(4000, since=now - days * 86400):
        if a.get("kind") == "system" or not _direction(a):
            continue
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("close") is None:
            continue
        r = round(row["sign"] * row["close"], 2)
        try:
            sev = Severity.parse(str(a.get("severity") or "LOW"))
        except KeyError:
            continue
        if not (sev >= floor or (sev == Severity.MEDIUM and mine & set(a.get("tickers") or []))):
            continue  # the server wouldn't have sent it to any phone: not this phone's setting
        b = engine.desk.block(device, a, float(a.get("created") or now), durable=True)
        if b is None:
            came.append(r)
            continue
        name = _setting(b)
        g = groups.setdefault(
            name, {"setting": name, "code": b["code"], "fix": b["fix"], "rets": [], "examples": []}
        )
        g["rets"].append(r)
        g["examples"].append({"id": a["id"], "title": a.get("title", "")[:140], "symbol": next(iter(o)), "ret": r,
                              "severity": a.get("severity"), "created": a.get("created")})  # fmt: skip
    base = _stats(came)
    ref = base["hit"] if base["n"] >= MIN_N else 0.5
    gates = []
    for g in groups.values():
        s = _stats(g.pop("rets"))
        verdict = "thin"
        if s["n"] >= MIN_N:
            if s["hit"] >= ref + EDGE and s["avg"] > 0:
                verdict = "costing"
            elif s["avg"] < 0 or s["hit"] <= ref - EDGE:
                verdict = "saving"
            else:
                verdict = "even"
        g["examples"] = sorted(g["examples"], key=lambda e: -e["ret"])[:3]
        gates.append({**g, **s, "verdict": verdict})
    gates.sort(key=lambda g: ({"costing": 0, "saving": 1, "even": 2, "thin": 3}[g["verdict"]], -g["n"]))
    kept_all = [{**e, "setting": g["setting"]} for g in gates for e in g["examples"]]
    misses = sorted((e for e in kept_all if e["ret"] >= BIG), key=lambda e: -e["ret"])[:5]
    n_kept = sum(g["n"] for g in gates)
    return {
        "days": days,
        "came": base,
        "kept": {"n": n_kept, "big": sum(g["big"] for g in gates)},
        "gates": gates,
        "misses": misses,
        "costing": sum(1 for g in gates if g["verdict"] == "costing"),
    }


def cost_line(g: dict[str, Any], came: dict[str, Any]) -> str:
    vs = (
        f" vs {round(came['hit'] * 100)}% of what reached you"
        if came.get("hit") is not None and came["n"] >= MIN_N
        else ""
    )
    return f"{round(g['hit'] * 100)}% went their way by the close{vs} · average {g['avg']:+.1f}% · {g['n']} kept back"
