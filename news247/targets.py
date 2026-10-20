"""Analyst price targets, read from the headlines Foretape already collects.

"Goldman Sachs raises Acme price target to $150 from $120", "Acme PT lowered to $80 at Morgan
Stanley", "Jefferies initiates Acme at buy with $60 target": each becomes {firm, action, to, from,
rating} on its stock. A stock page gets its targets by firm (the newest from each), their median,
range and the upside from the live price; the Watch tab gets the day's biggest changes; and a
phone watching the stock gets a push when a target moves 15%+ or sits 25%+ from the price.
Only what the headline says: no target is inferred, and a headline that can't be read is skipped.
"""

from __future__ import annotations

import re
import statistics as st
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

FIRMS = [
    "Goldman Sachs", "Morgan Stanley", "JPMorgan", "J.P. Morgan", "JP Morgan", "Bank of America", "BofA",
    "Citigroup", "Citi", "Wells Fargo", "Barclays", "UBS", "Deutsche Bank", "Jefferies", "Piper Sandler",
    "Needham", "Wedbush", "Mizuho", "Bernstein", "Evercore ISI", "Evercore", "Raymond James", "KeyBanc",
    "Truist", "Oppenheimer", "Stifel", "RBC Capital", "RBC", "BMO Capital", "BMO", "TD Cowen", "Cowen",
    "Cantor Fitzgerald", "Cantor", "Baird", "Loop Capital", "Rosenblatt", "Benchmark", "B. Riley",
    "H.C. Wainwright", "HC Wainwright", "Canaccord", "Macquarie", "Guggenheim", "Susquehanna",
    "Wolfe Research", "Redburn", "Argus", "Morningstar", "D.A. Davidson", "DA Davidson", "Craig-Hallum",
    "Roth", "Northland", "Ladenburg", "Maxim", "Lake Street", "JMP", "Telsey", "BTIG", "Bernstein SocGen",
    "Scotiabank", "Citizens JMP", "Seaport", "Melius", "Erste", "HSBC", "Nomura", "Daiwa", "Exane BNP",
    "BNP Paribas", "Societe Generale", "Kepler", "Berenberg", "Morgan Stanley", "William Blair", "Leerink",
    "Leerink Partners", "Bank of America Securities", "Wells", "Compass Point", "Keefe Bruyette",
    "KBW", "Janney", "Moffett", "MoffettNathanson", "New Street", "Tigress", "Arete", "CFRA",
]  # fmt: skip
_FIRM_RE = re.compile(
    r"\b(" + "|".join(sorted({re.escape(f) for f in FIRMS}, key=len, reverse=True)) + r")\b", re.I
)
_CANON = {f.lower(): f for f in FIRMS}
_TARGET = re.compile(r"\b(?:price\s+target|target\s+price|PT|price\s+objective|target)\b", re.I)
_MONEY = r"(?:US)?\$\s?(\d[\d,]*(?:\.\d+)?)(?!\s*(?:million|billion|mln|bln|bn|trillion|[MBK]\b))"
_TO = re.compile(r"\bto\s+" + _MONEY, re.I)
_FROM = re.compile(r"\bfrom\s+" + _MONEY, re.I)
_BEFORE = re.compile(_MONEY + r"\s+(?:price\s+)?(?:target|PT|price\s+objective)\b", re.I)
_AFTER = re.compile(r"(?:target|PT|objective)(?:\s+(?:of|at))?\s+" + _MONEY, re.I)
_RAISE = re.compile(
    r"\b(?:raise[sd]?|lift(?:s|ed)?|boost(?:s|ed)?|hike[sd]?|up(?:s|ped)|increase[sd]?|bump(?:s|ed)?|ups)\b",
    re.I,
)
_CUT = re.compile(
    r"\b(?:cut(?:s)?|lower(?:s|ed)?|trim(?:s|med)?|reduce[sd]?|slash(?:es|ed)?|decrease[sd]?)\b", re.I
)
_INIT = re.compile(
    r"\b(?:initiat\w*|starts?\s+(?:coverage|at)|launch(?:es|ed)?\s+coverage|resum\w+\s+coverage|assum\w+\s+coverage)\b",
    re.I,
)
_RATING = re.compile(
    r"\b(strong[- ]buy|buy|outperform|overweight|accumulate|market[- ]perform|sector[- ]perform|peer[- ]perform|"
    r"neutral|hold|equal[- ]weight|in[- ]line|underperform|underweight|reduce|sell)\b",
    re.I,
)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_target(title: str) -> dict[str, Any] | None:
    """{firm, action (raise|cut|initiate|maintain|set), to, from, rating} from a headline, or None."""
    t = title or ""
    if not _TARGET.search(t) or "$" not in t:
        return None
    strong = re.search(r"\bprice\s+target|\btarget\s+price|\bPT\b|\bprice\s+objective", t, re.I)
    if not strong and not (_FIRM_RE.search(t) or _RATING.search(t) or re.search(r"\banalysts?\b", t, re.I)):
        return None  # "hits its target of $300 million": not an analyst's price target
    m_from = _FROM.search(t)
    to = None
    for rx in (_TO, _BEFORE, _AFTER):
        m = rx.search(t)
        if m:
            to = _num(m.group(1))
            break
    if to is None or not 0.5 <= to <= 100000:
        return None
    frm = _num(m_from.group(1)) if m_from else None
    if frm is not None and (frm == to or not 0.2 <= to / frm <= 5):
        frm = None
    if frm is not None:
        action = "raise" if to > frm else "cut"
    elif _INIT.search(t):
        action = "initiate"
    elif _RAISE.search(t):
        action = "raise"
    elif _CUT.search(t):
        action = "cut"
    elif re.search(r"\b(?:maintain|reiterat|keeps?|affirm|reaffirm)\w*", t, re.I):
        action = "maintain"
    else:
        action = "set"
    firm = _FIRM_RE.search(t)
    rating = _RATING.search(t)
    return {
        "firm": _CANON.get(firm.group(1).lower(), firm.group(1)) if firm else "",
        "action": action,
        "to": to,
        "from": frm,
        "change_pct": round((to / frm - 1) * 100, 1) if frm else None,
        "rating": rating.group(1).lower().replace(" ", "-") if rating else "",
    }


def _read(row: dict[str, Any]) -> dict[str, Any] | None:
    pt = parse_target(row.get("title") or "")
    if pt is None or not row.get("tickers"):
        return None
    return {**pt, "symbol": row["tickers"][0], "title": row["title"], "url": row.get("url"),
            "source": row.get("source"), "ts": row.get("published") or row.get("detected")}  # fmt: skip


def targets_for(engine: Engine, sym: str, now: float | None = None, days: float = 90) -> dict[str, Any]:
    """A stock's analyst targets from the last ``days``: the newest from each firm, their median and
    range, the upside from the live price, and the raises against the cuts in the last 30 days."""
    now = now or time.time()
    rows = [
        r
        for r in (_read(x) for x in engine.storage.items_for_ticker(sym, since=now - days * 86400, limit=400))
        if r and r["symbol"] == sym
    ]
    rows.sort(key=lambda r: -(r["ts"] or 0))
    seen, latest = set(), []
    for r in rows:
        key = r["firm"] or r["title"]  # an unnamed firm counts once per headline
        if key in seen:
            continue
        seen.add(key)
        latest.append(r)
    q = engine.price_of(sym, 86400, now=now)
    px = q[1] if q else None
    out: dict[str, Any] = {
        "symbol": sym,
        "price": px,
        "n": len(latest),
        "rows": latest[:12],
        "history": rows[:15],
    }
    if latest:
        tos = [r["to"] for r in latest]
        med = st.median(tos)
        out.update(median=round(med, 2), high=max(tos), low=min(tos))
        if px:
            out["upside"] = round((med / px - 1) * 100, 1)
            for r in out["rows"]:
                r["upside"] = round((r["to"] / px - 1) * 100, 1)
    month = [r for r in rows if (r["ts"] or 0) >= now - 30 * 86400]
    out["raises"] = sum(r["action"] == "raise" for r in month)
    out["cuts"] = sum(r["action"] == "cut" for r in month)
    return out


def target_board(
    engine: Engine, now: float | None = None, days: float = 3, limit: int = 25
) -> dict[str, Any]:
    """The last few days' target changes across every stock, the biggest first."""
    now = now or time.time()
    rows = []
    for x in engine.storage.items_matching(
        ("%target%", "% PT %", "% PT"), since=now - days * 86400, limit=600
    ):
        r = _read(x)
        if r is None:
            continue
        q = engine.price_of(r["symbol"], 86400, now=now)
        if q:
            r["price"] = q[1]
            r["upside"] = round((r["to"] / q[1] - 1) * 100, 1)
        rows.append(r)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -(r["ts"] or 0)):
        key = (r["symbol"], r["firm"], r["to"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    out.sort(
        key=lambda r: -abs(r["change_pct"] if r["change_pct"] is not None else (r.get("upside") or 0) / 2)
    )
    return {"rows": out[:limit], "days": days}


def is_big(pt: dict[str, Any], price: float | None) -> bool:
    """Worth a push to a watcher: a 15%+ change, or a target 25%+ from the price."""
    if pt["change_pct"] is not None and abs(pt["change_pct"]) >= 15:
        return True
    return bool(price) and abs(pt["to"] / price - 1) >= 0.25 and pt["action"] in ("raise", "cut", "initiate")
