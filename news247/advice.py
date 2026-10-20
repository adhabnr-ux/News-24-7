"""The desk's advice: the setting changes the records back, ranked, each one tap from applied.

Many panels each say something about a setting: tune my pushes finds a better edge bar, what your
settings cost finds one holding back winners, the alert line's report finds the line too high,
the call's record shows whether following it pays. Each is read in its own corner. The advice
reads all of them for one phone and lists the changes worth making, most evidence first, each with
the numbers behind it and the one tap that makes it:

- **Switch off a kind of news that keeps losing here** (30+ graded by the close at 40% or less,
  with a negative median), and **back on** a switched-off kind whose record recovered (55%+
  on 20+).
- **Use the tuned push setting** (tune.py's best pick, when it beats the current one).
- **Fix a setting that costs you** (keptback.py: one holding back alerts that went their way).
- **Lower the alert line** (shadow.py, when the headlines just under it did as well), or **move one
  kind of news' line** (linekinds.py: lower it where near misses ran, raise it where alerts lost).
- **Follow the call on paper** (when "act now" went its way 60%+ on 30+ replayed alerts and no
  autopilot follows it yet).

- **Undo a change that backfired** (changes.py: a kind switched off whose alerts kept winning, a
  line move whose added alerts lost or whose kept-back headlines won).

Advice is evidence, not an order: every item shows its counts, and nothing changes until tapped.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

OFF_N, OFF_HIT = 30, 0.40
BACK_N, BACK_HIT = 20, 0.55


def _item(key: str, title: str, why: str, weight: float, *, action: str = "", body: dict[str, Any] | None = None,
          api: str = "", label: str = "Apply") -> dict[str, Any]:  # fmt: skip
    return {"key": key, "title": title, "why": why, "weight": round(weight, 1), "action": action, "body": body or {},
            "api": api, "label": label}  # fmt: skip


def advise(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    from .catalysts import LABELS

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    items: list[dict[str, Any]] = []
    cat = getattr(engine, "catalyst_rates", None) or {}
    off = set(p.get("cat_off") or [])
    for key, label in LABELS.items():
        c = (cat.get(f"{key}|*") or {}).get("close")
        if not c:
            continue
        if key not in off and c["n"] >= OFF_N and c["hit"] <= OFF_HIT and c["median"] < 0:
            items.append(_item(f"off:{key}", f"Switch off {label.lower()} news",
                               f"It went its way {round(c['hit'] * 100)}% by the close here (median {c['median']:+.1f}%, {c['n']} graded).",
                               c["n"] * (0.5 - c["hit"]), action="cat_mute", body={"key": key, "on": True}, label="Switch off"))  # fmt: skip
        elif key in off and c["n"] >= BACK_N and c["hit"] >= BACK_HIT and c["median"] > 0:
            items.append(_item(f"on:{key}", f"Switch {label.lower()} news back on",
                               f"Its record recovered: {round(c['hit'] * 100)}% by the close (median {c['median']:+.1f}%, {c['n']} graded).",
                               c["n"] * (c["hit"] - 0.5), action="cat_mute", body={"key": key, "on": False}, label="Switch on"))  # fmt: skip
    if p:
        from .tune import tune_cached

        with_tune = tune_cached(engine, device, now)
        pick = with_tune.get("balanced") or with_tune.get("quality")
        cur = with_tune.get("current") or {}
        if pick and pick.get("vs_avg") is not None and pick["vs_avg"] >= 0.2:
            items.append(_item("tune", f"Push with {pick['label']}",
                               f"The last month replayed: {pick['avg']:+.2f}% a push on average ({pick['n']}, {pick['per_day']} a day) vs "
                               f"{cur.get('avg', 0):+.2f}% with your setting now.", pick["n"] * pick["vs_avg"],
                               action="prefs", body={"edge_min": pick["edge_min"], "budget": pick["budget"]}))  # fmt: skip
        from .keptback import settings_cost

        for g in settings_cost(engine, device, 14, now).get("gates") or []:
            if g.get("verdict") == "costing" and (g.get("fix") or {}).get("action"):
                items.append(_item(f"cost:{g['code']}", g["fix"]["label"],
                                   f"{g['setting']} kept back {g['n']} alerts that went their way {round(g['hit'] * 100)}% "
                                   f"(average {g['avg']:+.1f}%) in two weeks.", g["n"] * max(0.1, g["hit"] - 0.5),
                                   action=g["fix"]["action"], body=g["fix"].get("body") or {}))  # fmt: skip
    from .shadow import line_report

    lr = line_report(engine, 60, now)
    if lr.get("verdict") == "lower":
        top = lr["bands"][0]
        items.append(_item("line", f"Lower the alert line to {top['from']:g}", lr["line_text"], top["n"] * 0.5,
                           api="/api/line", body={"medium": top["from"], "why": lr["line_text"]}, label="Lower it"))  # fmt: skip
    from .linekinds import kind_report

    for r in kind_report(engine, 60, now):
        if r["verdict"] in ("lower", "raise"):
            side = r["under"] if r["verdict"] == "lower" else r["over"]
            items.append(_item(f"kline:{r['kind']}:{r['to']:g}", f"{'Lower' if r['verdict'] == 'lower' else 'Raise'} the line for {r['label'].lower()} news to {r['to']:g}",
                               r["text"], side["n"] * 0.4, api="/api/line/kind",
                               body={"kind": r["kind"], "offset": r["to"] - engine.cfg.scoring.medium, "why": r["text"]},
                               label="Lower it" if r["verdict"] == "lower" else "Raise it"))  # fmt: skip
    from .changes import kind_offs, line_moves

    for c in (kind_offs(engine, device, now) if p else []) + line_moves(engine, now):
        u, s = c.get("undo"), c.get("since") or {}
        if u:
            why = (f"Since then, {s['n']} graded went their way {round(s['hit'] * 100)}% by the close (median {s['median']:+.1f}%): the change backfired.")  # fmt: skip
            items.append(_item(f"undo:{c['what']}:{c['at']:.0f}", f"Undo: {c['what'][:1].lower() + c['what'][1:]}", why,
                               s["n"] * 0.6, action=u.get("action", ""), body=u.get("body"), api=u.get("api", ""),
                               label=u["label"]))  # fmt: skip
    rp = getattr(engine, "call_replay", None) or {}
    act = next((v for v in rp.get("verbs") or [] if v["verb"] == "act"), None)
    follows = any(r.get("follow") == "call" for r in p.get("autopilots") or [])
    if p and act and act["n"] >= 30 and act["hit"] >= 0.6 and not follows:
        items.append(_item("follow", "Follow the call on paper",
                           f"Replayed, its \"act now\" calls went their way {round(act['hit'] * 100)}% by the close "
                           f"(median {act['median']:+.1f}%, {act['n']}).", act["n"] * (act["hit"] - 0.5),
                           action="autopilot_add", body={"follow": "call", "catalyst": "all", "band": "all", "min_sev": "HIGH", "exit": "close"},
                           label="Follow it"))  # fmt: skip
    if p:  # a signal that's weak here, still pushed; a strong one switched off (signals.py)
        from .signals import advice_items

        items += [{**x, "api": "", "weight": round(x["weight"], 1)} for x in advice_items(engine, device)]
    items.sort(key=lambda i: -i["weight"])
    dismissed = set((p.get("advice_done") or {}).keys())
    shown = [i for i in items if i["key"] not in dismissed]
    return {"items": shown[:8], "hidden": len(items) - len(shown)}
