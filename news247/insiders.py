"""Insider buys, graded by shape: which kinds of Form 4 purchases the stock followed.

The research on insider buying (Cohen, Malloy and Pomorski; Lakonishok and Lee) says the signal
isn't in "an insider bought" but in who, how much and how unusual: a CEO's first open-market buy in
years, several insiders at once, a buy after a drop, carry information; a director's routine yearly
purchase, or one on a pre-set 10b5-1 plan, mostly don't. Each insider-buy alert already carries
those facts from the filing (sources/sec_insider.py); here they're read into a shape:

- **Who:** the CEO or president or chair, the CFO, another officer, a director, a 10% owner.
- **How much:** under $100K, $100K-1M, $1M+.
- **How unusual:** opportunistic (not the insider's usual month), routine, or new (under three
  years of history).
- **With others:** a cluster (other insiders bought the same stock in the same window) or alone.
- **After a drop:** 20%+ under the 3-month high, or not.
- **On a plan:** a pre-set 10b5-1 trading plan, or not.

Graded like every alert, and also a month on (20 sessions, dossier.record_month), since insider
buying plays out over weeks. Each dimension pools into a record (five sessions and a month, its way,
and against SPY over the same window), and a new insider buy carries the most specific record with
8+ graded: "Insider buys like this (CEO, opportunistic): 64% up five sessions on (median +3.1%,
14)". History, not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

MIN_N = 8
HORIZONS = ("close", "d5", "d20")
ROLES = {"top": "CEO, president or chair", "cfo": "CFO", "officer": "Other officers", "director": "Directors",
         "owner": "10% owners", "other": "Other insiders"}  # fmt: skip
SHORT = {"top": "CEO-level", "cfo": "CFO", "officer": "officer", "director": "director", "owner": "10% owner",
         "other": "insider"}  # fmt: skip
SIZES = ((100_000, "small", "under $100K"), (1_000_000, "mid", "$100K-1M"), (float("inf"), "big", "$1M+"))
PATTERNS = {
    "opportunistic": "opportunistic",
    "routine": "routine",
    "new": "new to buying",
    "": "pattern unknown",
}
DIMENSIONS = (
    ("role", "Who bought", lambda s: s["role"], lambda v: ROLES[v]),
    ("size", "How much", lambda s: s["size"], lambda v: next(lbl for _, k, lbl in SIZES if k == v)),
    ("pattern", "How unusual", lambda s: s["pattern"], lambda v: PATTERNS[v].capitalize()),
    ("cluster", "With others", lambda s: "yes" if s["cluster"] else "no", lambda v: "A cluster" if v == "yes" else "Alone"),
    ("dip", "After a drop", lambda s: "yes" if s["dip"] else "no", lambda v: "20%+ off the high" if v == "yes" else "Not after a drop"),
    ("plan", "On a plan", lambda s: "yes" if s["plan"] else "no", lambda v: "On a 10b5-1 plan" if v == "yes" else "Not on a plan"),
)  # fmt: skip


def role_bucket(role: str) -> str:
    r = (role or "").strip()
    if r in ("CEO", "President", "Chair"):
        return "top"
    if r == "CFO":
        return "cfo"
    if r == "Director":
        return "director"
    if r == "10% owner":
        return "owner"
    if r in ("Insider", ""):
        return "other"
    return "officer"  # "Officer" or an officer title the filing spelled out


def read_buy(a: dict[str, Any]) -> dict[str, Any] | None:
    """The shape of an insider-buy alert, from the Form 4 facts it carries; None for any other alert."""
    ib = ((a.get("item") or {}).get("extra") or {}).get("insider_buy")
    if not ib:
        return None
    value = float(ib.get("value") or 0)
    off = ib.get("off_3m_high_pct")
    s = {"role": role_bucket(str(ib.get("role") or "")), "size": next(k for top, k, _ in SIZES if value < top),
         "pattern": ib.get("pattern") if ib.get("pattern") in PATTERNS else "", "cluster": bool(ib.get("cluster")),
         "dip": off is not None and off <= -20, "plan": bool(ib.get("plan_10b5_1")), "value": value}  # fmt: skip
    s["label"] = label(s)
    return s


def label(s: dict[str, Any]) -> str:
    """'CEO-level, $1M+, opportunistic, a cluster'."""
    bits = [SHORT[s["role"]], next(lbl for _, k, lbl in SIZES if k == s["size"])]
    if s["pattern"]:
        bits.append(PATTERNS[s["pattern"]])
    if s["cluster"]:
        bits.append("a cluster")
    if s["dip"]:
        bits.append("after a 20%+ drop")
    if s["plan"]:
        bits.append("on a 10b5-1 plan")
    return _cap(", ".join(bits))


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:]


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def keys(s: dict[str, Any]) -> list[str]:
    """Most specific first: the record falls back through these."""
    return [f"role+pattern:{s['role']}+{s['pattern']}", f"role+size:{s['role']}+{s['size']}", f"role:{s['role']}",
            f"pattern:{s['pattern']}", f"size:{s['size']}", "*"]  # fmt: skip


def insider_rates(engine: Engine, days: float = 730, now: float | None = None) -> dict[str, Any]:
    """{key: {"key", "label", "close", "d5", "d20", "x_d20"}} for every dimension value, the
    combinations used for a new buy, and "*" (every graded insider buy)."""
    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    labels: dict[str, str] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        s = read_buy(a)
        if s is None:
            continue
        e = a.get("edge") or {}
        o = e.get("outcome") or {}
        row = next(iter(o.values()), None) if o else None
        if not row or not row.get("sign"):
            continue
        sign = row["sign"]
        mkt = e.get("mkt_outcome") or {}
        groups = {"*": "Every insider buy"}
        for key, _, get, name in DIMENSIONS:
            groups[f"{key}:{get(s)}"] = name(get(s))
        groups[f"role+pattern:{s['role']}+{s['pattern']}"] = _cap(
            f"{SHORT[s['role']]}, {PATTERNS[s['pattern']]}"
        )
        groups[f"role+size:{s['role']}+{s['size']}"] = _cap(
            f"{SHORT[s['role']]}, {next(lbl for _, k, lbl in SIZES if k == s['size'])}"
        )
        for k, lbl in groups.items():
            g = pools.setdefault(k, {h: [] for h in (*HORIZONS, "x_d20", "x_d5")})
            labels[k] = lbl
            for h in HORIZONS:
                if row.get(h) is not None:
                    g[h].append(sign * row[h])
            for h in ("d5", "d20"):
                if row.get(h) is not None and mkt.get(h) is not None:
                    g[f"x_{h}"].append(sign * (row[h] - mkt[h]))
    out = {}
    for k, g in pools.items():
        if not g["d5"] and not g["close"]:
            continue
        out[k] = {"key": k, "label": labels[k], **{h: _stats(v) for h, v in g.items()}}
    return out


def insider_base(rates: dict[str, Any], s: dict[str, Any] | None) -> dict[str, Any] | None:
    """The most specific record for this buy's shape with MIN_N graded five sessions on."""
    if not s:
        return None
    for k in keys(s):
        r = rates.get(k)
        if r and r.get("d5") and r["d5"]["n"] >= MIN_N:
            return r
    return None


def insider_line(b: dict[str, Any] | None) -> str:
    if not b:
        return ""
    d5 = b["d5"]
    month = (
        f", {b['d20']['median']:+.1f}% a month on ({b['d20']['n']})"
        if b.get("d20") and b["d20"]["n"] >= MIN_N
        else ""
    )
    who = "Insider buys here" if b["key"] == "*" else f"Insider buys like this ({b['label']})"
    return f"{who}: {round(d5['hit'] * 100)}% up five sessions on (median {d5['median']:+.1f}%, {d5['n']}){month}"


def insider_board(rates: dict[str, Any]) -> dict[str, Any]:
    """Each dimension, its values side by side: what separated the buys that worked here."""
    dims = []
    for key, title, _, _ in DIMENSIONS:
        rows = [r for k, r in rates.items() if k.startswith(f"{key}:") and r.get("d5")]
        if rows:
            dims.append({"key": key, "title": title, "rows": sorted(rows, key=lambda r: -r["d5"]["median"])})
    combos = [r for k, r in rates.items() if "+" in k.split(":")[0] and r.get("d5") and r["d5"]["n"] >= MIN_N]
    combos.sort(key=lambda r: -r["d5"]["median"])
    return {"all": rates.get("*"), "dims": dims, "best": [r for r in combos if r["d5"]["median"] > 0][:5],
            "worst": [r for r in reversed(combos) if r["d5"]["median"] < 0][:5], "need": MIN_N}  # fmt: skip
