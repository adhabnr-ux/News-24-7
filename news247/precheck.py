"""Before you track it: one verdict line per thing that should give a trader pause.

Tapping Track on an alert or a stock opens a paper call. Just before it does, this reads everything
the desk already knows about that call in one place: the odds and expected move, how its kind of
news has done (on days like today too), how the firm's calls have done when an analyst made it,
whether the stock has already made its typical move since the alert, whether the market is
fading it, the plan's reward-to-risk, a report before the exit, how much of the desk is already on
the same side of the same sector, and what the playbook would do with it (pass, wait for its way
in, or take it its way). Each line is good, a warning or a red flag, with
the number behind it, and the verdict sums them up. It never blocks the call: paper only, and
the choice is yours.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
SAME_SIDE_WARN = 2  # other positions on the same side of the same sector before it's a warning
SAME_SIDE_BAD = 4


def _tone(v: float, good: float, bad: float) -> str:
    return "good" if v >= good else "bad" if v < bad else "warn"


def pre_trade(
    engine: Engine, device: str, symbol: str, direction: str, alert_id: str = "", now: float | None = None
) -> dict[str, Any]:
    """{"symbol", "direction", "verdict": "go" | "caution" | "think twice", "checks": [{key, tone, text}]}"""
    from .dossier import _since
    from .plans import trade_plan

    now = now or time.time()
    sym = symbol.upper()
    long = direction != "short"
    side = "long" if long else "short"
    checks: list[dict[str, str]] = []

    def add(key: str, tone: str, text: str) -> None:
        checks.append({"key": key, "tone": tone, "text": text})

    a = engine.storage.get_alert(alert_id) if alert_id else None
    e = (a or {}).get("edge") or {}
    if a is not None:
        o = e.get("odds") or {}
        if o.get("p") is not None:
            ev = f", expected {o['ev']:+.1f}%" if o.get("ev") is not None else ""
            tone = _tone(o["p"], 0.6, 0.5)
            if o.get("ev") is not None and o["ev"] < 0:
                tone = "bad"
            add("odds", tone, f"Odds {round(o['p'] * 100)}% by the close{ev} (the model, {o['n']} graded)")
        from .catalysts import catalyst_base, classify

        rates = getattr(engine, "catalyst_rates", None) or {}
        cb = (
            e.get("cat_base") or catalyst_base(rates, e.get("catalyst") or classify(a, engine.universe)) or {}
        )
        if cb.get("close"):
            c = cb["close"]
            add("record", _tone(c["hit"], 0.6, 0.5),
                f"{cb['label']}, {cb['band_label']}: {round(c['hit'] * 100)}% went their way by the close (median {c['median']:+.1f}%, {c['n']})")  # fmt: skip
            x = cb.get("x_close")
            if x and x["n"] >= 6:
                add("market", "good" if x["median"] > 0 else "warn",
                    f"Against the market, that kind of news {'beat' if x['median'] > 0 else 'trailed'} SPY by {abs(x['median']):.1f}% (median, {x['n']})")  # fmt: skip
        from .macrodays import macro_line

        mc = e.get("macro")
        if mc:
            add("macro", "warn" if mc["macro"]["hit"] <= 0.45 else "info", macro_line(mc))
        from .climate import climate_line

        cl = e.get("climate")
        if cl:
            add("climate", "good" if cl["state"] == "hot" else "warn", climate_line(cl))
        rb = e.get("regime_base")
        if rb:
            add(
                "regime",
                _tone(rb["hit"], 0.6, 0.5),
                f"On {rb['regime']} days like today: {round(rb['hit'] * 100)}% went their way ({rb['n']})",
            )
        fb = e.get("firm_base")
        if fb:
            add("firm", _tone(fb["close"]["hit"], 0.6, 0.5),
                f"{fb['label']} here: {round(fb['close']['hit'] * 100)}% went their way ({fb['close']['n']})")  # fmt: skip
        since = _since(a, sym, engine.price_of)
        typical = (cb.get("close") or {}).get("median") if cb.get("close") else None
        if since is not None:
            its_way = since if long else -since
            if typical and typical > 0:
                share = its_way / typical
                tone = "bad" if share >= 1 else "warn" if share >= 0.6 or its_way <= -typical else "good"
                add("moved", tone, f"{its_way:+.1f}% its way since the alert, of a typical {typical:+.1f}%"
                    + (": the usual move is done" if share >= 1 else ""))  # fmt: skip
            else:
                add(
                    "moved",
                    "warn" if abs(its_way) >= 3 else "info",
                    f"{its_way:+.1f}% its way since the alert",
                )
        from .liveodds import live_odds

        lo = live_odds(engine, a, now)
        if lo:
            add("live", _tone(lo["p"], 0.6, 0.4),
                f"{lo['hours']:g} hours in, {lo['since']:+.1f}% its way: alerts like this finished their way {round(lo['p'] * 100)}% ({lo['n']})")  # fmt: skip
        from .newsclock import early, early_line

        ea = early(engine, a, now)  # under an hour in: how alerts that stood the same then finished
        if ea:
            tone = (
                "good"
                if ea["hit"] >= 0.6 and ea["median"] > 0
                else "warn"
                if ea["hit"] < 0.45 or ea["median"] < 0
                else "info"
            )
            add("early", tone, early_line(ea))
        from .opening import premarket

        if datetime.fromtimestamp(now, ET).hour < 10:  # before the open and in its first half hour
            row = next((r for r in premarket(engine, now)["rows"] if r["alert_id"] == alert_id), None)
            if row and row["record"]:
                aft = row["record"]["after"]
                add("open", "good" if aft["hit"] >= 0.55 and aft["median"] > 0 else "warn" if aft["hit"] <= 0.45 else "info",
                    f"{row['gap']:+.1f}% its way since the news: {row['line'][0].lower()}{row['line'][1:]}")  # fmt: skip
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        try:
            dv = [
                x
                for x in json.loads(engine.storage.get_setting("diverge_log") or "[]")
                if x.get("alert_id") == alert_id and x.get("day") == day
            ]
        except ValueError:
            dv = []
        if dv:
            add(
                "diverge",
                "bad",
                f"The market isn't buying this news: {dv[0]['against']:.1f}% the other way when it was flagged",
            )
        plan = trade_plan(engine, device, a, now)
        if plan.get("ok"):
            add("plan", _tone(plan["rr"], 1.5, 1.0),
                f"Plan from {plan['entry']:,.2f}: stop {plan['stop']:,.2f}, target {plan['target']:,.2f}, reward-to-risk {plan['rr']:.1f}")  # fmt: skip
            pt = plan.get("patient")
            if pt and pt.get("p_filled") is not None:
                add("patient", "info",
                    f"A limit at {pt['limit']:,.2f} (the winners' median dip) fills {round(pt['fills_winners'] * 100)}% of winners "
                    f"and {round(pt['fills_losers'] * 100)}% of losers: if filled, {round(pt['p_filled'] * 100)}% won vs "
                    f"{round(pt['p_now'] * 100)}% now, at {pt['rr']:.1f} : 1")  # fmt: skip
            es = plan.get("edge_size")
            if es:
                sh = f" ({es['shares']:,} sh)" if es.get("shares") else ""
                add("size", "bad" if es["risk_pct"] <= 0 else "good" if not es["capped"] or es["risk_pct"] >= 0.5 else "warn",
                    es["why"] if es["risk_pct"] <= 0 else f"Size by edge: risk {es['risk_pct']:g}% of the account{sh}, {es['why']}")  # fmt: skip
        elif plan.get("status") in ("done", "invalidated"):
            add("plan", "bad", plan.get("why") or "The plan is off.")
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    er = book.lookup(sym, datetime.fromtimestamp(now, ET).date()) if book is not None else None
    if er and er.get("in_days") is not None and er["in_days"] <= 1:
        when = (" " + er["when"]) if er.get("when") else ""
        add("report", "bad" if er["in_days"] == 0 else "warn",
            f"{sym} reports{when} {'today' if er['in_days'] == 0 else 'tomorrow'}: a gap can go straight through a stop")  # fmt: skip
    from .persona import TONE, TRAITS, persona_line
    from .persona import stamp as persona_stamp

    ps = persona_stamp(getattr(engine, "persona_rates", None) or {}, sym)
    if ps and ps["trait"] not in ("mixed", "runs"):  # "runs with its news" is the record check again
        from .dossier import _direction

        # with the news: the trait reads as it is (a small sample, so never worse than a caution);
        # against it, or without an alert, it's context
        with_news = a is not None and _direction(a) == ("up" if long else "down")
        tone = TONE[ps["trait"]] if with_news else "info"
        add("persona", "warn" if tone == "bad" else tone, f"{persona_line(ps)}: {TRAITS[ps['trait']][1]}")
    if a is not None:  # what the playbook would do with this alert (autopilot.py, combos.py)
        _playbook(a, long, add)
    _concentration(engine, device, sym, long, add)
    from .risk import desk_risk

    rk = desk_risk(engine, device, now)
    if rk["account"] and rk["total_pct"] is not None:
        new = round(rk["total_pct"] + rk["per_idea"], 1)
        cap = rk["cap"]
        add("book", "bad" if cap and new > cap else "info",
            f"Open risk {rk['total_pct']:.1f}% of the account → {new:.1f}% with this one" + (f" (your cap {cap:g}%)" if cap else ""))  # fmt: skip
    bad = sum(c["tone"] == "bad" for c in checks)
    warn = sum(c["tone"] == "warn" for c in checks)
    verdict = "think twice" if bad else "caution" if warn >= 2 else "go"
    return {"symbol": sym, "direction": side, "verdict": verdict, "checks": checks,
            "good": sum(c["tone"] == "good" for c in checks), "warn": warn, "bad": bad}  # fmt: skip


def _playbook(a: dict[str, Any], long: bool, add: Any) -> None:
    """The playbook's view: it passes (and why), it waits for its way in, or it takes it its way."""
    from .autopilot import playbook_in, playbook_out, playbook_pass
    from .combos import DEFAULT, NAMES
    from .dossier import _direction

    cr = (a.get("edge") or {}).get("combo_rec") or {}
    why = playbook_pass(a)
    if why:
        add("playbook", "bad", f"The playbook passes on this one: {why}")
        return
    if not cr.get("best"):
        return
    if _direction(a) != ("up" if long else "down"):
        add("playbook", "warn", "Against its news: the playbook only trades news its own way")
        return
    pin, pout = playbook_in(a), playbook_out(a)
    pair = f"{pin}|{pout}"
    styles = cr.get("styles") or {}
    rec = styles.get(cr["best"]) or {}
    plain = (styles.get(DEFAULT) or {}).get("avg")
    name = NAMES.get(pair, pair).lower()
    avg = f" ({rec['avg']:+.1f}% on average, {rec.get('n', '?')})" if rec.get("avg") is not None else ""
    if pin != "alert":
        add(
            "playbook",
            "warn",
            f"The playbook waits: {name}{avg}"
            + (f"; in now and out at the close made {plain:+.1f}%" if plain is not None else ""),
        )
    elif pout != "close":
        add("playbook", "good", f"The playbook takes it, {name}{avg}: mind the exit")
    else:
        add("playbook", "good", f"The playbook takes it the plain way{avg}")


def _concentration(engine: Engine, device: str, sym: str, long: bool, add: Any) -> None:
    """Already on the same side of the same sector: holdings count as long, open calls by side."""
    p = engine.desk.profiles.get(device) or {}
    li = engine.universe.get(sym)
    sector = (li.sector if li else "") or ""
    side = "long" if long else "short"
    same_stock = [t for t in p.get("tracked") or [] if t["symbol"] == sym and (t.get("direction") == side)]
    if same_stock:
        add("dup", "warn", f"You already track {sym} {side} ({len(same_stock)} open)")
    if not sector:
        return
    names = set()
    for t in p.get("tracked") or []:
        other = engine.universe.get(t["symbol"])
        if t["symbol"] != sym and other is not None and other.sector == sector and t.get("direction") == side:
            names.add(t["symbol"])
    if long:
        for s_, h in (p.get("holdings") or {}).items():
            other = engine.universe.get(s_)
            if s_ != sym and other is not None and other.sector == sector and (h.get("shares") or 0) > 0:
                names.add(s_)
    if len(names) >= SAME_SIDE_WARN:
        tone = "bad" if len(names) >= SAME_SIDE_BAD else "warn"
        add(
            "sector",
            tone,
            f"You're already {side} {len(names)} other {sector} stocks ({', '.join(sorted(names)[:5])}): one sector move hits them all",
        )
    elif names:
        add("sector", "info", f"You're also {side} {', '.join(sorted(names))} in {sector}")
