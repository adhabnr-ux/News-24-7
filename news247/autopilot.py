"""Paper autopilot: forward-test a rule on new alerts, hands-free.

A rule is a filter from the strategy lab (a kind of news or any, a company size, a minimum
severity) and an exit (an hour later, at the close, the next session's close). Every new alert
that matches opens a paper call on that phone with its plan's stop and target (plans.py), and the
call closes itself at the exit, or earlier when the stop or the target is reached. Each rule keeps
its own record, so an edge found in the history is tested on alerts it has never seen. Paper only:
the system never trades.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .desk import SEV_RANK, session_edge

ET = ZoneInfo("America/New_York")
MAX_RULES = 5
PER_RULE_PER_DAY = 3
EXITS = {"h1": "an hour later", "close": "at the close", "d1": "at the next day's close"}
ODDS_CHOICES = (0, 55, 60, 65, 70)  # a rule can follow the odds model: only alerts it rates this high
# a rule can follow a signal instead of a kind of news: the call when the alert goes out, a second
# leg when it breaks out (secondleg.py), or the call turning to "act now" during the day (callnow.py)
FOLLOWS = ("", "call", "leg2", "turn")


def rule_label(rule: dict[str, Any]) -> str:
    from .catalysts import BAND_LABEL, LABELS

    if rule.get("follow") == "call":
        return f"Following the call ({rule['min_sev']}+): act now at once, wait for the open at the open; out {EXITS[rule['exit']]}"
    if rule.get("follow") == "leg2":
        return f"Following second legs ({rule['min_sev']}+): in at the breakout; out {EXITS[rule['exit']]}"
    if rule.get("follow") == "turn":
        return f"Following the call when it turns to act ({rule['min_sev']}+): in at the turn; out {EXITS[rule['exit']]}"

    kind = (
        LABELS.get(rule["catalyst"], "Any kind of news") if rule["catalyst"] != "all" else "Any kind of news"
    )
    band = BAND_LABEL.get(rule["band"], "any size") if rule["band"] != "all" else "any size"
    odds = f", odds {rule['min_odds']}%+" if rule.get("min_odds") else ""
    return f"{kind}, {band}, {rule['min_sev']}+{odds}, out {EXITS[rule['exit']]}"


def clean_rule(body: dict[str, Any], now: float) -> dict[str, Any]:
    """A rule from the app, checked: raises ValueError on anything it doesn't know."""
    import secrets

    from .catalysts import LABELS

    catalyst = str(body.get("catalyst") or "all")
    band = str(body.get("band") or "all")
    min_sev = str(body.get("min_sev") or "HIGH").upper()
    exit_ = str(body.get("exit") or "close")
    if catalyst != "all" and catalyst not in LABELS:
        raise ValueError("not a kind of news Foretape reads")
    if band not in ("all", "small", "mid", "large"):
        raise ValueError("size is all, small, mid or large")
    if min_sev not in ("MEDIUM", "HIGH", "CRITICAL"):
        raise ValueError("severity is MEDIUM, HIGH or CRITICAL")
    if exit_ not in EXITS:
        raise ValueError("exit is h1, close or d1")
    try:
        min_odds = int(body.get("min_odds") or 0)
    except (TypeError, ValueError):
        min_odds = -1
    if min_odds not in ODDS_CHOICES:
        raise ValueError("odds are 0 (any), 55, 60, 65 or 70")
    follow = str(body.get("follow") or "")
    if follow not in FOLLOWS:
        raise ValueError("follow is call, leg2, turn or nothing")
    if follow and (catalyst != "all" or band != "all" or min_odds):
        who = "the call" if follow == "call" else "the signal"
        raise ValueError(f"following {who} takes every kind and size: {who} decides")
    rule = {"id": secrets.token_hex(4), "catalyst": catalyst, "band": band, "min_sev": min_sev, "exit": exit_,
            "min_odds": min_odds, "on": True, "created": now}  # fmt: skip
    if follow:
        rule["follow"] = follow
    rule["label"] = rule_label(rule)
    return rule


def matches(rule: dict[str, Any], alert: dict[str, Any]) -> bool:
    from .dossier import _direction

    if not rule.get("on") or alert.get("kind") == "system":
        return False
    if SEV_RANK.get(str(alert.get("severity")), 0) < SEV_RANK[rule["min_sev"]]:
        return False
    if not _direction(alert):
        return False
    if rule.get("follow") in ("leg2", "turn"):
        return False  # these open on their signal, later in the day (signal_matches)
    if rule.get("follow") == "call":  # the call decides: act now opens at once (waits are scheduled)
        return ((alert.get("edge") or {}).get("call") or {}).get("verb") == "act"
    if rule.get("min_odds"):
        o = (alert.get("edge") or {}).get("odds") or {}
        if o.get("p") is None or o["p"] * 100 < rule["min_odds"]:
            return False  # following the model: only alerts it rates this high
    c = (alert.get("edge") or {}).get("catalyst")
    if not c:
        # only news Foretape could read for a kind (that's what the record is about), unless the rule
        # follows the model on any kind of news: its odds are the record then
        return bool(rule.get("min_odds")) and rule["catalyst"] == "all" and rule["band"] == "all"
    if rule["catalyst"] != "all" and c["key"] != rule["catalyst"]:
        return False
    return rule["band"] == "all" or c["band"] == rule["band"]


def signal_matches(rule: dict[str, Any], alert: dict[str, Any], signal: str) -> bool:
    """A rule following ``signal`` ("leg2" or "turn"), on an alert at its severity with a direction."""
    from .dossier import _direction

    if (
        rule.get("follow") != signal
        or not rule.get("on")
        or alert.get("kind") == "system"
        or not _direction(alert)
    ):
        return False
    return SEV_RANK.get(str(alert.get("severity")), 0) >= SEV_RANK[rule["min_sev"]]


def waits_for_open(rule: dict[str, Any], alert: dict[str, Any]) -> bool:
    """A rule following the call, on an alert the call says to wait for the open on: entered at the
    session's first print, not now."""
    from .dossier import _direction

    if (
        rule.get("follow") != "call"
        or not rule.get("on")
        or alert.get("kind") == "system"
        or not _direction(alert)
    ):
        return False
    if SEV_RANK.get(str(alert.get("severity")), 0) < SEV_RANK[rule["min_sev"]]:
        return False
    call = (alert.get("edge") or {}).get("call") or {}
    return call.get("verb") == "wait" and call.get("wait_for") == "open"


def exit_time(exit_: str, ts: float, calendar: Any = None) -> float:
    if exit_ == "h1":
        return ts + 3600.0
    first = session_edge("close", ts, calendar)
    return first if exit_ == "close" else session_edge("close", first, calendar)


def opened_today(p: dict[str, Any], rule_id: str, now: float) -> int:
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return sum(
        1
        for t in [*p.get("tracked", []), *p.get("closed", [])]
        if t.get("auto") == rule_id and t.get("ts", 0) >= day0
    )


def rule_stats(p: dict[str, Any], rule_id: str) -> dict[str, Any]:
    done = [c for c in p.get("closed") or [] if c.get("auto") == rule_id and c.get("pnl_pct") is not None]
    rets = [c["pnl_pct"] for c in done]
    return {
        "open": sum(1 for t in p.get("tracked") or [] if t.get("auto") == rule_id),
        "closed": len(done),
        "win": round(sum(r > 0 for r in rets) / len(rets), 2) if rets else None,
        "avg": round(sum(rets) / len(rets), 2) if rets else None,
        "total": round(sum(rets), 2) if rets else None,
        "last": [
            {
                "symbol": c["symbol"],
                "direction": c["direction"],
                "pnl_pct": c["pnl_pct"],
                "closed": c.get("closed"),
                "why": c.get("lesson", ""),
            }
            for c in done[:5]
        ],  # fmt: skip
    }


def due_exit(t: dict[str, Any], price: float | None, now: float) -> str:
    """Why an autopilot call closes now ("stop", "target", "time"), or "" to keep it open."""
    if price is not None:
        long = t["direction"] == "long"
        if t.get("stop") is not None and (price <= t["stop"] if long else price >= t["stop"]):
            return "stop"
        if t.get("target") is not None and (price >= t["target"] if long else price <= t["target"]):
            return "target"
    return "time" if now >= (t.get("exit_at") or float("inf")) else ""
