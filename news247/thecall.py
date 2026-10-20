"""The call: what the records add up to for an alert, in one line, before you read the rest.

A push now carries a dozen graded facts: the odds, its kind's record, the stock's personality, how
fast that news plays out, what overnight news did after the bell, how late the alert was to its
story, the regime, the firm, the flow, the insider's shape. Each is useful; together they ask the
reader to do the adding. The call does it, by a short, visible rule, and says which facts decided:

- **✋ Skip:** two or more facts against it (odds under 45%, a negative expected move, a kind that
  went its way 40% or less, a stock that fights its news, a late alert when late alerts lose, a bad
  regime record, a kind running cold lately...), or one against and nothing for it.
- **⏸ Wait for the open:** news from outside the session whose kind kept going after the bell 45%
  of the time or less: the gap tends to fade first.
- **⏸ Wait for a pullback:** a stock whose first-hour moves usually fade by the close.
- **▶ Act now:** fast news (half its move within 15 minutes) with something for it and nothing
  against, or two or more facts for it and nothing against.
- **👀 Watch:** slow news (no rush), or a mixed record.

Every call is stamped on its alert (edge.call) and graded like the alert, so the Desk shows the
call's own record: if "act now" doesn't beat "skip" by the close, the rule is wrong and it shows.
Never a trade: paper only, and history, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

VERBS = {"act": ("▶", "Act now"), "wait": ("⏸", "Wait"), "watch": ("👀", "Watch"), "skip": ("✋", "Skip")}
MIN_N = 8


def _rec(x: dict[str, Any] | None, key: str = "close") -> dict[str, Any] | None:
    r = (x or {}).get(key)
    return r if r and r.get("n", 0) >= MIN_N else None


def the_call(
    e: dict[str, Any], outside: bool = False, muted: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """{"verb", "label", "line", "for", "against"} from an alert's edge, or None when nothing on it
    is graded yet (no call is better than a made-up one)."""
    pro: list[str] = []
    con: list[str] = []
    facts: list[list[str]] = []  # [key, "+" or "-"]: which facts spoke, for the replay's per-fact record

    hushed: list[str] = []  # facts that spoke but are muted: they haven't sorted winners from losers

    def add(side: str, key: str, text: str) -> None:
        if key in muted:
            hushed.append(key)
            return
        (pro if side == "+" else con).append(text)
        facts.append([key, side])

    o = e.get("odds") or {}
    if o.get("p") is not None:
        if o["p"] >= 0.6:
            add("+", "odds", f"odds {round(o['p'] * 100)}%")
        elif o["p"] < 0.45:
            add("-", "odds", f"odds {round(o['p'] * 100)}%")
        if o.get("ev") is not None and o["ev"] < 0:
            add("-", "ev", f"expected {o['ev']:+.1f}%")
    cb = e.get("cat_base") or e.get("base") or {}
    c = _rec(cb)
    if c:
        what = cb.get("label") or "its kind"
        what = what[:1].lower() + what[1:]
        if c["hit"] >= 0.6:
            add("+", "record", f"{what} went its way {round(c['hit'] * 100)}%")
        elif c["hit"] <= 0.4:
            add("-", "record", f"{what} went its way {round(c['hit'] * 100)}%")
    for key, horizon, what in (("firm_base", "close", "the firm's calls"), ("flow_base", "close", "flow like it"),
                               ("insider_base", "d5", "insider buys like it")):  # fmt: skip
        r = _rec(e.get(key), horizon)
        if r and r["hit"] >= 0.6:
            add("+", key.replace("_base", ""), f"{what} {round(r['hit'] * 100)}%")
        elif r and r["hit"] <= 0.4:
            add("-", key.replace("_base", ""), f"{what} {round(r['hit'] * 100)}%")
    fb = e.get("float_base") or {}
    if fb.get("n", 0) >= MIN_N and (fb["hit"] >= 0.6 or fb["hit"] <= 0.4):
        what = fb["label"][:1].lower() + fb["label"][1:]
        add("+" if fb["hit"] >= 0.6 else "-", "float", f"{what} {round(fb['hit'] * 100)}%")
    vb = e.get("rvol_base") or {}
    if vb.get("n", 0) >= MIN_N and (vb["hit"] >= 0.6 or vb["hit"] <= 0.4):
        add(
            "+" if vb["hit"] >= 0.6 else "-",
            "rvol",
            f"{vb['label'][:1].lower() + vb['label'][1:]} {round(vb['hit'] * 100)}%",
        )
    db = _rec(e.get("dilution_base"))
    if db and (db["hit"] >= 0.6 or db["hit"] <= 0.4):
        lbl = e["dilution_base"]["label"]
        add(
            "+" if db["hit"] >= 0.6 else "-",
            "dilution",
            f"{lbl[:1].lower() + lbl[1:]} {round(db['hit'] * 100)}%",
        )
    cl = e.get("climate") or {}
    if cl.get("state") in ("hot", "cold"):
        add(
            "+" if cl["state"] == "hot" else "-",
            "climate",
            f"{cl['state']} lately ({round(cl['recent']['hit'] * 100)}% in two weeks)",
        )
    mc = e.get("macro") or {}
    if (mc.get("macro") or {}).get("n", 0) >= MIN_N and mc["macro"]["hit"] <= 0.4:
        add("-", "macro", f"on macro days {round(mc['macro']['hit'] * 100)}%")
    rb = e.get("regime_base")
    if rb and rb.get("n", 0) >= MIN_N and rb["hit"] <= 0.4:
        add("-", "regime", f"on {rb['regime']} days {round(rb['hit'] * 100)}%")
    ps = e.get("persona") or {}
    if ps.get("trait") == "fights":
        add("-", "persona", f"{ps['symbol']} fights its news")
    elif ps.get("trait") in ("runs", "drifts"):
        add("+", "persona", f"{ps['symbol']} {ps['label'].lower()}")
    lt = e.get("late") or {}
    lrec = _rec(lt.get("rec"))
    if lt.get("min", 0) >= 15 and lrec and lrec["hit"] < 0.5:
        add("-", "late", f"late to the story ({lt['min']:.0f} min)")
    if not pro and not con and not e.get("clock") and not e.get("open_base"):
        return None
    ck = (e.get("clock") or {}).get("half")
    ob = _rec(e.get("open_base") or {}, "after")
    if len(con) >= 2 or (con and not pro):
        verb, why = "skip", con[:2]
    elif outside and ob and ob["hit"] <= 0.45:
        verb, why = (
            "wait",
            [f"for the open: news like it kept going after the bell {round(ob['hit'] * 100)}%"],
        )
    elif ps.get("trait") == "fades_day" and pro:
        verb, why = "wait", [f"for a pullback: {ps['symbol']}'s first-hour moves usually fade"]
    elif ck in ("m5", "m15") and pro and not con:
        verb, why = "act", ["fast news", *pro[:2]]
    elif len(pro) >= 2 and not con:
        verb, why = "act", pro[:3]
    elif ck == "late":
        verb, why = "watch", ["no rush: it builds through the day", *pro[:1]]
    else:
        verb, why = "watch", (pro[:1] + con[:1]) or ["a mixed record"]
    icon, label = VERBS[verb]
    out = {"verb": verb, "label": label, "line": f"{icon} The call: {label.lower()} · {', '.join(why)}",
           "for": pro, "against": con, "facts": facts}  # fmt: skip
    if verb == "wait":
        out["wait_for"] = "open" if why[0].startswith("for the open") else "pullback"
    if hushed:
        out["muted"] = sorted(set(hushed))
    return out


MUTE_N = 20  # alerts on each side before a fact can be muted


def muted_facts(rp: dict[str, Any] | None) -> list[str]:
    """Facts whose "for" alerts did no better than their "against" alerts in the replay, with
    MUTE_N+ on each side: they stop counting in the call until they sort again."""
    out = []
    for f in (rp or {}).get("facts") or []:
        if f.get("sorts") is not None and f["sorts"] <= 0 and min(f["for"]["n"], f["against"]["n"]) >= MUTE_N:
            out.append(f["key"])
    return sorted(out)


def call_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """The call's own record: by verb, how its alerts did an hour later, by the close and the next day."""
    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        call, o = e.get("call") or {}, e.get("outcome") or {}
        if not call.get("verb") or not o:
            continue
        row = next(iter(o.values()))
        if not row.get("sign"):
            continue
        g = pools.setdefault(call["verb"], {"h1": [], "close": [], "d1": []})
        for h in g:
            if row.get(h) is not None:
                g[h].append(row["sign"] * row[h])
    out = {}
    for verb in VERBS:
        g = pools.get(verb)
        if not g:
            continue
        out[verb] = {"verb": verb, "label": VERBS[verb][1], "icon": VERBS[verb][0],
                     **{h: ({"n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)} if xs else None)
                        for h, xs in g.items()}}  # fmt: skip
    return out


FACT_LABEL = {"odds": "The odds", "ev": "A negative expected move", "record": "Its kind's record", "firm": "The firm's record",
              "flow": "The flow's record", "insider": "The insider's shape", "climate": "The climate", "regime": "Days like today",
              "persona": "The stock's personality", "late": "Late to the story", "macro": "Macro days",
              "float": "Float and short interest", "dilution": "Dilution risk",
              "rvol": "Volume at the alert"}  # fmt: skip


def replay(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """The call's rule run on every graded alert, from what its edge carried when it went out (so
    older alerts, sent before some facts existed, are judged on fewer of them): its record by verb,
    and for each fact, how alerts it spoke for and against did against every alert."""
    from .opening import outside

    now = now or time.time()
    holidays = getattr(engine.calendar, "holidays", set())
    verbs: dict[str, list[float]] = {}
    facts: dict[tuple[str, str], list[float]] = {}
    every: list[float] = []
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        o = e.get("outcome") or {}
        if not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if not row.get("sign") or row.get("close") is None:
            continue
        x = row["sign"] * row["close"]
        every.append(x)
        call = the_call(e, outside(float(a.get("created") or 0), holidays))
        if call is None:
            continue
        verbs.setdefault(call["verb"], []).append(x)
        for key, side in {tuple(f) for f in call["facts"]}:
            facts.setdefault((key, side), []).append(x)

    def stats(xs: list[float]) -> dict[str, Any]:
        return {
            "n": len(xs),
            "hit": round(sum(v > 0 for v in xs) / len(xs), 2),
            "median": round(st.median(xs), 2),
        }

    base = stats(every) if every else None
    rows = []
    for key in FACT_LABEL:
        pro, con = facts.get((key, "+")), facts.get((key, "-"))
        if not pro and not con:
            continue
        r = {
            "key": key,
            "label": FACT_LABEL[key],
            "for": stats(pro) if pro else None,
            "against": stats(con) if con else None,
        }
        if r["for"] and r["against"] and min(r["for"]["n"], r["against"]["n"]) >= MIN_N:
            r["sorts"] = round(
                r["for"]["hit"] - r["against"]["hit"], 2
            )  # how well it sorted winners from losers
        rows.append(r)
    rows.sort(key=lambda r: -(r.get("sorts") if r.get("sorts") is not None else -9))
    return {"all": base, "verbs": [{"verb": v, "label": VERBS[v][1], "icon": VERBS[v][0], **stats(verbs[v])} for v in VERBS if v in verbs],
            "facts": rows, "days": days}  # fmt: skip
