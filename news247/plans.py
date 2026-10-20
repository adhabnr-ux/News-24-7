"""From an alert to a plan, and from your calls to what they say about you.

``trade_plan``: for an alert with a direction, a paper plan from the live price: the stop where the
news would be proven wrong (back through where the stock was before it), the target at the move
that kind of news has typically made at this company size (catalysts.py), else at what options
price, the reward-to-risk from here, and a position size from this phone's account size and the
share of it it is willing to lose on one idea. Nothing is guessed: a part with no basis is left
out and says why.

``coach``: this phone's closed paper calls read for habits: the payoff of winners against losers,
whether losers are held longer than winners, whether exits come before the alert's move was done,
which catalysts, tags and directions work, and whether stops help. Plain sentences, each with the
numbers behind it, and nothing until there are enough calls to mean something.
"""

from __future__ import annotations

import statistics as st
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
RISK_CHOICES = (0.25, 0.5, 1.0, 2.0)  # % of the account at risk on one idea
COACH_MIN = 5  # closed calls before the coach says anything


def _primary(a: dict[str, Any]) -> str:
    play = (a.get("edge") or {}).get("play") or {}
    return ((play.get("direct") or a.get("tickers") or [""]) or [""])[0]


def trade_plan(
    engine: Engine, device: str, alert: dict[str, Any], now: float | None = None
) -> dict[str, Any]:
    """A paper plan for this alert from the live price (see the module docstring)."""
    from .catalysts import catalyst_base, classify
    from .dossier import _direction

    now = now or time.time()
    sym = _primary(alert)
    d = _direction(alert)
    out: dict[str, Any] = {"symbol": sym, "direction": d, "ok": False}
    if not sym or not d:
        out["why"] = "No direction to plan for: the alert doesn't lean up or down."
        return out
    q = engine.price_of(sym, 1800, now=now)
    if not q:
        out["why"] = f"No live price for {sym} right now."
        return out
    sign = 1 if d == "up" else -1
    px = q[1]
    e = alert.get("edge") or {}
    ref = (
        (e.get("refs") or {}).get(sym)
        or (
            (alert.get("move") or {}).get("price") if (alert.get("move") or {}).get("symbol") == sym else None
        )
        or px
    )
    # the target: the catalyst's typical move at this size, else the nearest expiry's implied move
    cb = e.get("cat_base") or catalyst_base(
        getattr(engine, "catalyst_rates", {}) or {}, e.get("catalyst") or classify(alert, engine.universe)
    )
    target = basis = None
    if cb and cb.get("typical"):
        target = ref * (1 + sign * cb["typical"] / 100)
        basis = f"the typical close move for {cb['label'].lower()} news, {cb['band_label']} ({cb['close']['n']} graded)"
    else:
        cached = (getattr(engine, "implied_cache", {}) or {}).get(sym)
        rows = (cached[1] if cached else {}).get("rows") or []
        if rows and rows[0].get("move_pct"):
            target = ref * (1 + sign * rows[0]["move_pct"] / 100)
            basis = f"what options price by {rows[0].get('expiry', 'the nearest expiry')} (±{rows[0]['move_pct']:.1f}%)"
    # the stop: past where most winners of its kind dipped before working (the record's heat), else
    # back through where it stood before the news, else half the target's distance
    from .heat import BUFFER, KEEP, heat_for

    heat = heat_for(getattr(engine, "heat_rates", None) or {}, alert, engine.universe)
    pre = (e.get("pre") or {}).get(sym)
    before = ref / (1 + pre / 100) if pre is not None else None
    if heat:
        out["heat"] = heat
    if heat and heat["dip80"] > 0:
        dip = heat["dip80"] + BUFFER
        stop = ref * (1 - sign * dip / 100)
        stop_basis = f"past where {round(KEEP * 100)}% of winners on {heat['from']} dipped before working (−{heat['dip80']:.2f}%, {heat['winners']} winners)"
    elif before is not None and sign * (ref - before) / ref * 100 >= 0.3:
        stop, stop_basis = before, "back through where it stood before the news"
    elif target is not None:
        stop, stop_basis = (
            ref - sign * abs(target - ref) / 2,
            "half the target's distance the other way (no pre-news level)",
        )
    else:
        out["why"] = (
            f"No basis for a target yet: {sym}'s kind of news has too few graded alerts and no option prices are cached."
        )
        return out
    out.update(entry=round(px, 4), ref=round(ref, 4), stop=round(stop, 4), stop_basis=stop_basis)
    if target is None:
        out["why"] = "No basis for a target yet (too few graded alerts like it, no option prices cached)."
        return out
    out.update(target=round(target, 4), target_basis=basis)
    risk, reward = sign * (px - stop), sign * (target - px)
    if risk <= 0:
        out["status"] = "invalidated"
        out["why"] = f"{sym} is already through the stop: the plan is off."
        return out
    if reward <= 0:
        out["status"] = "done"
        out["why"] = f"{sym} has already made the typical move: no reward left from here."
        return out
    out.update(
        ok=True,
        status="live",
        risk_pct=round(risk / px * 100, 2),
        reward_pct=round(reward / px * 100, 2),
        rr=round(reward / risk, 2),
    )
    p = engine.desk.profiles.get(device) or {}
    prefs = p.get("prefs") or {}
    account = float(prefs.get("account") or 0)
    if not account and p.get("holdings"):
        from .dossier import portfolio

        account = portfolio(engine, device, now).get("value") or 0.0
        out["account_basis"] = "your portfolio's value (set an account size to change it)"
    risk_pct = float(prefs.get("risk_pct") or 1.0)
    if account > 0:
        budget = account * risk_pct / 100
        shares = int(budget // risk)
        out.update(
            account=round(account, 2),
            risk_budget=round(budget, 2),
            risk_pct_account=risk_pct,
            shares=shares,
            position=round(shares * px, 2),
            position_pct=round(shares * px / account * 100, 1),
        )
    es = edge_size((e.get("odds") or {}).get("p"), out["rr"], risk_pct)
    if es:
        if account > 0 and es["risk_pct"] > 0:
            es["shares"] = int(account * es["risk_pct"] / 100 // risk)
            es["risk_budget"] = round(account * es["risk_pct"] / 100, 2)
        out["edge_size"] = es
    return out


def edge_size(p: float | None, rr: float, cap_pct: float) -> dict[str, Any] | None:
    """How much of the account to risk on a plan, from its odds and its reward-to-risk: half the
    Kelly fraction (p - (1 - p) / rr), never more than the phone's own risk per idea. A plan whose
    odds don't pay at its reward-to-risk gets nothing, with the odds it would need. The odds are
    for the close, the plan's target and stop may come first: a guide, not a formula to trust."""
    if p is None or rr <= 0:
        return None
    kelly = p - (1 - p) / rr
    need = 1 / (1 + rr)
    if kelly <= 0:
        return {"p": p, "kelly_pct": round(kelly * 100, 2), "risk_pct": 0.0, "capped": False, "need": round(need, 3),
                "why": f"No edge at {rr:.1f} : 1 with {round(p * 100)}% odds: it would need {round(need * 100)}%+"}  # fmt: skip
    half = kelly / 2 * 100
    use = round(min(half, cap_pct), 2)
    why = f"half-Kelly at {round(p * 100)}% odds and {rr:.1f} : 1" + (
        f", capped at your {cap_pct:g}%" if half > cap_pct else ""
    )
    return {
        "p": p,
        "kelly_pct": round(kelly * 100, 2),
        "risk_pct": use,
        "capped": half > cap_pct,
        "need": round(need, 3),
        "why": why,
    }


# --------------------------------------------------------------------------- the coach


def _hours(c: dict[str, Any]) -> float | None:
    if c.get("ts") and c.get("closed"):
        return max(0.0, (c["closed"] - c["ts"]) / 3600)
    return None


def coach(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    """What this phone's closed paper calls say (see the module docstring)."""
    from .catalysts import classify

    p = engine.desk.profiles.get(device) or {}
    closed = [c for c in p.get("closed") or [] if c.get("pnl_pct") is not None]
    out: dict[str, Any] = {"n": len(closed), "need": COACH_MIN, "insights": []}
    if len(closed) < COACH_MIN:
        out["enough"] = False
        return out
    out["enough"] = True
    rets = [c["pnl_pct"] for c in closed]
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    out["summary"] = {
        "win_rate": round(len(wins) / len(rets), 2),
        "avg_win": round(avg_w, 2),
        "avg_loss": round(avg_l, 2),
        "payoff": round(avg_w / abs(avg_l), 2) if avg_l else None,
        "expectancy": round(sum(rets) / len(rets), 2),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
    }
    ins: list[dict[str, Any]] = []

    def say(text: str, tone: str, weight: float) -> None:
        ins.append({"text": text, "tone": tone, "weight": round(weight, 2)})

    s = out["summary"]
    exp = s["expectancy"]
    say(
        f"Across {len(rets)} closed calls you average {exp:+.2f}% a call: {round(s['win_rate'] * 100)}% win, "
        f"winners {avg_w:+.2f}% on average, losers {avg_l:+.2f}%.",
        "good" if exp > 0 else "bad",
        3.0,
    )
    if s["payoff"] is not None and wins and losses:
        if s["payoff"] < 1 and s["win_rate"] < 0.6:
            say(
                f"Your losers are bigger than your winners ({abs(avg_l):.2f}% vs {avg_w:.2f}%) and you win under 60%: cutting losers sooner, or letting winners run, is where the edge is.",
                "bad",
                2.6,
            )
        elif s["payoff"] >= 1.5:
            say(
                f"Your winners are {s['payoff']:.1f}x your losers: that's what lets a {round(s['win_rate'] * 100)}% hit rate pay.",
                "good",
                1.6,
            )
    hw = [h for c in closed if c["pnl_pct"] > 0 and (h := _hours(c)) is not None]
    hl = [h for c in closed if c["pnl_pct"] <= 0 and (h := _hours(c)) is not None]
    if len(hw) >= 2 and len(hl) >= 2:
        mw, ml = st.median(hw), st.median(hl)
        if ml >= 1.5 * mw and ml - mw >= 1:
            say(
                f"You hold losers longer than winners (median {ml:.1f} h vs {mw:.1f} h): the classic way small losses become big ones.",
                "bad",
                2.4,
            )
        elif mw >= 1.5 * ml and mw - ml >= 1:
            say(
                f"You cut losers fast and give winners time (median {ml:.1f} h vs {mw:.1f} h). Keep doing that.",
                "good",
                1.4,
            )
    # exits against what the alert's move did by the close
    early = []
    for c in closed:
        aid = c.get("alert_id")
        a = engine.storage.get_alert(aid) if aid else None
        oc = ((a or {}).get("edge") or {}).get("outcome") or {}
        row = oc.get(c["symbol"]) or (next(iter(oc.values())) if oc else None)
        if not row or row.get("close") is None:
            continue
        side = 1 if c["direction"] == "long" else -1
        early.append((c["pnl_pct"], side * row["close"]))
    if len(early) >= 3:
        better = [(mine, hold) for mine, hold in early if hold > mine + 0.25]
        if len(better) >= max(2, len(early) / 2):
            gap = st.median([hold - mine for mine, hold in better])
            say(
                f"In {len(better)} of {len(early)} calls taken from alerts, holding to the alert's close would have done better (median {gap:+.2f}% more): your exits may come too early.",
                "bad",
                2.2,
            )
        elif len(better) <= len(early) / 4:
            say(
                f"Your exits beat simply holding to the close in {len(early) - len(better)} of {len(early)} calls taken from alerts.",
                "good",
                1.2,
            )
    # by catalyst of the alert behind the call, by tag, by direction
    groups: dict[str, dict[str, list[float]]] = {"catalyst": {}, "tag": {}, "direction": {}}
    for c in closed:
        aid = c.get("alert_id")
        a = engine.storage.get_alert(aid) if aid else None
        cat = ((a or {}).get("edge") or {}).get("catalyst") or (classify(a, engine.universe) if a else None)
        if cat:
            groups["catalyst"].setdefault(cat["label"], []).append(c["pnl_pct"])
        for t in c.get("tags") or []:
            groups["tag"].setdefault("#" + t, []).append(c["pnl_pct"])
        groups["direction"].setdefault(c["direction"], []).append(c["pnl_pct"])
    out["by"] = {
        k: sorted(
            (
                {
                    "name": n,
                    "n": len(v),
                    "win": round(sum(x > 0 for x in v) / len(v), 2),
                    "avg": round(sum(v) / len(v), 2),
                }
                for n, v in g.items()
            ),
            key=lambda r: -r["avg"],
        )  # fmt: skip
        for k, g in groups.items()
    }
    for kind, label in (("catalyst", "news"), ("tag", "calls")):
        rows = [r for r in out["by"][kind] if r["n"] >= 2]
        if len(rows) >= 2 and rows[0]["avg"] - rows[-1]["avg"] >= 1:
            best, worst = rows[0], rows[-1]
            say(
                f"Your best {label}: {best['name']} ({best['avg']:+.2f}% a call, {best['n']}); your worst: {worst['name']} ({worst['avg']:+.2f}%, {worst['n']}).",
                "info",
                1.8,
            )
    dirs = {r["name"]: r for r in out["by"]["direction"] if r["n"] >= 3}
    if "long" in dirs and "short" in dirs and abs(dirs["long"]["avg"] - dirs["short"]["avg"]) >= 1:
        good, bad = sorted(dirs.values(), key=lambda r: -r["avg"])
        say(
            f"Your {good['name']}s ({good['avg']:+.2f}% a call) do better than your {bad['name']}s ({bad['avg']:+.2f}%).",
            "info",
            1.5,
        )
    with_stop = [c["pnl_pct"] for c in closed if c.get("stop")]
    no_stop = [c["pnl_pct"] for c in closed if not c.get("stop")]
    if len(with_stop) >= 2 and len(no_stop) >= 2:
        worst_w, worst_n = min(with_stop), min(no_stop)
        if worst_n < worst_w - 1:
            say(
                f"Calls without a stop lost up to {worst_n:.2f}%; with one, the worst was {worst_w:+.2f}%. Set the stop when you take the call.",
                "bad",
                2.0,
            )
    # time of day of the entry
    opening = [
        c["pnl_pct"]
        for c in closed
        if c.get("ts") and _minute(c["ts"]) < 10 * 60 + 30 and _minute(c["ts"]) >= 9 * 60 + 30
    ]
    later = [c["pnl_pct"] for c in closed if c.get("ts") and _minute(c["ts"]) >= 10 * 60 + 30]
    if len(opening) >= 3 and len(later) >= 3:
        a1, a2 = sum(opening) / len(opening), sum(later) / len(later)
        if abs(a1 - a2) >= 1:
            say(f"Calls taken in the first hour average {a1:+.2f}%; later ones {a2:+.2f}%.", "info", 1.3)
    out["insights"] = sorted(ins, key=lambda x: -x["weight"])
    return out


def _minute(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


# --------------------------------------------------------------------------- today's setups


def todays_setups(
    engine: Engine, device: str = "", now: float | None = None, limit: int = 5
) -> dict[str, Any]:
    """The best alerts since the last close, as plans: a kind of news with a record (went its way
    55%+ of the time by the close, median move its way positive), a live plan with reward-to-risk
    1.2 or better, one per stock. Ranked by reward-to-risk x hit rate x confidence in the record
    (n / (n + 10)), the phone's own stocks a step ahead. History and prices, not advice."""
    from .catalysts import catalyst_base, classify
    from .edge import Calendar

    now = now or time.time()
    since = Calendar.last_close(now, getattr(engine.calendar, "holidays", set()))
    rates = getattr(engine, "catalyst_rates", {}) or {}
    p = engine.desk.profiles.get(device) or {}
    mine = set(p.get("watchlist") or []) | set(p.get("holdings") or {})
    best: dict[str, dict[str, Any]] = {}
    for a in engine.storage.recent_alerts(400, since=since):
        if a.get("kind") == "system" or a.get("severity") not in ("MEDIUM", "HIGH", "CRITICAL"):
            continue
        e = a.get("edge") or {}
        cb = e.get("cat_base") or catalyst_base(rates, e.get("catalyst") or classify(a, engine.universe))
        close = (cb or {}).get("close") or {}
        if not close or close["hit"] < 0.55 or close["median"] <= 0:
            continue
        plan = trade_plan(engine, device, a, now)
        if not plan.get("ok") or plan["rr"] < 1.2:
            continue
        sym = plan["symbol"]
        n = close["n"]
        odds = e.get("odds") or {}
        hit = odds.get("p") if odds.get("p") is not None else close["hit"]  # the model's read, when in use
        score = plan["rr"] * hit * n / (n + 10) * (1.3 if sym in mine else 1.0)
        if sym in best and best[sym]["score"] >= score:
            continue
        best[sym] = {"id": a["id"], "title": a.get("title", ""), "created": a.get("created"), "severity": a.get("severity"),
                     "symbol": sym, "mine": sym in mine, "catalyst": cb["label"], "band": cb["band_label"],
                     "record": close, "plan": plan, "score": round(score, 3), "odds": odds or None}  # fmt: skip
    rows = sorted(best.values(), key=lambda r: -r["score"])[:limit]
    return {"rows": rows, "since": since}


def setups_push(s: dict[str, Any]) -> dict[str, Any] | None:
    rows = s.get("rows") or []
    if not rows:
        return None
    lines = []
    for r in rows[:4]:
        pl = r["plan"]
        side = "long" if pl["direction"] == "up" else "short"
        o = r.get("odds") or {}
        odds = f" · odds {round(o['p'] * 100)}%" if o.get("p") is not None else ""
        if odds and o.get("ev") is not None:
            odds += f", exp {o['ev']:+.1f}%"
        lines.append(f"{'★' if r['mine'] else '•'} {r['symbol']} {side} · R:R {pl['rr']:.1f} · stop ${pl['stop']:,.2f} · "
                     f"{r['catalyst'].lower()} {round(r['record']['hit'] * 100)}% ({r['record']['n']}){odds}")  # fmt: skip
    return {
        "title": f"🎯 {len(rows)} setup{'s' if len(rows) > 1 else ''} for today",
        "body": "\n".join(lines)[:600],
    }


# --------------------------------------------------------------------------- event risk


def event_risk(engine: Engine, device: str, now: float | None = None, days: int = 10) -> dict[str, Any]:
    """Each holding's next report or binary event in the next ``days``, in dollars on the position:
    what options price for it (the expiry spanning the report) and what its last reports moved,
    times the position's value. Plus the total at stake. Only what is known: a holding with no
    price, or a report with no option or history read yet, says so."""
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    holdings = {s: h for s, h in (p.get("holdings") or {}).items() if (h.get("shares") or 0) > 0}
    today = datetime.fromtimestamp(now, ET).date()
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    events: list[dict[str, Any]] = []
    for sym, h in holdings.items():
        q = engine.price_of(sym, 86400, now=now)
        value = round(h["shares"] * q[1], 2) if q else None
        base = {"symbol": sym, "shares": h["shares"], "price": q[1] if q else None, "value": value}
        e = book.lookup(sym, today) if book is not None else None
        if e and 0 <= (e.get("in_days") if e.get("in_days") is not None else 99) <= days:
            cached = (getattr(engine, "implied_cache", {}) or {}).get(sym)
            rows = (cached[1] if cached else {}).get("rows") or []
            im = next((r for r in rows if r.get("earnings")), None) or (rows[0] if rows else None)
            hist = (getattr(engine, "earnings_cache", {}) or {}).get(sym)
            usual = (hist[1] if hist else {}).get("avg_abs_move")
            ev = {**base, "kind": "earnings", "title": f"{sym} reports", "date": e["date"], "when": e.get("when") or "",
                  "in_days": e["in_days"], "implied_pct": im["move_pct"] if im else None, "usual_pct": usual}  # fmt: skip
            events.append(ev)
        for c in engine.storage.catalysts(today.isoformat()):
            if c.get("symbol") != sym:
                continue
            in_days = (datetime.fromisoformat(c["date"]).date() - today).days
            if 0 <= in_days <= days:
                events.append({**base, "kind": c.get("event") or "binary", "title": c.get("title") or f"{sym} event",
                               "date": c["date"], "when": c.get("time") or "", "in_days": in_days,
                               "implied_pct": None, "usual_pct": None})  # fmt: skip
    for ev in events:
        v = ev["value"]
        ev["implied_usd"] = round(v * ev["implied_pct"] / 100, 2) if v and ev["implied_pct"] else None
        ev["usual_usd"] = round(v * ev["usual_pct"] / 100, 2) if v and ev["usual_pct"] else None
    events.sort(key=lambda e: (e["date"], e["symbol"]))
    total_value = sum(
        (engine.price_of(s, 86400, now=now) or (0, 0))[1] * h["shares"] for s, h in holdings.items()
    )
    exposed = sum(e["value"] or 0 for e in {e["symbol"]: e for e in events}.values())
    return {
        "events": events,
        "days": days,
        "implied_usd": round(sum(e["implied_usd"] or 0 for e in events), 2),
        "usual_usd": round(sum(e["usual_usd"] or 0 for e in events), 2),
        "exposed_value": round(exposed, 2),
        "exposed_pct": round(exposed / total_value * 100, 1) if total_value else None,
        "holdings": len(holdings),
    }
