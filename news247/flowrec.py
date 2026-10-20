"""Flow record: unusual options activity, graded by its shape.

Options alerts used to go out without a direction or a reference price, so nothing graded them.
Now each one is read at publish:

- **Its direction**, from the flow: calls bought or puts sold lean up, puts bought or calls sold
  lean down, weighted by premium (prints mid-spread count half); a contract spiking thousands of
  percent leans with its type; a blowup with its side. Flow that's balanced has no direction and
  isn't graded.
- **Its shape:** calls or puts, how soon the lead contract expires (0-7, 8-30, 31+ days), how far
  out of the money it is (at or in the money within 2%, 2-10% out, 10%+ out), and how much premium
  it carried (under $250K, $250K-1M, $1M+).
- **Its reference price:** the stock's price in the chain it was read from.

Graded like every alert (an hour later, the close, the next day), the shapes pool into a record:
"OTM call buying, 0-7 days, $1M+: 58% went their way by the close (31)". A new alert carries the
most specific record with 8+ graded, else a coarser one. History, not a forecast; flow can be a
hedge, and the side is estimated from where prints sat in the spread.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
SIZES = ((250_000, "under $250K"), (1_000_000, "$250K-1M"), (float("inf"), "$1M+"))
DTES = ((7, "0-7 days"), (30, "8-30 days"), (10_000, "31+ days"))
LANES = {"flow": "flow", "spike": "spike", "blowup": "blowup", "surge": "surge"}


def _lead(d: dict[str, Any]) -> dict[str, Any] | None:
    for k in ("flow", "spikes", "blowups"):
        rows = d.get(k) or []
        if rows:
            return max(rows, key=lambda r: r.get("premium") or 0)
    return None


def flow_direction(d: dict[str, Any]) -> str:
    """ "up", "down" or "" for an options alert's dict (OptionsHit.to_dict)."""
    lane = d.get("kind")
    if lane == "blowup":
        return {"calls": "up", "puts": "down"}.get(d.get("side") or "", "")
    if lane == "spike" and d.get("spikes"):
        return "up" if d["spikes"][0].get("type") == "call" else "down"
    score = total = 0.0
    for r in d.get("flow") or []:
        w = float(r.get("premium") or 0)
        side = str(r.get("side") or "")
        lean = 1.0 if r.get("type") == "call" else -1.0
        if "sold" in side:
            lean = -lean
        elif "bought" not in side:
            lean *= 0.5  # mid-spread or unknown: half a vote for its type
        score += w * lean
        total += w
    if total <= 0:
        c, p = float(d.get("call_premium") or 0), float(d.get("put_premium") or 0)
        if c + p <= 0 or max(c, p) < 2 * min(c, p):
            return ""
        return "up" if c > p else "down"
    if score > 0.2 * total:
        return "up"
    if score < -0.2 * total:
        return "down"
    return ""


def flow_shape(d: dict[str, Any]) -> dict[str, str] | None:
    """{"lane", "side", "dte", "money", "size", "label"}; None when there's nothing to read."""
    lead = _lead(d)
    if lead is None:
        return None
    kind = lead.get("type") or ""
    side = "calls" if kind == "call" else "puts" if kind == "put" else "mixed"
    dte = next(lbl for top, lbl in DTES if (lead.get("dte") or 0) <= top)
    px, strike = d.get("stock_price"), lead.get("strike")
    money = "?"
    if px and strike:
        otm = (strike / px - 1) * 100 if kind == "call" else (1 - strike / px) * 100
        money = "at the money" if otm < 2 else "2-10% out" if otm < 10 else "10%+ out"
    prem = float(d.get("flow_premium") or lead.get("premium") or 0)
    size = next(lbl for top, lbl in SIZES if prem < top)
    lane = LANES.get(str(d.get("kind")), str(d.get("kind") or "flow"))
    label = f"{'OTM ' if money in ('2-10% out', '10%+ out') else ''}{side[:-1] if side != 'mixed' else 'mixed'} {lane}, {dte}, {size}"
    return {"lane": lane, "side": side, "dte": dte, "money": money, "size": size, "label": label}


def keys(shape: dict[str, str]) -> list[str]:
    """Most specific first: the record falls back through these."""
    lane, side = shape["lane"], shape["side"]
    return [
        f"{lane}|{side}|{shape['dte']}|{shape['money']}|{shape['size']}",
        f"{lane}|{side}|{shape['dte']}|{shape['money']}",
        f"{lane}|{side}|{shape['dte']}",
        f"{lane}|{side}",
        f"*|{side}",
    ]


def stamp(alert: Any, price: float | None = None) -> None:
    """Read an options alert at publish: its direction, shape and reference price (edge.flow, edge.refs)."""
    d = alert.edge.get("options") or {}
    if not d:
        return
    direction = flow_direction(d)
    shape = flow_shape(d)
    if shape:
        alert.edge["flow"] = {"dir": direction, **shape}
    ref = d.get("stock_price") or price
    if ref and alert.tickers and "refs" not in alert.edge:
        alert.edge["refs"] = {alert.tickers[0]: float(ref)}


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def flow_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    labels: dict[str, str] = {}
    for a in engine.storage.recent_alerts(6000, since=now - days * 86400):
        f = (a.get("edge") or {}).get("flow") or {}
        o = (a.get("edge") or {}).get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not f or not f.get("dir") or not row or not row.get("sign"):
            continue
        for k in keys(f):
            g = pools.setdefault(k, {"h1": [], "close": [], "d1": []})
            labels.setdefault(k, _key_label(k))
            for h in ("h1", "close", "d1"):
                if row.get(h) is not None:
                    g[h].append(row["sign"] * row[h])
    return {
        k: {"key": k, "label": labels[k], **{h: _stats(v) for h, v in g.items()}}
        for k, g in pools.items()
        if g["close"]
    }


def _key_label(k: str) -> str:
    parts = k.split("|")
    lane, side = parts[0], parts[1]
    head = f"{'any' if lane == '*' else lane} {side}"
    return ", ".join([head, *parts[2:]])


def flow_base(rates: dict[str, Any], shape: dict[str, str] | None) -> dict[str, Any] | None:
    if not shape:
        return None
    for k in keys(shape):
        r = rates.get(k)
        if r and r["close"] and r["close"]["n"] >= MIN_N:
            return r
    return None


def flow_line(fb: dict[str, Any] | None) -> str:
    if not fb:
        return ""
    c = fb["close"]
    return f"Flow like this ({fb['label']}): {round(c['hit'] * 100)}% went its way by the close (median {c['median']:+.1f}%, {c['n']})"


def flow_board(rates: dict[str, Any], min_n: int = MIN_N) -> dict[str, Any]:
    """Every shape with enough graded, ranked by how it followed through."""
    rows = [r for r in rates.values() if r["close"] and r["close"]["n"] >= min_n and r["key"].count("|") >= 2]
    kept: list[dict[str, Any]] = []
    for r in sorted(
        rows, key=lambda r: -r["key"].count("|")
    ):  # a coarser shape holding the same alerts adds nothing
        if not any(k["key"].startswith(r["key"] + "|") and k["close"]["n"] == r["close"]["n"] for k in kept):
            kept.append(r)
    rows = kept
    for r in rows:
        c = r["close"]
        r["score"] = round(c["median"] * c["n"] / (c["n"] + 10), 3)
    rows.sort(key=lambda r: -r["score"])
    return {"best": [r for r in rows if r["score"] > 0][:8], "worst": [r for r in reversed(rows) if r["score"] < 0][:8],
            "graded": sum(r["close"]["n"] for k, r in rates.items() if k.startswith("*|"))}  # fmt: skip
