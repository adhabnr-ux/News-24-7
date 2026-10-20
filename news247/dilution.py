"""Dilution risk, from the company's own filings, graded.

A small company whose stock runs on news can sell shares into the run. It does that from a shelf
registration (S-3, F-3) with prospectus supplements (424B), often within days. That ends many
small-cap runs, and it was never on the alerts. The company's filings say whether it can:

- **Selling now:** a 424B prospectus filed in the last 180 days (shares are being sold off a shelf).
- **Shelf on file:** a shelf registration (S-3, S-3ASR, F-3) in the last three years, with no recent
  sale.
- **No shelf:** neither, filings read.

Each news alert's stock carries its state, read from SEC EDGAR's submissions API (data.sec.gov),
kept a day. A stock not read yet is read in the background and its alert stamped when the filings
arrive. Graded by the close and five sessions on, its way, for every kind of news and each kind,
the record says whether news on a stock selling shares holds. An alert carries its state's record,
and a weak (40% or less) or strong (60%+) one counts in the call. When the filings can't be read,
nothing is stamped: no guess.
"""

from __future__ import annotations

import json
import statistics as st
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TTL = 86400
MIN_N = 8
SHELF_FORMS = ("S-3", "S-3ASR", "F-3", "F-3ASR")
STATES = (("selling", "selling shares now"), ("shelf", "a shelf on file"), ("none", "no shelf"))
LABEL = dict(STATES)


def state_of(doc: Any, today: date) -> dict[str, Any] | None:
    """EDGAR submissions JSON -> {"state", "shelf": date|None, "sales": n in 180 days, "last_sale": date|None}."""
    recent = (((doc or {}).get("filings") or {}).get("recent")) or {}
    forms, dates = recent.get("form") or [], recent.get("filingDate") or []
    if not forms or len(forms) != len(dates):
        return None
    shelf = None
    sales = []
    for f, d in zip(forms, dates, strict=True):
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        if f in SHELF_FORMS and today - day <= timedelta(days=3 * 365) and (shelf is None or day > shelf):
            shelf = day
        if f.startswith("424B") and today - day <= timedelta(days=180):
            sales.append(day)
    st_ = "selling" if sales else "shelf" if shelf else "none"
    return {"state": st_, "shelf": shelf.isoformat() if shelf else None, "sales": len(sales),
            "last_sale": max(sales).isoformat() if sales else None}  # fmt: skip


async def cik_map(engine: Engine) -> dict[str, str]:
    """{TICKER: 10-digit CIK} from the SEC's ticker file, kept a day (shared with the EDGAR source's cache)."""
    now = time.time()
    raw = engine.storage.get_setting("sec:tickers")
    if raw:
        d = json.loads(raw)
        if now - d.get("ts", 0) < TTL:
            return d["map"]
    resp = await engine.http.get(TICKERS_URL, headers={"User-Agent": engine.cfg.general.user_agent})
    m = {
        str(r.get("ticker", "")).upper(): str(r.get("cik_str", "")).zfill(10)
        for r in (resp.json() or {}).values()
    }
    engine.storage.set_setting("sec:tickers", json.dumps({"ts": now, "map": m}))
    return m


def cached(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any] | None:
    now = now or time.time()
    raw = engine.storage.get_setting(f"dilution:{sym}")
    if not raw:
        return None
    d = json.loads(raw)
    return d if now - d.get("ts", 0) < TTL else None


async def fetch(engine: Engine, sym: str, now: float | None = None) -> dict[str, Any] | None:
    """Read the company's filings and keep its state a day. None when they can't be read."""
    now = now or time.time()
    try:
        cik = (await cik_map(engine)).get(sym.upper().replace(".", "-"))
        if not cik:
            return None
        resp = await engine.http.get(
            SUBMISSIONS.format(cik=cik), headers={"User-Agent": engine.cfg.general.user_agent}
        )
        d = state_of(resp.json(), date.fromtimestamp(now))
    except Exception:  # noqa: BLE001 - no filings: nothing is stamped
        return None
    if d:
        d["ts"] = now
        engine.storage.set_setting(f"dilution:{sym}", json.dumps(d))
    return d


def stamp_of(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if not d or not d.get("state"):
        return None
    return {k: d.get(k) for k in ("state", "shelf", "sales", "last_sale")}


async def stamp_later(engine: Engine, alert_id: str, sym: str, kind: str | None) -> None:
    d = stamp_of(await fetch(engine, sym))
    if d:
        patch: dict[str, Any] = {"dilution": d}
        base = dilution_for(getattr(engine, "dilution_rates", None) or {}, kind, d)
        if base:
            patch["dilution_base"] = base
        engine.storage.update_alert_edge(alert_id, patch)


def dilution_rates(engine: Engine, days: float = 365, now: float | None = None) -> dict[str, Any]:
    """{"state|kind": {"key", "state", "kind", "label", "close": stats, "d5": stats}}."""
    from .catalysts import LABELS, classify

    now = now or time.time()
    pools: dict[str, dict[str, list[float]]] = {}
    for a in engine.storage.recent_alerts(8000, since=now - days * 86400):
        e = a.get("edge") or {}
        dl = (e.get("dilution") or {}).get("state")
        o = e.get("outcome") or {}
        if not dl or not o or a.get("kind") == "system":
            continue
        row = next(iter(o.values()))
        if not row.get("sign"):
            continue
        c = e.get("catalyst") or classify(a, engine.universe)
        for kind in ("*", c["key"]) if c else ("*",):
            p = pools.setdefault(f"{dl}|{kind}", {"close": [], "d5": []})
            for h in ("close", "d5"):
                if row.get(h) is not None:
                    p[h].append(row["sign"] * row[h])
    out = {}
    for key, p in pools.items():
        if len(p["close"]) < MIN_N:
            continue
        state, kind = key.split("|")
        who = "News" if kind == "*" else LABELS.get(kind, kind)

        def stats(xs: list[float]) -> dict[str, Any] | None:
            if len(xs) < MIN_N:
                return None
            return {
                "n": len(xs),
                "hit": round(sum(x > 0 for x in xs) / len(xs), 2),
                "median": round(st.median(xs), 2),
            }

        out[key] = {"key": key, "state": state, "kind": kind, "label": f"{who} on a stock with {LABEL[state]}",
                    "close": stats(p["close"]), "d5": stats(p["d5"])}  # fmt: skip
    return out


def dilution_for(rates: dict[str, Any], kind: str | None, d: dict[str, Any] | None) -> dict[str, Any] | None:
    if not d or not d.get("state"):
        return None
    for k in (f"{d['state']}|{kind}" if kind else None, f"{d['state']}|*"):
        if k and k in rates:
            return rates[k]
    return None


def board(rates: dict[str, Any]) -> dict[str, Any]:
    rows = [rates[f"{s}|*"] for s, _ in STATES if f"{s}|*" in rates]
    kinds = sorted((r for r in rates.values() if r["kind"] != "*"), key=lambda r: -r["close"]["n"])[:12]
    return {"rows": rows, "kinds": kinds, "need": MIN_N}
