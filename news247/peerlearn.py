"""Sympathy, learned: which stocks actually follow which on news here.

Sympathy pushes pick a news stock's peers by industry: the same group, not yet moved. Whether a
given peer tends to follow that stock's news was never measured, because only the alert's own stock
was priced. Now every HIGH alert with a direction also notes its stock's peers' prices when it goes
out (up to eight with a live quote, edge.peer_refs), and an hour later and at the close their moves
(edge.peer_out). Pooled per pair, the record says it: "when ACME has news, DLTA followed its way by
the close 70% of the time (median +1.1%, 10)".

A sympathy push carries its pair's record when there is one (5+ graded), and the stock page lists
who follows the stock's news (and who goes the other way). History, not a forecast: a pair needs a
handful of shared news days before it means anything, and the counts are always shown.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

from .dossier import session_closes

if TYPE_CHECKING:
    from .engine import Engine

MAX_PEERS = 8
MIN_N = 5


def stamp(engine: Engine, alert: Any, now: float | None = None) -> dict[str, float]:
    """Peers' prices when a HIGH alert goes out (edge.peer_refs)."""
    from .dossier import _direction, peers
    from .models import Severity

    now = now or time.time()
    if alert.severity < Severity.HIGH or not alert.tickers or not _direction(alert.to_dict()):
        return {}
    lead = alert.tickers[0]
    out = {}
    for r in peers(engine, lead, now, limit=12).get("rows") or []:
        q = engine.price_of(r["symbol"], 900, now=now)
        if q and q[1] and r["symbol"] != lead:
            out[r["symbol"]] = q[1]
        if len(out) >= MAX_PEERS:
            break
    return out


def grade(engine: Engine, now: float | None = None) -> int:
    """Peers an hour on and at the session's close, from quotes within 3 hours of each moment."""
    now = now or time.time()
    made = 0
    for a in engine.storage.recent_alerts(600, since=now - 3 * 86400):
        e = a.get("edge") or {}
        refs = e.get("peer_refs") or {}
        if not refs:
            continue
        got = {k: dict(v) for k, v in (e.get("peer_out") or {}).items()}
        if refs and all("close" in got.get(s, {}) for s in refs):
            continue
        created = float(a["created"])
        close_ts = session_closes(created, 1, engine.calendar.holidays)[0]
        changed = False
        for sym, ref in refs.items():
            row = got.setdefault(sym, {})
            q = engine.price_of(sym, now=now)
            if not q or not q[1] or q[0] < created:
                continue
            move = round((q[1] / ref - 1) * 100, 2)
            if "h1" not in row and now - created >= 3600 and q[0] - created < 3 * 3600:
                row["h1"], changed = move, True
            if "close" not in row and now >= close_ts and abs(q[0] - close_ts) < 3 * 3600:
                row["close"], changed = move, True
        if changed:
            engine.storage.update_alert_edge(a["id"], {"peer_out": got})
            made += 1
    return made


def pair_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """{"LEAD>PEER": {"lead", "peer", "n", "hit", "median"}}: how the peer moved by the close, the
    lead's news's way, on every graded day."""
    now = now or time.time()
    pools: dict[tuple[str, str], list[float]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        out = e.get("peer_out") or {}
        o = e.get("outcome") or {}
        if not out or not o or not a.get("tickers"):
            continue
        sign = next(iter(o.values())).get("sign")
        if not sign:
            continue
        lead = a["tickers"][0]
        for peer, row in out.items():
            if row.get("close") is not None:
                pools.setdefault((lead, peer), []).append(sign * row["close"])
    return {f"{lead}>{peer}": {"lead": lead, "peer": peer, "n": len(xs), "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                               "median": round(st.median(xs), 2)} for (lead, peer), xs in pools.items()}  # fmt: skip


def pair(rates: dict[str, Any], lead: str, peer: str) -> dict[str, Any] | None:
    r = rates.get(f"{lead}>{peer}")
    return r if r and r["n"] >= MIN_N else None


def pair_line(r: dict[str, Any] | None) -> str:
    if not r:
        return ""
    return (f"When {r['lead']} has news, {r['peer']} followed its way by the close {round(r['hit'] * 100)}% "
            f"of the time here (median {r['median']:+.1f}%, {r['n']}).")  # fmt: skip


def followers(rates: dict[str, Any], lead: str) -> dict[str, Any]:
    """Who follows this stock's news here, and who goes the other way (5+ graded each)."""
    rows = [r for r in rates.values() if r["lead"] == lead and r["n"] >= MIN_N]
    rows.sort(key=lambda r: (-r["hit"], -r["n"]))
    return {"lead": lead, "follow": [r for r in rows if r["hit"] >= 0.5], "against": [r for r in rows if r["hit"] < 0.5],
            "need": MIN_N}  # fmt: skip
