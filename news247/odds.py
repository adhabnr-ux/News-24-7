"""Calibrated odds: one honest number per alert, learned from this server's own graded history.

Every graded alert (did it go its way by the close?) is a lesson. This fits a small logistic model
on what was known when each alert went out (its kind, severity, kind of news and company size,
direction, how far the stock had already moved, the time of day, the market regime of its day,
whether other evidence lined up) and says, for a new alert, how often alerts like it went their
way: "71% by the close".

It has to earn its place. The newest quarter of the history is held out: the model is fitted on
the older alerts and scored on those it never saw, against simply guessing the overall hit rate
(the Brier score, the mean squared miss of a probability). Only when it beats that guess does a
new alert carry its odds; the track record shows the test either way, with a reliability table
(of alerts it gave 60-70%, how many went their way?). Then it is refitted on everything for use.

Each alert's odds come with the few factors that moved them most, in points, so the number can
be read rather than trusted. History, not a forecast.
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_TRAIN = 120  # graded alerts before a model is fitted at all
MIN_TEST = 30  # held-out alerts it is scored on
HOLDOUT = 0.25  # the newest quarter
EPOCHS = 40
L2 = 0.002
BUCKETS = ((0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01))
SEV_X = {"MEDIUM": 0.0, "HIGH": 0.5, "CRITICAL": 1.0}

LABELS = {
    "kind": "{v} alert",
    "sev": "severity",
    "cat": "{v} news",
    "cat:none": "no kind of news read",
    "band": "{v} company",
    "dir": "{v} call",
    "pre": "how far it had already moved its way",
    "pre:none": "no pre-alert tape",
    "hour": "{v}",
    "regime": "{v} day",
    "conf": "other evidence lined up",
    "small": "small cap",
}
HOURS = (
    (0, 9.5, "before the open"),
    (9.5, 10.5, "the first hour"),
    (10.5, 15, "midday"),
    (15, 16, "the last hour"),
    (16, 24, "after the close"),
)


def _sigmoid(z: float) -> float:
    if z < -30:
        return 0.0
    if z > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _direction(a: dict[str, Any]) -> str:
    from .dossier import _direction as d

    return d(a) or ""


def features(a: dict[str, Any], regime: str | None, universe: Any = None) -> dict[str, float]:
    """What was known when the alert went out, as model inputs (name -> value)."""
    from .catalysts import classify

    edge = a.get("edge") or {}
    x: dict[str, float] = {f"kind={a.get('kind') or 'news'}": 1.0}
    x["sev"] = SEV_X.get(str(a.get("severity")), 0.0)
    c = edge.get("catalyst") or classify(a, universe)
    if c:
        x[f"cat={c['key']}"] = 1.0
        if c.get("band"):
            x[f"band={c['band']}"] = 1.0
    else:
        x["cat:none"] = 1.0
    d = _direction(a)
    x[f"dir={d or 'none'}"] = 1.0
    sym = next(iter(edge.get("outcome") or {}), None) or ((a.get("tickers") or [""])[0])
    pre = (edge.get("pre") or {}).get(sym)
    if pre is not None and d:
        its_way = (1 if d == "up" else -1) * float(pre)
        x["pre"] = max(-3.0, min(3.0, its_way / 2.0))
    else:
        x["pre:none"] = 1.0
    h = datetime.fromtimestamp(float(a.get("created") or 0), ET)
    hh = h.hour + h.minute / 60
    x[f"hour={next(lbl for lo, hi, lbl in HOURS if lo <= hh < hi)}"] = 1.0
    if regime in ("risk-on", "risk-off", "mixed"):
        x[f"regime={regime}"] = 1.0
    if edge.get("confluence"):
        x["conf"] = 1.0
    if edge.get("smallcap"):
        x["small"] = 1.0
    return x


def _label(name: str) -> str:
    if "=" in name:
        k, v = name.split("=", 1)
        if k == "cat":
            from .catalysts import LABELS as CAT

            v = CAT.get(v, v).lower()
        elif k == "band":
            from .catalysts import BAND_LABEL

            v = BAND_LABEL.get(v, v)
        elif k == "kind":
            v = {"news": "a headline", "options": "an options", "radar": "a radar", "insider": "an insider",
                 "confluence": "a confluence", "macro": "a macro"}.get(v, v)  # fmt: skip
        elif k == "dir":
            v = {"up": "a long", "down": "a short"}.get(v, "a no-direction")
        text = LABELS.get(k, "{v}").format(v=v)
        return text[0].upper() + text[1:]
    text = LABELS.get(name, name)
    return text[0].upper() + text[1:]


def _rows(
    engine: Engine, days: float, now: float, live: list[tuple[float, int]] | None = None
) -> list[tuple[float, dict[str, float], int]]:
    """(created, features, went its way?) for every graded alert, oldest first. ``live`` collects
    (the odds it went out with, went its way?) for the alerts that carried odds: the forward test."""
    regimes: dict[str, str | None] = {}
    out = []
    for a in engine.storage.recent_alerts(6000, since=now - days * 86400):
        if a.get("kind") == "system":
            continue
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign") or row.get("close") is None:
            continue
        day = datetime.fromtimestamp(a["created"], ET).date().isoformat()
        if day not in regimes:
            raw = engine.storage.get_setting(f"memory:{day}")
            regimes[day] = json.loads(raw).get("regime") if raw else None
        y = int(row["sign"] * row["close"] > 0)
        out.append((a["created"], features(a, regimes[day], engine.universe), y))
        p = ((a.get("edge") or {}).get("odds") or {}).get("p")
        if live is not None and p is not None:
            live.append((float(p), y))
    out.sort(key=lambda r: r[0])
    return out


def _fit(rows: list[tuple[float, dict[str, float], int]], seed: int = 7) -> dict[str, Any]:
    """L2-regularized logistic regression by stochastic gradient descent (a few thousand rows of
    ~10 active inputs each: well under a second)."""
    w: dict[str, float] = {}
    base = sum(r[2] for r in rows) / len(rows)
    b = math.log(max(base, 1e-3) / max(1 - base, 1e-3))
    order = list(range(len(rows)))
    rng = random.Random(seed)
    for ep in range(EPOCHS):
        rng.shuffle(order)
        lr = 0.08 / (1 + ep * 0.15)
        for i in order:
            _, x, y = rows[i]
            z = b + sum(w.get(k, 0.0) * v for k, v in x.items())
            g = _sigmoid(z) - y
            b -= lr * g
            for k, v in x.items():
                wk = w.get(k, 0.0)
                w[k] = wk - lr * (g * v + L2 * wk)
    return {"b": b, "w": {k: round(v, 5) for k, v in w.items() if abs(v) > 1e-6}, "base": base}


def _p(model: dict[str, Any], x: dict[str, float]) -> float:
    return _sigmoid(model["b"] + sum(model["w"].get(k, 0.0) * v for k, v in x.items()))


def _auc(ps: list[float], ys: list[int]) -> float | None:
    pos = [p for p, y in zip(ps, ys, strict=True) if y]
    neg = [p for p, y in zip(ps, ys, strict=True) if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return round(wins / (len(pos) * len(neg)), 3)


def _reliability(ps: list[float], ys: list[int]) -> list[dict[str, Any]]:
    """Of the alerts given 60-70%, how many went their way? One row per band that has any."""
    rel = []
    for lo, hi in BUCKETS:
        got = [(p, y) for p, y in zip(ps, ys, strict=True) if lo <= p < hi]
        if got:
            rel.append({"lo": lo, "hi": min(hi, 1.0), "n": len(got), "predicted": round(sum(p for p, _ in got) / len(got), 3),
                        "actual": round(sum(y for _, y in got) / len(got), 3)})  # fmt: skip
    return rel


def live_record(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    """The forward test: alerts that went out carrying odds, graded since. Nothing here was used to
    fit the model that gave them their odds."""
    if not pairs:
        return {"n": 0}
    ps, ys = [p for p, _ in pairs], [y for _, y in pairs]
    avg = sum(ps) / len(ps)
    return {"n": len(ps), "predicted": round(avg, 3), "actual": round(sum(ys) / len(ys), 3),
            "brier": round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 4),
            "brier_guess": round(sum((avg - y) ** 2 for y in ys) / len(ys), 4),
            "auc": _auc(ps, ys), "reliability": _reliability(ps, ys)}  # fmt: skip


def fit_odds(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """Fit, test on the newest quarter, refit on everything. {"ready", "trusted", "model", "test", ...}."""
    now = now or time.time()
    live: list[tuple[float, int]] = []
    rows = _rows(engine, days, now, live)
    out: dict[str, Any] = {"n": len(rows), "need": MIN_TRAIN, "ready": False, "trusted": False, "fitted": now,
                           "live": live_record(live)}  # fmt: skip
    if len(rows) < MIN_TRAIN:
        return out
    cut = int(len(rows) * (1 - HOLDOUT))
    train, test = rows[:cut], rows[cut:]
    m = _fit(train)
    ps = [_p(m, x) for _, x, _ in test]
    ys = [y for _, _, y in test]
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ys)
    guess = m["base"]
    brier0 = sum((guess - y) ** 2 for y in ys) / len(ys)
    skill = 1 - brier / brier0 if brier0 else 0.0
    rel = _reliability(ps, ys)
    out["test"] = {"n": len(test), "train": len(train), "brier": round(brier, 4), "brier_guess": round(brier0, 4),
                   "skill": round(skill, 3), "auc": _auc(ps, ys), "hit": round(sum(ys) / len(ys), 3),
                   "guess": round(guess, 3), "reliability": rel,
                   "from": test[0][0], "to": test[-1][0]}  # fmt: skip
    full = _fit(rows)
    out["model"] = full
    out["ready"] = True
    out["trusted"] = len(test) >= MIN_TEST and skill > 0
    top = sorted(full["w"].items(), key=lambda kv: -abs(kv[1]))
    out["factors"] = [
        {"name": k, "label": _label(k), "weight": round(v, 3)} for k, v in top[:12] if abs(v) >= 0.05
    ]
    return out


def predict(state: dict[str, Any] | None, a: dict[str, Any], regime: str | None) -> dict[str, Any] | None:
    """A new alert's odds of going its way by the close, with the factors that moved them most (in
    points). None until a model has beaten the plain hit rate on alerts it never saw."""
    if not state or not state.get("trusted") or not _direction(a):
        return None
    m = state["model"]
    x = features(a, regime)
    z = m["b"] + sum(m["w"].get(k, 0.0) * v for k, v in x.items())
    p = _sigmoid(z)
    why = []
    for k, v in x.items():
        c = m["w"].get(k, 0.0) * v
        if abs(c) < 1e-9:
            continue
        pts = (p - _sigmoid(z - c)) * 100
        if abs(pts) >= 1:
            why.append({"label": _label(k), "pts": round(pts)})
    why.sort(key=lambda r: -abs(r["pts"]))
    return {
        "p": round(p, 3),
        "base": round(m["base"], 3),
        "n": state["n"],
        "skill": state["test"]["skill"],
        "why": why[:4],
    }


def odds_line(o: dict[str, Any] | None) -> str:
    """For a push: "Odds 71% by the close (model, 412 graded)"."""
    if not o:
        return ""
    return f"Odds {round(o['p'] * 100)}% by the close (model, {o['n']} graded)"


def report(state: dict[str, Any] | None) -> dict[str, Any]:
    """For the track record: the out-of-sample test and the biggest factors."""
    if not state:
        return {"ready": False, "trusted": False, "n": 0, "need": MIN_TRAIN}
    return {k: v for k, v in state.items() if k != "model"}
