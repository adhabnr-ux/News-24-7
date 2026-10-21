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


def _month(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.year * 12 + d.month - 1


def _label(m: int) -> str:
    return f"{m // 12}-{m % 12 + 1:02d}"


def rows(engine: Engine, days: float = 730, now: float | None = None) -> list[dict[str, Any]]:
    """Every graded alert with its pairs: {"id", "created", "month", "seen", "key", "got"}."""
    from .catalysts import classify
    from .combos import DEFAULT, combos, key_of

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
                    "key": key, "got": got})  # fmt: skip
    return out


def _pick(pools: dict[str, list[float]]) -> tuple[str, bool] | None:
    """(the best pair, whether it passes) from {pair: [sum, n]}, or None under 8 alerts."""
    from .combos import DEFAULT

    ok = {p: s / n for p, (s, n) in pools.items() if n >= MIN_N}
    if not ok:
        return None
    best = max(ok, key=lambda p: (ok[p], p == DEFAULT))
    return best, ok[best] <= 0


def replay(rs: list[dict[str, Any]], months_back: int | None) -> dict[str, dict[str, Any]]:
    """{alert id: {"created", "month", "pb", "plain", "pick"}}: each month traded with the picks from
    alerts fully graded before it, looking back ``months_back`` months (None: all of them)."""
    from .combos import DEFAULT

    # sums per (the month its grades were all in, kind, pair): each month's fit is a sum over earlier ones
    agg: dict[int, dict[str, dict[str, list[float]]]] = {}
    for r in rs:
        kinds = agg.setdefault(r["seen"], {})
        for k in ("*", r["key"]):
            pk = kinds.setdefault(k, {})
            for p, x in r["got"].items():
                s = pk.setdefault(p, [0.0, 0])
                s[0] += x
                s[1] += 1
    out: dict[str, dict[str, Any]] = {}
    by_month: dict[int, list[dict[str, Any]]] = {}
    for r in rs:
        by_month.setdefault(r["month"], []).append(r)
    for m, alerts in sorted(by_month.items()):
        lo = m - months_back if months_back else -1
        fit: dict[str, dict[str, list[float]]] = {}
        for sm, kinds in agg.items():
            if lo <= sm < m:
                for k, pairs in kinds.items():
                    fk = fit.setdefault(k, {})
                    for p, (s, n) in pairs.items():
                        f = fk.setdefault(p, [0.0, 0])
                        f[0] += s
                        f[1] += n
        picks = {k: _pick(v) for k, v in fit.items()}
        for r in alerts:
            pk = picks.get(r["key"]) or picks.get("*")
            if not pk:
                continue  # nothing on record yet to pick from
            pair, passes = pk
            out[r["id"]] = {"created": r["created"], "month": m, "pick": pair,
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
    engine: Engine, days: float = 730, now: float | None = None, current: str = DEFAULT_WINDOW
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
    out["line"] = line(out)
    return out


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
    return head + f" Picking from {others}." + tail
