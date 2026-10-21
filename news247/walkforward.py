"""The learning system, walked forward: every month of history traded with only what was known before it.

The playbook's live track record (pbcurve.py) is honest, but it starts when pairs were first stamped
on alerts. This replays all of the graded history the same way the live system learns:

- at the start of each month, each kind of news's pair (a way in and a way out) is picked from the
  alerts **already fully graded** before that month (8+ days old, so even the five-session exit was
  known): the best average on 8+ of them, or a pass when even the best lost;
- that month's alerts are taken the picked way (an entry that never came adds 0%), set beside the
  plain way (in at the alert, out at the close) on the same alerts.

The result is an out-of-sample record of the whole learning system, as far back as the data goes.

**The lookback, learned.** The same replay is run three ways: picking from all the history before
each month, from the last 6 months, and from the last 3. Markets change, and this is how the
playbook learns how fast. The live picker looks back 6 months by default (combos.py). It switches
to another window only when that window beat the current one in both halves of the walk-forward
record, on the alerts both of them covered (100+), and switches back the same way. History, not a
forecast.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 8
SETTLE = 8 * 86400  # every exit, out to five sessions, is graded by then
WINDOWS = {"all": None, "6m": 6, "3m": 3}  # months of graded alerts each pick looks back
DAYS = {"all": 3650, "6m": 180, "3m": 90}  # the same windows, for the live picker (combos.combo_rates)
LABELS = {"all": "all its history", "6m": "the last 6 months", "3m": "the last 3 months"}
DEFAULT_WINDOW = "6m"
MIN_SHARED = 100
MIN_KIND = 30  # walk-forward alerts of a kind before it's trusted with its pair, or not


def _month(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.year * 12 + d.month - 1


def _label(m: int) -> str:
    return f"{m // 12}-{m % 12 + 1:02d}"


def rows(engine: Engine, days: float = 730, now: float | None = None) -> list[dict[str, Any]]:
    """Every graded alert with its pairs: {"id", "created", "month", "seen", "key", "got"}."""
    from .catalysts import classify
    from .combos import DEFAULT, combos, key_of
    from .pbmarket import market_parts

    now = now or time.time()
    out = []
    for a in engine.storage.recent_alerts(20000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        got = combos(a)
        if DEFAULT not in got:
            continue
        e = a.get("edge") or {}
        key = (e.get("combo_rec") or {}).get("kind") or key_of(
            a, e.get("catalyst") or classify(a, engine.universe)
        )
        created = float(a["created"])
        out.append({"id": a["id"], "created": created, "month": _month(created), "seen": _month(created + SETTLE),
                    "key": key, "got": got, "mk": market_parts(a, got)})  # fmt: skip
    return out


def _pick(pools: dict[str, list[float]]) -> tuple[str, bool] | None:
    """(the best pair, whether it passes) from {pair: [sum, n]}, or None under 8 alerts."""
    from .combos import DEFAULT

    ok = {p: s / n for p, (s, n) in pools.items() if n >= MIN_N}
    if not ok:
        return None
    best = max(ok, key=lambda p: (ok[p], p == DEFAULT))
    return best, ok[best] <= 0


def _agg(rs: list[dict[str, Any]], by: str) -> dict[int, dict[str, dict[str, list[float]]]]:
    """Sums per (the month its grades were all in, kind, pair): what each pair made ("raw"), or what it
    made beyond SPY over its own window ("beyond", only where the market was read)."""
    agg: dict[int, dict[str, dict[str, list[float]]]] = {}
    for r in rs:
        kinds = agg.setdefault(r["seen"], {})
        for k in ("*", r["key"]):
            pk = kinds.setdefault(k, {})
            for p, x in r["got"].items():
                if by == "beyond":
                    m = (r.get("mk") or {}).get(p)
                    if m is None:
                        continue
                    x -= m
                s = pk.setdefault(p, [0.0, 0])
                s[0] += x
                s[1] += 1
    return agg


def _fit(agg: dict[int, dict[str, dict[str, list[float]]]], lo: int, m: int) -> dict[str, Any]:
    fit: dict[str, dict[str, list[float]]] = {}
    for sm, kinds in agg.items():
        if lo <= sm < m:
            for k, pairs in kinds.items():
                fk = fit.setdefault(k, {})
                for p, (s, n) in pairs.items():
                    f = fk.setdefault(p, [0.0, 0])
                    f[0] += s
                    f[1] += n
    return {k: _pick(v) for k, v in fit.items()}


def picks_now(
    rs: list[dict[str, Any]], months_back: int | None, by: str, now: float | None = None
) -> dict[str, Any]:
    """{kind: [pair, passes]}: what each picker would pick for this month's alerts."""
    m = _month(now or time.time())
    return {k: list(v) for k, v in _fit(_agg(rs, by), m - months_back if months_back else -1, m).items() if v}


def replay(rs: list[dict[str, Any]], months_back: int | None, by: str = "raw") -> dict[str, dict[str, Any]]:
    """{alert id: {"created", "month", "pb", "plain", "pick"}}: each month traded with the picks from
    alerts fully graded before it, looking back ``months_back`` months (None: all of them), picked by
    what each pair made ("raw") or by what it made beyond the market ("beyond")."""
    from .combos import DEFAULT

    agg = _agg(rs, by)  # each month's fit is a sum over the months before it
    out: dict[str, dict[str, Any]] = {}
    by_month: dict[int, list[dict[str, Any]]] = {}
    for r in rs:
        by_month.setdefault(r["month"], []).append(r)
    for m, alerts in sorted(by_month.items()):
        picks = _fit(agg, m - months_back if months_back else -1, m)
        for r in alerts:
            pk = picks.get(r["key"]) or picks.get("*")
            if not pk:
                continue  # nothing on record yet to pick from
            pair, passes = pk
            out[r["id"]] = {"created": r["created"], "month": m, "pick": pair, "key": r["key"],
                            "pb": 0.0 if passes else r["got"].get(pair, 0.0), "plain": r["got"][DEFAULT]}  # fmt: skip
    return out


def _stats(res: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not res:
        return {"n": 0}
    xs = sorted(res.values(), key=lambda r: r["created"])
    pb, plain = [r["pb"] for r in xs], [r["plain"] for r in xs]
    months: dict[int, list[float]] = {}
    for r in xs:
        mo = months.setdefault(r["month"], [0.0, 0.0, 0])
        mo[0] += r["pb"]
        mo[1] += r["plain"]
        mo[2] += 1
    return {"n": len(xs), "since": _label(xs[0]["month"]), "total": round(sum(pb), 2), "avg": round(sum(pb) / len(pb), 3),
            "plain_total": round(sum(plain), 2), "plain_avg": round(sum(plain) / len(plain), 3),
            "taken": sum(1 for x in pb if x != 0.0),
            "months": [{"month": _label(m), "n": int(v[2]), "playbook": round(v[0], 2), "plain": round(v[1], 2)}
                       for m, v in sorted(months.items())]}  # fmt: skip


def _beats(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]) -> bool:
    """Whether ``a`` beat ``b`` in both halves of the alerts both covered (MIN_SHARED+)."""
    shared = sorted(set(a) & set(b), key=lambda i: a[i]["created"])
    if len(shared) < MIN_SHARED:
        return False
    mid = len(shared) // 2
    halves = (shared[:mid], shared[mid:])
    return all(sum(a[i]["pb"] for i in h) > sum(b[i]["pb"] for i in h) for h in halves)


def walk_forward(
    engine: Engine,
    days: float = 730,
    now: float | None = None,
    current: str = DEFAULT_WINDOW,
    picker: str = "raw",
) -> dict[str, Any]:
    """{"windows": {window: stats}, "pick", "current", "line"}."""
    rs = rows(engine, days, now)
    runs = {w: replay(rs, mb) for w, mb in WINDOWS.items()}
    out: dict[str, Any] = {
        "windows": {w: {**_stats(r), "label": LABELS[w]} for w, r in runs.items()},
        "current": current,
    }
    pick = current
    for w in WINDOWS:
        if (
            w != current
            and _beats(runs[w], runs[current])
            and (pick == current or _beats(runs[w], runs[pick]))
        ):
            pick = w
    out["pick"] = pick
    out["days"] = DAYS[pick]
    out["edge"], out["kinds"] = edge(runs[pick])
    # the beyond-the-market picker, against the raw one on the same lookback: switches on both halves
    beyond = replay(rs, WINDOWS[pick], "beyond")
    out["beyond"] = {**_stats(beyond), "label": "picked by what pairs made beyond SPY"}
    was = picker
    picker = "beyond" if _beats(beyond, runs[pick]) else "raw" if _beats(runs[pick], beyond) else was
    out["picker"], out["picker_was"] = picker, was
    out["beyond_picks"] = picks_now(rs, WINDOWS[pick], "beyond", now) if picker == "beyond" else {}
    out["line"] = line(out)
    return out


def _range(xs: list[float], seed: str) -> dict[str, Any]:
    from .combos import ranged

    lo, hi = ranged(xs, seed)
    return {"n": len(xs), "avg": round(sum(xs) / len(xs), 3), "lo": lo, "hi": hi}


def edge(res: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """How sure the learner's edge over the plain way is (an 80% bootstrap range of the per-alert gap),
    in all and per kind of news, with each kind's verdict: "earned" (its range above zero), "plain"
    (its range below zero: the plain way did better) or "unclear"."""
    from .combos import label_of

    if not res:
        return {"n": 0}, {}
    gaps: dict[str, list[float]] = {}
    for r in sorted(res.values(), key=lambda r: r["created"]):
        gaps.setdefault(r["key"], []).append(r["pb"] - r["plain"])
    every = [x for xs in gaps.values() for x in xs]
    kinds = {}
    for k, xs in gaps.items():
        if len(xs) < MIN_KIND:
            continue
        rg = _range(xs, f"wf|{k}")
        rg["verdict"] = "earned" if rg["lo"] > 0 else "plain" if rg["hi"] < 0 else "unclear"
        kinds[k] = {"kind": k, "label": label_of(k), **rg}
    return _range(every, "wf|*"), kinds


def fallback(wf: dict[str, Any], kind: str | None) -> dict[str, Any] | None:
    """The walk-forward's say on a kind's pair at publish: the plain way instead, when the learner lost
    to it there with its whole range below zero."""
    k = (wf.get("kinds") or {}).get(kind or "")
    if not k or k["verdict"] != "plain":
        return None
    return {"why": (f"walked forward, picking a pair for {k['label'].lower()} news did worse than the plain way "
                    f"({k['avg']:+.2f}% an alert, likely {k['lo']:+.2f}% to {k['hi']:+.2f}%, {k['n']}): the plain way it is")}  # fmt: skip


def line(r: dict[str, Any]) -> str:
    w = r["windows"]
    main = w[r["pick"]]
    if not main.get("n"):
        return ""
    head = (
        f"Walked forward since {main['since']}, on {main['n']} alerts each traded with only what was known before its "
        f"month: the learning system {main['avg']:+.2f}% an alert ({main['total']:+.1f}% in all), the plain way "
        f"{main['plain_avg']:+.2f}% ({main['plain_total']:+.1f}%)."
    )
    others = "; ".join(f"{LABELS[k]} {v['avg']:+.2f}% ({v['n']})" for k, v in w.items() if v.get("n"))
    tail = (
        f" It now looks back over {LABELS[r['pick']]}: that beat {LABELS[r['current']]} in both halves of the record."
        if r["pick"] != r["current"]
        else f" It looks back over {LABELS[r['pick']]}."
    )
    e = r.get("edge") or {}
    sure = ""
    if e.get("n"):
        verdict = (
            "a real edge"
            if e["lo"] > 0
            else "behind the plain way"
            if e["hi"] < 0
            else "not yet clear of noise"
        )
        sure = f" Against the plain way: {e['avg']:+.2f}% an alert, likely {e['lo']:+.2f}% to {e['hi']:+.2f}% ({verdict})."
    return head + sure + f" Picking from {others}." + tail
