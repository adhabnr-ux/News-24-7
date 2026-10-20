"""Liquidity bands: where the paper edge is a tradeable edge.

A paper call fills at the alert price. In a stock that trades $50M a day that's roughly what a person
gets; in one that trades $500K, the first buyer moves the price and the rest chase it. So each
alert now carries its stock's dollar volume when it went out (the screener's last read of price and
shares traded) in four bands:

- **Thin:** under $2M a day;
- **Light:** $2M to $20M;
- **Liquid:** $20M to $200M;
- **Deep:** $200M and over.

Per band the record grades the plain way (in at the alert, out at the close), the same 5 minutes
late (from where the stock stood 5 minutes in), and the playbook's own way. The 5-minutes-late
number is the tradeability test: when thin stocks lost even 5 minutes late on 15+ graded, their
paper edge is a fill nobody gets, and the playbook sits thin stocks out (the autopilot passes and the
board says why). History, not a forecast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

BANDS = (("thin", 2e6), ("light", 2e7), ("liquid", 2e8), ("deep", float("inf")))
LABELS = {
    "thin": "Thin (under $2M a day)",
    "light": "Light ($2M-20M)",
    "liquid": "Liquid ($20M-200M)",
    "deep": "Deep ($200M+)",
}
MIN_N = 15


def band(dv: float | None) -> str | None:
    if not dv or dv <= 0:
        return None
    return next(b for b, top in BANDS if dv < top)


def stamp(engine: Engine, sym: str) -> dict[str, Any] | None:
    li = engine.universe.get(sym) if sym and getattr(engine, "universe", None) is not None else None
    dv = getattr(li, "dollar_volume", None) if li is not None else None
    b = band(dv)
    if not b:
        return None
    out: dict[str, Any] = {"dv": round(dv), "band": b}
    r = (getattr(engine, "liq_rates", None) or {}).get(b) or {}
    if r.get("untradeable"):
        out["pass"] = True
        out["why"] = (
            f"a thin stock: thin names here lost even 5 minutes late ({r['late']['avg']:+.1f}%, {r['late']['n']})"
        )
    return out


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "avg": round(sum(xs) / len(xs), 2),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
    }


def liq_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{band: {"band", "label", "plain", "late", "playbook", "untradeable"}} from graded, stamped alerts."""
    from .pbcurve import taken

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        b = (e.get("liq") or {}).get("band")
        o = e.get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not b or not row or not row.get("sign") or row.get("close") is None:
            continue
        g = pools.setdefault(b, {"plain": [], "late": [], "playbook": []})
        sign = row["sign"]
        g["plain"].append(sign * row["close"])
        if row.get("m5") is not None:
            g["late"].append(sign * ((1 + row["close"] / 100) / (1 + row["m5"] / 100) - 1) * 100)
        tk = taken(a)
        if tk:
            g["playbook"].append(tk[0])
    out = {}
    for b, g in pools.items():
        r = {"band": b, "label": LABELS[b], **{k: _stats(v) for k, v in g.items()}}
        late = r.get("late")
        r["untradeable"] = b == "thin" and bool(late) and late["n"] >= MIN_N and late["avg"] <= 0
        out[b] = r
    return out


def board(rates: dict[str, Any]) -> dict[str, Any]:
    order = [b for b, _ in BANDS]
    return {"rows": [rates[b] for b in order if b in rates], "need": MIN_N}
