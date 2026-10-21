"""The market's part of the playbook: its track record split into what SPY gave and what's beyond it.

A playbook that holds long news calls for five sessions in a rising market makes money whether or not
it knows anything. The track record (pbcurve.py) says what the playbook made; this says how much of
that the market would have made anyway. Each graded alert the playbook took is set against SPY over
the same window, from the playbook's own way in to its own way out, in the call's direction (a short
call loses the market's rise):

- **The market's part:** SPY's move over that window, its way;
- **Beyond the market:** the rest, which is the playbook's actual edge.

The plain way (in at the alert, out at the close) gets the same split on the same alerts. Per kind of
news too, so a kind whose whole result was the tide shows it. A pass, or an entry that never came,
holds nothing: no market part.

The market is read at the alert, an hour in, at the close, the next day and five sessions on, and
now also at the open, 15 minutes in, at a second leg and when the call turns to act. Ways in whose
moment isn't on file (a patient fill) and the bracket exit have no reading; they're counted apart,
never guessed. History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8  # alerts with a reading before a kind of news gets its own split


def _mkt_in(e: dict[str, Any], style: str) -> float | None:
    """SPY's own move from the alert to that way in, raw % (0 at the alert), or None if not read."""
    mkt = e.get("mkt_outcome") or {}
    if style == "alert":
        return 0.0
    if style == "open":
        return mkt.get("open")
    if style == "vol15":
        return mkt.get("m15")
    if style in ("leg2", "turn"):
        px = ((e.get("leg2") if style == "leg2" else e.get("call_now")) or {}).get("mkt")
        ref = e.get("mkt_ref")
        return (float(px) / float(ref) - 1) * 100 if px and ref else None
    return None  # a patient fill: its moment isn't kept


def _window(sign: int, m_in: float, m_out: float) -> float:
    return sign * ((1 + m_out / 100) / (1 + m_in / 100) - 1) * 100


def market_parts(a: dict[str, Any], got: dict[str, float] | None = None) -> dict[str, float]:
    """{"in|out": SPY's move over that pair's own window, the call's way, %} for each pair this alert had
    and the market was read for (the walk-forward's beyond-the-market picker reads these)."""
    from .combos import combos

    e = a.get("edge") or {}
    mkt = e.get("mkt_outcome") or {}
    row = next(iter((e.get("outcome") or {}).values()), None) or {}
    sign = row.get("sign") or 0
    if not sign or not mkt:
        return {}
    out = {}
    for pair in got if got is not None else combos(a):
        pin, pout = pair.split("|")
        m_in = _mkt_in(e, pin)
        legs = [mkt.get(leg) if leg != "bracket" else None for leg in pout.split("+")]
        if m_in is None or any(x is None for x in legs):
            continue
        out[pair] = round(sum(_window(sign, m_in, float(x)) for x in legs) / len(legs), 2)  # type: ignore[arg-type]
    return out


def split(a: dict[str, Any]) -> dict[str, Any] | None:
    """{"pb", "pb_mkt", "plain", "plain_mkt"} for a graded, stamped alert (a market part is None when
    SPY wasn't read over that window), else None."""
    from .autopilot import playbook_in, playbook_out, playbook_pass
    from .combos import combos
    from .pbcurve import taken

    got = taken(a)
    if got is None:
        return None
    pb, plain = got
    e = a.get("edge") or {}
    row = next(iter((e.get("outcome") or {}).values()), None) or {}
    sign = row.get("sign") or 0
    mkt = e.get("mkt_outcome") or {}
    plain_mkt = round(sign * mkt["close"], 2) if sign and mkt.get("close") is not None else None
    pb_mkt: float | None
    pin, pout = playbook_in(a), playbook_out(a)
    if not (pin and pout) and not playbook_pass(a):
        pin, pout = "alert", "close"  # no pair to read: the plain way, as the track record takes it
    if playbook_pass(a) or f"{pin}|{pout}" not in combos(a):
        pb_mkt = 0.0  # it held nothing
    else:
        m_in = _mkt_in(e, pin)
        legs = [mkt.get(leg) if leg != "bracket" else None for leg in pout.split("+")]
        if not sign or m_in is None or any(x is None for x in legs):
            pb_mkt = None
        else:
            pb_mkt = round(sum(_window(sign, m_in, float(x)) for x in legs) / len(legs), 2)  # type: ignore[arg-type]
    return {"pb": pb, "pb_mkt": pb_mkt, "plain": plain, "plain_mkt": plain_mkt}


def _sum(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    if not pairs:
        return {"n": 0}
    beyond = [x - m for x, m in pairs]
    return {"n": len(pairs), "total": round(sum(x for x, _ in pairs), 2), "market": round(sum(m for _, m in pairs), 2),
            "beyond": round(sum(beyond), 2), "avg_beyond": round(sum(beyond) / len(beyond), 3),
            "hit_beyond": round(sum(b > 0 for b in beyond) / len(beyond), 2)}  # fmt: skip


def market_part(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"n", "unread", "playbook": {...}, "plain": {...}, "kinds": [...], "verdict", "line"}."""
    from .combos import label_of

    now = now or time.time()
    pb_rows: list[tuple[float, float]] = []
    plain_rows: list[tuple[float, float]] = []
    kinds: dict[str, list[tuple[float, float]]] = {}
    n = unread = 0
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        sp = split(a)
        if sp is None:
            continue
        n += 1
        if sp["pb_mkt"] is None:
            unread += 1
        else:
            pb_rows.append((sp["pb"], sp["pb_mkt"]))
            kind = ((a.get("edge") or {}).get("combo_rec") or {}).get("kind") or "*"
            kinds.setdefault(kind, []).append((sp["pb"], sp["pb_mkt"]))
        if sp["plain_mkt"] is not None:
            plain_rows.append((sp["plain"], sp["plain_mkt"]))
    pb, plain = _sum(pb_rows), _sum(plain_rows)
    krows = sorted(
        ({"kind": k, "label": label_of(k), **_sum(v)} for k, v in kinds.items() if len(v) >= MIN_N),
        key=lambda r: -r["n"],
    )[:12]
    out = {"n": n, "unread": unread, "playbook": pb, "plain": plain, "kinds": krows, "need": MIN_N}
    out["verdict"] = verdict(pb)
    out["line"] = line(out)
    return out


def verdict(s: dict[str, Any]) -> str:
    """ "edge" (most of it beyond the market), "tide" (most of it the market), "behind" (it loses
    beyond the market), or "" with nothing to read."""
    if not s.get("n"):
        return ""
    if s["beyond"] < 0:
        return "behind"
    if s["market"] > s["beyond"]:
        return "tide"
    return "edge"


def line(r: dict[str, Any]) -> str:
    p, q = r["playbook"], r["plain"]
    if not p.get("n"):
        return ""
    head = (
        f"On {p['n']} alerts with a market reading, the playbook made {p['total']:+.1f}%: the market's part "
        f"{p['market']:+.1f}%, beyond it {p['beyond']:+.1f}% ({p['avg_beyond']:+.2f}% an alert; "
        f"{round(p['hit_beyond'] * 100)}% beat the market)."
    )
    if q.get("n"):
        head += f" The plain way: {q['total']:+.1f}%, the market's part {q['market']:+.1f}%, beyond it {q['beyond']:+.1f}%."
    tail = {
        "edge": " Most of the playbook's result is its own.",
        "tide": " Most of it was the market: the playbook rode the tide more than it read the news.",
        "behind": " Beyond the market it loses: the market made its result, and then some.",
    }.get(r["verdict"], "")
    return head + tail
