"""Market-wide news, graded by theme: which corners of the market moved after a tariff, Fed, oil or
geopolitics headline.

Stock news is graded on its stock. A headline about tariffs, the Fed, an oil shock or a war names no
stock at all, and was graded on nothing, though it moves whole sectors and every portfolio at once.
Now an alert carrying a market-wide theme notes a basket of market and sector ETFs when it goes out
(edge.etf_refs: SPY, QQQ, IWM, the sector SPDRs, semis, regional banks, bonds, gold, oil). Their
moves are graded an hour later and at the close (edge.etf_out).

Pooled per theme, each ETF's record gives how often it rose and its median move, and the same
against SPY (did it beat the market). The headline's sheet shows its theme's biggest movers here:
"Trade-war headlines: SMH -1.1% in the hour (down 78%, 9), XLU +0.2%". The Watch tab lists every
theme. Raw moves, not "its way": a market-wide headline rarely carries a clean direction. History,
not a forecast.
"""

from __future__ import annotations

import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

THEMES = {
    "fed_policy": "Fed policy", "fed_independence": "Fed independence", "macro_print": "Macro data",
    "trade_war": "Trade-war", "chip_export_controls": "Chip export controls", "geopolitical_shock": "Geopolitical",
    "oil_supply_shock": "Oil supply", "systemic_stress": "Financial stress", "yield_threshold": "Bond-yield",
    "election_shock": "Election", "asia_market_shock": "Asian market", "pandemic_shock": "Pandemic",
    "crypto_policy": "Crypto policy", "ai_slowdown": "AI slowdown", "ai_capex_retreat": "AI capex",
}  # fmt: skip
BASKET = ("SPY", "QQQ", "IWM", "SMH", "IGV", "XBI", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE",
          "XLB", "XLC", "KRE", "TLT", "GLD", "USO")  # fmt: skip
MIN_N = 5


def theme_of(alert: Any) -> str | None:
    """The first market-wide theme on an alert (object or dict), or None."""
    an = alert.get("analysis") if isinstance(alert, dict) else getattr(alert, "analysis", None)
    themes = (an or {}).get("themes") if isinstance(an, dict) else getattr(an, "themes", None)
    return next((t for t in themes or [] if t in THEMES), None)


def stamp(engine: Engine, alert: Any, now: float | None = None) -> dict[str, float]:
    """The basket's prices when a market-wide headline goes out (edge.etf_refs)."""
    now = now or time.time()
    if not theme_of(alert):
        return {}
    out = {}
    for sym in BASKET:
        q = engine.price_of(sym, 900, now=now)
        if q and q[1]:
            out[sym] = q[1]
    return out if "SPY" in out else {}


def grade(engine: Engine, now: float | None = None) -> int:
    """The basket an hour on and at the session's close, from quotes within 3 hours of each moment."""
    from .dossier import session_closes

    now = now or time.time()
    made = 0
    for a in engine.storage.recent_alerts(600, since=now - 3 * 86400):
        e = a.get("edge") or {}
        refs = e.get("etf_refs") or {}
        if not refs:
            continue
        got = {k: dict(v) for k, v in (e.get("etf_out") or {}).items()}
        if all("close" in got.get(s, {}) for s in refs):
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
            engine.storage.update_alert_edge(a["id"], {"etf_out": got})
            made += 1
    return made


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if len(xs) < MIN_N:
        return None
    return {"n": len(xs), "up": round(sum(x > 0 for x in xs) / len(xs), 2), "median": round(st.median(xs), 2)}


def theme_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{theme: {"theme", "label", "n", "etfs": {sym: {"h1", "close", "vs_spy"}}}} for themes with
    5+ graded headlines; "vs_spy" is the ETF's hour against SPY's."""
    now = now or time.time()
    pools: dict[str, dict[str, dict[str, list[float]]]] = {}
    counts: dict[str, int] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        out = e.get("etf_out") or {}
        th = theme_of(a)
        if not out or not th:
            continue
        spy = (out.get("SPY") or {}).get("h1")
        counts[th] = counts.get(th, 0) + 1
        for sym, row in out.items():
            p = pools.setdefault(th, {}).setdefault(sym, {"h1": [], "close": [], "vs_spy": []})
            for h in ("h1", "close"):
                if row.get(h) is not None:
                    p[h].append(row[h])
            if row.get("h1") is not None and spy is not None and sym != "SPY":
                p["vs_spy"].append(row["h1"] - spy)
    out_: dict[str, Any] = {}
    for th, etfs in pools.items():
        rows = {sym: {k: _stats(v) for k, v in p.items()} for sym, p in etfs.items()}
        rows = {sym: r for sym, r in rows.items() if r["h1"] or r["close"]}
        if rows:
            out_[th] = {"theme": th, "label": THEMES[th], "n": counts[th], "etfs": rows}
    return out_


def movers(r: dict[str, Any] | None, k: int = 4) -> list[dict[str, Any]]:
    """A theme's ETFs that moved most in the hour (by the size of the median move)."""
    if not r:
        return []
    rows = [{"symbol": s, **x["h1"]} for s, x in r["etfs"].items() if x.get("h1")]
    return sorted(rows, key=lambda x: -abs(x["median"]))[:k]


def theme_line(r: dict[str, Any] | None) -> str:
    """'Trade-war headlines here: SMH -1.1% in the hour (down 78%, 9), XLU +0.2% (up 60%, 9)'."""
    ms = movers(r, 3)
    if not ms:
        return ""
    parts = [f"{m['symbol']} {m['median']:+.1f}% ({'up' if m['median'] >= 0 else 'down'} "
             f"{round((m['up'] if m['median'] >= 0 else 1 - m['up']) * 100)}%, {m['n']})" for m in ms]  # fmt: skip
    return f"{r['label']} headlines here, in the hour: " + ", ".join(parts)


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {"theme": r["theme"], "label": r["label"], "n": r["n"], "movers": movers(r, 5)}
        for r in rates.values()
    ]
    return {"rows": sorted(rows, key=lambda r: -r["n"]), "need": MIN_N}
