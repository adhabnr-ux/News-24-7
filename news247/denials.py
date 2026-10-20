"""Denials and walk-backs: the follow-up that undoes the news.

The most dangerous headline for a news trade is the one after it: "Acme denies report of takeover
talks", "Beta clarifies: no plans to raise guidance", "the deal is called off". A headline that
denies or walks back the news on the same stock within 3 days of a directional alert is now
linked to the alert it undercuts:

- **Graded:** what stocks did after a denial here, from the denial's price to that session's close,
  against the original call (how much further it gave back), and how much of the original alert's
  move was gone by then.
- **Flagged:** the denial's sheet names the alert it undercuts, and that alert's sheet says it was
  denied.
- **Pushed:** to the phones the original alert reached, or that watch, hold or have a call on the
  stock: "⚠ ACME: a denial after its alert".
- **Acted on:** the playbook autopilot closes its open paper calls on the denied alert at once.

The words are a simple, published list (denies, refutes, not in talks, no plans, clarifies,
retracts, walks back, calls off, terminates, withdraws, scraps) and only count on the same stock
within 3 days. History, not a forecast.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

WINDOW = 3 * 86400
MIN_N = 5
DENY = re.compile(
    r"\b(den(?:y|ies|ied)|refut(?:e|es|ed)|not in (?:talks|discussions|negotiations)|no (?:plans?|intention)\b"
    r"|clarif(?:y|ies|ied|ication)|retract(?:s|ed|ion)?|walk(?:s|ed)? back|call(?:s|ed)? off"
    r"|terminat(?:es|ed) (?:the )?(?:talks|agreement|deal|merger)|withdraw(?:s|n)?|scrap(?:s|ped)"
    r"|false (?:report|rumou?r)|rumou?rs? (?:are )?(?:untrue|false|baseless))",
    re.IGNORECASE,
)


def is_denial(title: str) -> bool:
    return bool(DENY.search(title or ""))


def _sym(a: dict[str, Any]) -> str:
    o = (a.get("edge") or {}).get("outcome") or {}
    return next(iter(o), None) or ((a.get("tickers") or [""])[0])


def undercuts(engine: Engine, a: dict[str, Any]) -> dict[str, Any] | None:
    """The directional, non-denial alert on the same stock in the 3 days before this denial."""
    from .dossier import _direction

    if not is_denial(a.get("title") or ""):
        return None
    sym = _sym(a)
    if not sym:
        return None
    t = float(a.get("created") or time.time())
    for b in engine.storage.alerts_for_ticker(sym, since=t - WINDOW, limit=40):
        if b.get("id") == a.get("id") or b.get("kind") == "system" or float(b["created"]) >= t:
            continue
        if _direction(b) in ("up", "down") and not is_denial(b.get("title") or ""):
            return b
    return None


def denial_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any] | None:
    """{"n", "further": {"n", "avg", "hit"}, "kept": share of the original move left}: after denials here."""
    from .dossier import _direction

    now = now or time.time()
    further, kept = [], []
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        if a.get("kind") == "system" or not is_denial(a.get("title") or ""):
            continue
        prev = undercuts(engine, a)
        if not prev:
            continue
        d = _direction(prev)
        sign = 1 if d == "up" else -1
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if row and row.get("close") is not None:
            further.append(-sign * row["close"])  # against the original call: + is more given back
        po = (prev.get("edge") or {}).get("outcome") or {}
        prow = next(iter(po.values()), None) if po else None
        pref = ((prev.get("edge") or {}).get("refs") or {}).get(_sym(prev))
        dref = ((a.get("edge") or {}).get("refs") or {}).get(_sym(a))
        if prow is not None and pref and dref and row and row.get("close") is not None:
            ran = sign * (dref / pref - 1) * 100  # the original call's move up to the denial
            after = sign * ((dref * (1 + row["close"] / 100)) / pref - 1) * 100
            if ran > 0.5:
                kept.append(max(-1.0, min(1.5, after / ran)))
    if len(further) < MIN_N:
        return None
    out: dict[str, Any] = {"n": len(further), "further": {"n": len(further), "avg": round(sum(further) / len(further), 2),
                                                           "hit": round(sum(x > 0 for x in further) / len(further), 2)}}  # fmt: skip
    if len(kept) >= MIN_N:
        out["kept"] = round(sorted(kept)[len(kept) // 2], 2)
    return out


def stamp(engine: Engine, a: dict[str, Any]) -> dict[str, Any] | None:
    prev = undercuts(engine, a)
    if not prev:
        return None
    out: dict[str, Any] = {"prev_id": prev["id"], "prev_title": (prev.get("title") or "")[:140], "symbol": _sym(a),
                           "hours": round((float(a["created"]) - float(prev["created"])) / 3600, 1)}  # fmt: skip
    rec = getattr(engine, "denial_rates", None)
    if rec:
        out["rec"] = rec
    out["line"] = line(out)
    return out


def line(d: dict[str, Any]) -> str:
    from .repeat import ago

    when = ago(d["hours"])
    head = f"A denial {when} after its alert (“{d['prev_title'][:80]}”)"
    r = d.get("rec") or {}
    if r.get("further"):
        f = r["further"]
        head += f". After denials here, stocks moved {f['avg']:+.1f}% further against the original call by the close ({f['n']})"
        if r.get("kept") is not None:
            head += f", keeping {round(r['kept'] * 100)}% of the original move at the median"
    return head + "."


async def act(engine: Engine, a: dict[str, Any], d: dict[str, Any], now: float | None = None) -> int:
    """Mark the denied alert, push the phones on the stock, and close the playbook autopilot's calls on it."""
    from .waits import _devices

    now = now or time.time()
    prev = engine.storage.get_alert(d["prev_id"])
    if not prev:
        return 0
    engine.storage.update_alert_edge(
        prev["id"], {"denied": {"by": a["id"], "title": (a.get("title") or "")[:140], "ts": now}}
    )
    sym = d["symbol"]
    closed = 0
    for dev, p in list(engine.desk.profiles.items()):
        for t in list(p.get("tracked") or []):
            if t.get("auto") and t.get("alert_id") == prev["id"]:
                q = engine.price_of(t["symbol"], 900, now=now)
                engine.desk.apply(
                    dev,
                    "track_close",
                    {"id": t["id"], "lesson": "autopilot: the news was denied"[:300]},
                    lambda s, _q=q: _q,
                )
                closed += 1
    if engine.webpush is None:
        return closed
    payload = {"id": f"dn-{a['id']}"[:60], "title": f"⚠ {sym}: a denial after its alert", "body": f"{(a.get('title') or '')[:140]}\n{d['line']}"[:500],
               "severity": "HIGH", "kind": "system", "tickers": [sym], "tag": f"dn-{sym}", "ts": now, "open": f"/app/#a={a['id']}"}  # fmt: skip
    for dev in sorted(_devices(engine, prev, sym, "denials")):
        await engine._to_device(dev, dict(payload), urgency="high")
    return closed
