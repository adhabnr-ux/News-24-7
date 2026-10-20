"""What kind of news is this, and how has that kind of news traded for companies this size?

Kind-level base rates ("Headlines · HIGH went their way 58% of the time") mix an FDA approval with
an analyst downgrade. Here every alert is read for its catalyst (deal target, FDA approval, trial
win, earnings beat, guidance cut, offering, contract, upgrade, probe, short report...) and a size
band (under $2B, $2-10B, $10B+), and graded alerts are pooled per (catalyst, band), falling back
to the catalyst alone when the band is thin. Deterministic: the same headline always reads the
same, so the history of alerts stored before this existed counts from day one.

``room_left`` uses those rates for today's alerts: the history's typical move the alert's way by
the close, against how far this one has gone so far. History, not a forecast.
"""

from __future__ import annotations

import re
import statistics as st
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .analysis.smallcap import ACQUIRED_RE, GUIDE_DOWN_RE, GUIDE_UP_RE, OFFERING_RE, TAKEOVER_INTEREST_RE
from .desk import alert_category

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
MIN_N = 6  # graded alerts before a catalyst's record is shown


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.I)


# (key, label, pattern), most specific first: the first that matches wins
FAMILIES: list[tuple[str, str, re.Pattern[str] | None]] = [
    (
        "bankruptcy",
        "Bankruptcy",
        _rx(r"\bchapter\s+(?:7|11)\b|\bfiles? for bankruptcy\b|\bbankruptcy (?:protection|filing)\b"),
    ),
    (
        "short_report",
        "Short-seller report",
        _rx(
            r"\bshort[- ](?:seller|report)\b|\b(?:hindenburg|muddy waters|citron|spruce point|fuzzy panda|culper|blue orca|grizzly research|iceberg research)\b"
        ),
    ),
    (
        "fda_negative",
        "FDA setback",
        _rx(
            r"\bcomplete response letter\b|\bCRL\b|\bclinical hold\b|\bFDA\b.{0,60}\b(?:rejects?|refus\w+|declines? to approve)\b"
        ),
    ),
    (
        "fda_approval",
        "FDA approval",
        _rx(
            r"\bFDA\b.{0,60}\b(?:approv\w*|clears?|cleared|clearance|grants? accelerated)\b|\bapproved by the FDA\b"
        ),
    ),
    (
        "trial_fail",
        "Trial failure",
        _rx(
            r"\b(?:fail(?:s|ed)?|did not meet|does not meet|misses?|missed)\b.{0,40}\b(?:primary )?endpoint\b|\bdiscontinu\w+ .{0,30}\btrial\b"
        ),
    ),
    (
        "trial_win",
        "Positive trial data",
        _rx(
            r"\bmeets?\b.{0,30}\bprimary endpoint\b|\bmet (?:its |the )?primary endpoint\b|\bpositive (?:topline|top-line|phase|pivotal|interim)\b"
        ),
    ),
    ("takeover_interest", "Takeover interest", TAKEOVER_INTEREST_RE),
    ("offering", "Share offering", OFFERING_RE),
    (
        "earnings_beat",
        "Earnings beat",
        _rx(
            r"\b(?:beats?|tops?|exceeds?|surpass\w*|crush\w*)\b.{0,50}\b(?:estimates?|expectations|consensus|forecasts?|views?)\b|\brecord (?:quarterly )?(?:revenue|sales|results)\b"
        ),
    ),
    (
        "earnings_miss",
        "Earnings miss",
        _rx(
            r"\b(?:miss(?:es|ed)?|falls? short of|below)\b.{0,50}\b(?:estimates?|expectations|consensus|forecasts?|views?)\b"
        ),
    ),
    ("guidance_cut", "Guidance cut", GUIDE_DOWN_RE),
    ("guidance_raise", "Guidance raised", GUIDE_UP_RE),
    (
        "downgrade",
        "Analyst downgrade",
        _rx(r"\bdowngrade[sd]?\b|\bcut to (?:sell|underperform|underweight|neutral|hold)\b"),
    ),
    ("upgrade", "Analyst upgrade", _rx(r"\bupgrade[sd]?\b|\braised to (?:buy|outperform|overweight)\b")),
    (
        "probe",
        "Probe or lawsuit",
        _rx(
            r"\b(?:SEC|DOJ|FTC|justice department)\b.{0,40}\b(?:probe|investigat\w+|charges?|sues?|subpoena\w*|lawsuit)\b|\bantitrust (?:probe|suit|lawsuit|case)\b|\bsubpoena\w*\b|\bclass action\b|\bfraud\b"
        ),
    ),
    (
        "ceo_exit",
        "CEO exit",
        _rx(
            r"\b(?:CEO|chief executive)\b.{0,40}\b(?:resign\w*|steps? down|stepping down|depart\w*|ousted|exits?|retir\w+)\b|\b(?:resign\w*|steps? down) as (?:CEO|chief executive)\b"
        ),
    ),
    (
        "buyback",
        "Buyback",
        _rx(r"\bbuyback\b|\bshare repurchase\b|\brepurchase (?:program|authori[sz]ation)\b"),
    ),
    (
        "dividend_up",
        "Dividend raised",
        _rx(
            r"\b(?:raises?|increases?|hikes?|boosts?) (?:its )?(?:quarterly )?dividend\b|\bspecial dividend\b"
        ),
    ),
    (
        "dividend_cut",
        "Dividend cut",
        _rx(r"\b(?:cuts?|suspends?|slashes?|eliminates?) (?:its )?(?:quarterly )?dividend\b"),
    ),
    (
        "index_add",
        "Index inclusion",
        _rx(
            r"\b(?:join|joins|added to|to join|inclusion in|set to join)\b.{0,20}\b(?:S&P (?:500|MidCap 400|SmallCap 600)|Nasdaq-100)\b"
        ),
    ),
    (
        "layoffs",
        "Layoffs / restructuring",
        _rx(r"\blayoffs?\b|\bcut(?:s|ting)? [\d,]+ jobs\b|\bworkforce reduction\b|\brestructuring plan\b"),
    ),
    ("recall", "Recall", _rx(r"\brecall\w*\b")),
    (
        "contract",
        "Contract win",
        _rx(
            r"\b(?:awarded|wins?|won|secures?|lands?)\b.{0,50}\b(?:contract|order|award|deal)\b|\bcontract (?:award|win)\b|\bselected by\b"
        ),
    ),
    (
        "partnership",
        "Partnership",
        _rx(
            r"\bpartner(?:s|ship)? with\b|\bstrategic (?:partnership|collaboration|alliance)\b|\bcollaborat\w+ with\b|\bteams? up with\b"
        ),
    ),
]
LABELS = {k: lbl for k, lbl, _ in FAMILIES} | {
    "acquired": "Being acquired",
    "acquirer": "Making an acquisition",
    "insider_buy": "Insider buying",
    "halt": "Trading halt",
}
BANDS = (("small", "under $2B", 2e9), ("mid", "$2-10B", 10e9), ("large", "$10B+", float("inf")))
BAND_LABEL = {k: lbl for k, lbl, _ in BANDS} | {"*": "any size"}


def size_band(cap: float | None) -> str:
    if not cap:
        return "*"
    return next(k for k, _, top in BANDS if cap < top)


def classify(alert: dict[str, Any], universe: Any = None) -> dict[str, str] | None:
    """{key, label, band} for a news-like alert (headlines, insider buys, halts; a radar alert
    moving on a headline is read through that headline), or None when no catalyst is recognized."""
    kind = alert.get("kind")
    cat = alert_category(alert)
    item = alert.get("item") or {}
    extra = item.get("extra") or {}
    tickers = [t for t in alert.get("tickers") or [] if t]
    sym = (((alert.get("edge") or {}).get("play") or {}).get("direct") or tickers or [""])[0]
    cap = None
    if universe is not None and sym:
        li = universe.get(sym)
        cap = li.market_cap if li is not None else None
    if cap is None:
        cap = ((alert.get("edge") or {}).get("smallcap") or {}).get("market_cap")
    band = size_band(cap)

    def out(key: str) -> dict[str, str]:
        return {"key": key, "label": LABELS[key], "band": band}

    if cat == "insider" or extra.get("insider_buy"):
        return out("insider_buy")
    if extra.get("halt_code"):
        return out("halt")
    title = str(alert.get("title") or "")
    if kind == "price":  # a move: read the headline behind it, when there is one
        news = [r for r in alert.get("related") or [] if r.get("kind") == "news"]
        if not news:
            return None
        title = str(news[0].get("title") or "")
    elif kind not in ("news",):
        return None
    deal = (alert.get("analysis") or {}).get("deal") or {}
    if deal.get("target") and deal["target"] == sym:
        return out("acquired")
    if deal.get("buyer") and deal["buyer"] == sym:
        return out("acquirer")
    if ACQUIRED_RE.search(title) and not deal:
        return out("acquired")
    for key, _, rx in FAMILIES:
        if rx is not None and rx.search(title):
            return out(key)
    return None


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
        "median": round(st.median(xs), 2),
    }


def catalyst_rates(engine: Engine, days: float = 180, now: float | None = None) -> dict[str, Any]:
    """Graded alerts pooled per (catalyst, size band) and per catalyst: the hit rate and median
    move the alert's way an hour later, at the close and the next day, and the typical size of the
    close move either way."""
    now = now or time.time()
    pools: dict[tuple[str, str], dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(5000, since=now - days * 86400):
        out = (a.get("edge") or {}).get("outcome") or {}
        if not out:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        if not c:
            continue
        _sym, row = next(iter(out.items()))
        sign = row.get("sign") or 0
        if not sign:
            continue
        for key in ((c["key"], c["band"]), (c["key"], "*")):
            g = pools.setdefault(key, {"h1": [], "close": [], "d1": [], "abs": []})
            for k in ("h1", "close", "d1"):
                if row.get(k) is not None:
                    g[k].append(sign * row[k])
            if row.get("close") is not None:
                g["abs"].append(abs(row["close"]))
    rates = {}
    for (key, band), g in pools.items():
        rates[f"{key}|{band}"] = {
            "key": key,
            "label": LABELS.get(key, key),
            "band": band,
            "band_label": BAND_LABEL[band],
            "h1": _stats(g["h1"]),
            "close": _stats(g["close"]),
            "d1": _stats(g["d1"]),
            "typical": round(st.median(g["abs"]), 2) if g["abs"] else None,
        }
    return rates


def catalyst_base(rates: dict[str, Any], c: dict[str, str] | None) -> dict[str, Any] | None:
    """The record for this catalyst at this size, or for the catalyst at any size when the band
    has fewer than MIN_N graded closes."""
    if not c:
        return None
    for band in (c["band"], "*"):
        r = rates.get(f"{c['key']}|{band}")
        if r and r.get("close") and r["close"]["n"] >= MIN_N:
            return r
    return None


def cat_line(cb: dict[str, Any] | None) -> str:
    """One line for a push or a card: "Contract win, $2-10B: 64% went their way by the close (median +2.8%, 31)"."""
    if not cb or not cb.get("close"):
        return ""
    c = cb["close"]
    return (
        f"{cb['label']}, {cb['band_label']}: {round(c['hit'] * 100)}% went their way by the close "
        f"(median {c['median']:+.1f}%, {c['n']})"
    )


def catalyst_table(rates: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """The record per catalyst (any size), most-graded first, for the track record page."""
    rows = [r for k, r in rates.items() if k.endswith("|*") and r.get("close")]
    rows.sort(key=lambda r: -r["close"]["n"])
    for r in rows:
        r["bands"] = [
            {"band": b, "band_label": BAND_LABEL[b], "close": rates[f"{r['key']}|{b}"]["close"]}
            for b, _, _ in BANDS
            if f"{r['key']}|{b}" in rates and rates[f"{r['key']}|{b}"].get("close")
        ]
    return rows[:limit]


def room_left(engine: Engine, now: float | None = None, limit: int = 12) -> dict[str, Any]:
    """Today's alerts whose catalyst has a record (6+ graded, went its way at least half the time,
    median close move its way positive) and which have moved less, its way, than that median so
    far: where the history says the typical move hasn't happened yet. History, not a forecast."""
    from .dossier import _direction, _since

    now = now or time.time()
    rates = getattr(engine, "catalyst_rates", None) or {}
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows, seen = [], set()
    for a in engine.storage.recent_alerts(400, since=day0):
        if a.get("severity") not in ("MEDIUM", "HIGH", "CRITICAL"):
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        cb = catalyst_base(rates, c)
        if not cb or cb["close"]["hit"] < 0.5 or cb["close"]["median"] <= 0:
            continue
        d = _direction(a)
        if not d:
            continue
        sign = 1 if d == "up" else -1
        sym = (((a.get("edge") or {}).get("play") or {}).get("direct") or a.get("tickers") or [""])[0]
        if not sym or sym in seen:
            continue
        since = _since(a, sym, engine.price_of)
        if since is None:
            continue
        its_way = sign * since
        typical = cb["close"]["median"]
        room = round(typical - its_way, 2)
        if room < 0.5 or its_way < -typical:  # already done, or broken the other way
            continue
        seen.add(sym)
        pre = ((a.get("edge") or {}).get("pre") or {}).get(sym)
        rows.append({"id": a["id"], "symbol": sym, "title": a.get("title", ""), "severity": a.get("severity"),
                     "created": a.get("created"), "direction": d, "catalyst": cb["label"], "band": cb["band_label"],
                     "n": cb["close"]["n"], "hit": cb["close"]["hit"], "typical": typical, "since": round(its_way, 2),
                     "room": room, "pre": pre})  # fmt: skip
    rows.sort(key=lambda r: -(r["room"] * r["hit"]))
    return {"rows": rows[:limit], "rated": sum(1 for k in rates if k.endswith("|*"))}


def catalyst_kinds(rates: dict[str, Any]) -> list[dict[str, Any]]:
    """Every kind of news Foretape reads, with its record at any size when it has one: the chips
    a phone turns off, and what an edge bar would let through."""
    out = []
    for key, label in LABELS.items():
        r = rates.get(f"{key}|*") or {}
        out.append({"key": key, "label": label, "close": r.get("close"), "typical": r.get("typical")})
    return out


def catalyst_taste(engine: Engine, device: str) -> list[dict[str, Any]]:
    """This phone's votes by kind of news: a kind voted down three times or more and never up is
    suggested for turning off."""
    p = engine.desk.profiles.get(device) or {}
    counts: dict[str, dict[str, Any]] = {}
    for aid, v in list((p.get("votes") or {}).items())[-300:]:
        a = engine.storage.get_alert(aid)
        if not a:
            continue
        c = (a.get("edge") or {}).get("catalyst") or classify(a, engine.universe)
        if not c:
            continue
        row = counts.setdefault(c["key"], {"key": c["key"], "label": c["label"], "up": 0, "down": 0})
        row["up" if v > 0 else "down"] += 1
    off = set(p.get("cat_off") or [])
    rows = sorted(counts.values(), key=lambda r: -(r["up"] + r["down"]))
    for r in rows:
        r["suggest_off"] = r["down"] >= 3 and r["up"] == 0 and r["key"] not in off
    return rows
