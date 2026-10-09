"""SEC EDGAR "latest filings" feed — 8-Ks hit EDGAR before most newswires repeat them."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..models import NewsItem, SourceTier
from ..util import strip_html, struct_to_ts
from .base import PollingSource
from .rss import parse_feed

log = logging.getLogger(__name__)

CURRENT_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb="
    "&owner=include&start=0&count={count}&output=atom"
)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# 8-K item number -> (short label, score boost). Boosts reflect how often the item moves a stock.
ITEM_INFO: dict[str, tuple[str, float]] = {
    "1.01": ("Material Agreement", 14),
    "1.02": ("Agreement Terminated", 14),
    "1.03": ("BANKRUPTCY / Receivership", 45),
    "1.05": ("Cybersecurity Incident", 25),
    "2.01": ("Acquisition/Disposition Completed", 18),
    "2.02": ("Results of Operations", 14),
    "2.03": ("New Material Debt", 6),
    "2.04": ("Debt Acceleration Triggered", 25),
    "2.05": ("Exit/Restructuring Costs", 14),
    "2.06": ("Material Impairment", 18),
    "3.01": ("Delisting Notice", 28),
    "3.02": ("Unregistered Equity Sale", 6),
    "3.03": ("Shareholder Rights Modified", 8),
    "4.01": ("Auditor Change", 15),
    "4.02": ("NON-RELIANCE on Financials (restatement)", 40),
    "5.01": ("Change in Control", 30),
    "5.02": ("Officer/Director Change", 12),
    "5.03": ("Bylaws Amended", 2),
    "5.07": ("Shareholder Vote Results", 0),
    "7.01": ("Reg FD Disclosure", 5),
    "8.01": ("Other Events", 6),
    "9.01": ("Exhibits", 0),
}

# Forms where one company files about another (stakes, tender offers, merger communications).
# EDGAR lists the target as "(Subject)" and the holder/bidder as "(Filed by)" - paired below.
# Boost: an activist 13D or a tender offer moves the target; passive 13G stakes rarely do.
PAIRED_FORMS: dict[str, tuple[str, str, float]] = {  # form: (verb with a filer, noun without, boost)
    "SCHEDULE 13D": ("discloses stake in", "stake disclosure", 15),
    "SCHEDULE 13D/A": ("updates stake in", "stake update", 6),
    "SC 13D": ("discloses stake in", "stake disclosure", 15),
    "SC 13D/A": ("updates stake in", "stake update", 6),
    "SCHEDULE 13G": ("discloses stake in", "stake disclosure", 4),
    "SCHEDULE 13G/A": ("updates stake in", "stake update", 0),
    "SC 13G": ("discloses stake in", "stake disclosure", 4),
    "SC 13G/A": ("updates stake in", "stake update", 0),
    "SC TO-T": ("launches tender offer for", "tender offer", 25),
    "SC TO-T/A": ("amends tender offer for", "tender offer amended", 6),
    "SC 14D9": ("responds to tender offer for", "response to tender offer", 12),
    "425": ("files merger communication about", "merger communication", 10),
}

_TITLE_RE = re.compile(
    r"^(?P<form>[^ ]+(?: [^ ]+)?) - (?P<company>.+?) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)"
)
_ITEM_RE = re.compile(r"Item (\d\.\d\d)")
_ACC_RE = re.compile(r"accession-number=([\d-]+)")


class SECEdgarSource(PollingSource):
    """Polls EDGAR's current-filings Atom feed for the configured form types.

    SEC requires a descriptive User-Agent with a contact e-mail (set ``general.user_agent``)
    and allows at most 10 requests/second; the HTTP client spaces sec.gov calls for you.

    Options: ``forms`` (default ["8-K"]), ``only_tickers`` (skip filers with no exchange
    ticker, default true), ``min_boost`` (skip filings whose items are all below this).
    """

    type_name = "sec_edgar"
    default_interval = 10.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.forms: list[str] = list(cfg.get("forms", ["8-K"]))
        self.count = int(cfg.get("count", 40))
        self.only_tickers = bool(cfg.get("only_tickers", True))
        self.min_boost = float(cfg.get("min_boost", 0))
        self._cik_to_ticker: dict[str, str] = {}
        self._tickers_loaded_at = 0.0

    @property
    def sec_headers(self) -> dict[str, str]:
        # SEC rejects browser-like/anonymous agents; it wants "Company/App contact@email"
        return {"User-Agent": self.ctx.user_agent, **self.extra_headers}

    async def _load_tickers(self) -> None:
        if self._cik_to_ticker and time.time() - self._tickers_loaded_at < 86400:
            return
        cache = (self.ctx.data_dir / "sec_company_tickers.json") if self.ctx.data_dir else None
        raw: dict[str, Any] | None = None
        if cache and cache.is_file() and time.time() - cache.stat().st_mtime < 86400:
            try:
                raw = json.loads(cache.read_text())
            except ValueError:
                raw = None
        if raw is None:
            try:
                resp = await self.ctx.http.get(TICKERS_URL, headers=self.sec_headers)
                raw = resp.json()
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(resp.body)
            except Exception as exc:  # noqa: BLE001 - mapping is a nice-to-have
                log.warning("[%s] could not load SEC ticker map: %s", self.name, exc)
                self._tickers_loaded_at = time.time() - 86400 + 600  # retry in 10 min
                return
        mapping: dict[str, str] = {}
        for row in (raw or {}).values():
            cik = str(row.get("cik_str", "")).zfill(10)
            mapping.setdefault(cik, str(row.get("ticker", "")).upper())
        self._cik_to_ticker = mapping
        self._tickers_loaded_at = time.time()

    def load_ticker_map(self, mapping: dict[str, str]) -> None:
        """Inject a CIK->ticker map (tests, offline use)."""
        self._cik_to_ticker = {k.zfill(10): v for k, v in mapping.items()}
        self._tickers_loaded_at = time.time()

    async def fetch(self) -> list[NewsItem]:
        await self._load_tickers()
        items: list[NewsItem] = []
        for form in self.forms:
            url = CURRENT_URL.format(form=form.replace(" ", "+"), count=self.count)
            resp = await self.ctx.http.get(url, headers=self.sec_headers, conditional=True)
            if resp.not_modified:
                continue
            items.extend(self.parse(resp.body))
        return items

    def parse(self, body: bytes) -> list[NewsItem]:
        parsed = parse_feed(body)
        # who filed each paired form (13D/13G/tender offer): accession -> filer name
        filed_by: dict[str, str] = {}
        for e in parsed.entries:
            m = _TITLE_RE.match(strip_html(e.get("title"), None))
            acc = _ACC_RE.search(e.get("id", ""))
            if m and acc and m.group("role").lower() in ("filed by", "reporting"):
                filed_by.setdefault(acc.group(1), m.group("company").title())
        out: list[NewsItem] = []
        for e in parsed.entries:
            item = self.entry_to_item(e, filed_by)
            if item is not None:
                out.append(item)
        return out

    def entry_to_item(self, e: Any, filed_by: dict[str, str] | None = None) -> NewsItem | None:
        raw_title = strip_html(e.get("title"), None)
        m = _TITLE_RE.match(raw_title)
        if not m:
            return None
        if m.group("role").lower() not in ("filer", "subject", "subject company"):
            return None
        form, company, cik = m.group("form"), m.group("company").title(), m.group("cik")
        acc = _ACC_RE.search(e.get("id", ""))
        if form.upper() in PAIRED_FORMS:
            return self._paired_item(e, form, company, cik, (filed_by or {}).get(acc.group(1) if acc else ""))
        ticker = self._cik_to_ticker.get(cik, "")
        if self.only_tickers and not ticker:
            return None
        summary_raw = e.get("summary") or ""
        items = list(dict.fromkeys(_ITEM_RE.findall(summary_raw)))
        boost = sum(ITEM_INFO.get(i, ("", 0))[1] for i in items)
        if items and boost < self.min_boost:
            return None
        labels = [f"{i} {ITEM_INFO[i][0]}" for i in items if i in ITEM_INFO and ITEM_INFO[i][1] > 0]
        tick = f" ({ticker})" if ticker else ""
        title = f"{company}{tick} files {form}" + (f": {'; '.join(labels)}" if labels else "")
        item = self.make_item(
            title=title,
            url=e.get("link", ""),
            summary=strip_html(summary_raw),
            published=struct_to_ts(e.get("updated_parsed")) or struct_to_ts(e.get("published_parsed")),
            uid=f"sec:{acc.group(1)}" if acc else "",
            tickers=[ticker] if ticker else [],
            author=company,
        )
        item.extra.update({"form": form, "cik": cik, "sec_items": items})
        item.extra["boost"] = item.extra.get("boost", 0) + boost
        return item

    def _paired_item(self, e: Any, form: str, company: str, cik: str, filer: str | None) -> NewsItem | None:
        """'SCHEDULE 13G: Nvidia Corp discloses stake in Nebius Group N.V. (NBIS)', or without the
        filer's row 'Utz Brands, Inc. (UTZ): SC TO-T tender offer'."""
        ticker = self._cik_to_ticker.get(cik, "")
        if self.only_tickers and not ticker:
            return None
        verb, noun, boost = PAIRED_FORMS[form.upper()]
        tick = f" ({ticker})" if ticker else ""
        title = f"{form}: {filer} {verb} {company}{tick}" if filer else f"{company}{tick}: {form} {noun}"
        acc = _ACC_RE.search(e.get("id", ""))
        item = self.make_item(
            title=title,
            url=e.get("link", ""),
            summary=strip_html(e.get("summary") or ""),
            published=struct_to_ts(e.get("updated_parsed")) or struct_to_ts(e.get("published_parsed")),
            uid=f"sec:{acc.group(1)}" if acc else "",
            tickers=[ticker] if ticker else [],
            author=filer or company,
        )
        item.extra.update({"form": form, "cik": cik, "filed_by": filer or "", "sec_items": []})
        item.extra["boost"] = item.extra.get("boost", 0) + boost
        return item
