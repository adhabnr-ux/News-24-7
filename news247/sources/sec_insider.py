"""SEC Form 4: insiders buying their own stock on the open market, the hour it is filed.

Executives sell for a hundred reasons (taxes, houses, diversification) and buy for one. An
open-market purchase (transaction code P) by a CEO, CFO or director, especially a large one
against the company's size or several insiders buying the same week, is one of the few public
signals of what the people with the best information think. Form 4 must be filed within two
business days of the trade, and EDGAR shows it the minute it is accepted.

Each poll reads EDGAR's current Form 4 list, then the filing itself (the XML ownership
document inside the submission text file) for every new filing whose issuer has a ticker.
Only purchases are reported: grants, option exercises, tax withholding and sales are not.

Options: ``min_value`` (default $250,000), ``count`` (filings per poll, default 100),
``max_fetch`` (filings read per poll, default 40).
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from ..models import NewsItem, SourceTier
from ..util import strip_html, struct_to_ts
from .rss import parse_feed
from .sec_edgar import _ACC_RE, _TITLE_RE, CURRENT_URL, SECEdgarSource

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
_XML_RE = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)
CLUSTER_WINDOW_S = 7 * 86400


@dataclass
class Purchase:
    issuer: str
    ticker: str
    cik: str
    owner: str
    role: str  # "CEO", "Director", "10% owner", ...
    shares: float = 0.0
    value: float = 0.0
    low: float = 0.0
    high: float = 0.0
    dates: list[str] = field(default_factory=list)

    @property
    def avg_price(self) -> float:
        return self.value / self.shares if self.shares else 0.0


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    el = node.find(path)
    if el is None:
        return ""
    v = el.find("value")
    return ((v.text if v is not None else el.text) or "").strip()


def _num(s: str) -> float:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return 0.0


def _role(owner: ET.Element) -> str:
    rel = owner.find("reportingOwnerRelationship")
    title = _text(rel, "officerTitle")
    if title:
        t = title.lower()
        if "chief executive" in t or re.search(r"\bceo\b", t):
            return "CEO"
        if "chief financial" in t or re.search(r"\bcfo\b", t):
            return "CFO"
        if "president" in t:
            return "President"
        if "chair" in t:
            return "Chair"
        return title.strip()[:40]
    if _text(rel, "isDirector") in ("1", "true"):
        return "Director"
    if _text(rel, "isTenPercentOwner") in ("1", "true"):
        return "10% owner"
    if _text(rel, "isOfficer") in ("1", "true"):
        return "Officer"
    return "Insider"


def parse_form4(doc: str) -> Purchase | None:
    """The open-market purchases in one Form 4 (code P, acquired), summed. None when there are
    none (sales, grants, exercises, gifts and tax withholding are ignored)."""
    m = _XML_RE.search(doc)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return None
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")
    if issuer is None or owner is None:
        return None
    buy = Purchase(
        issuer=_text(issuer, "issuerName"),
        ticker=_text(issuer, "issuerTradingSymbol").upper(),
        cik=_text(issuer, "issuerCik").zfill(10),
        owner=_text(owner.find("reportingOwnerId"), "rptOwnerName"),
        role=_role(owner),
    )
    prices: list[float] = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _text(tx, "transactionCoding/transactionCode") != "P":
            continue
        if _text(tx, "transactionAmounts/transactionAcquiredDisposedCode") not in ("A", ""):
            continue
        shares = _num(_text(tx, "transactionAmounts/transactionShares"))
        price = _num(_text(tx, "transactionAmounts/transactionPricePerShare"))
        if shares <= 0 or price <= 0:
            continue
        buy.shares += shares
        buy.value += shares * price
        prices.append(price)
        d = _text(tx, "transactionDate")
        if d and d not in buy.dates:
            buy.dates.append(d)
    if not prices:
        return None
    buy.low, buy.high = min(prices), max(prices)
    return buy


def _owner_name(raw: str) -> str:
    """EDGAR writes owners "Last First Middle"; show "First Last"."""
    parts = raw.replace(",", " ").split()
    if len(parts) >= 2 and raw.isupper():
        return " ".join(p.title() for p in [*parts[1:], parts[0]])
    return raw.title() if raw.isupper() else raw


def _money(v: float) -> str:
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K"


class SECInsiderSource(SECEdgarSource):
    type_name = "sec_insider"
    default_interval = 30.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        cfg = {"forms": ["4"], **cfg}
        super().__init__(cfg, ctx)
        self.count = int(cfg.get("count", 100))
        self.min_value = float(cfg.get("min_value", 250_000))
        self.max_fetch = int(cfg.get("max_fetch", 40))
        self._done: dict[str, float] = {}  # accession -> when read
        self._recent: dict[str, list[tuple[float, str]]] = {}  # ticker -> [(when, owner)] buys
        self._primed = False

    async def fetch(self) -> list[NewsItem]:
        await self._load_tickers()
        url = CURRENT_URL.format(form="4", count=self.count)
        resp = await self.ctx.http.get(url, headers=self.sec_headers, conditional=True)
        if resp.not_modified:
            return []
        filings = self.filings(resp.body)
        if not self._primed:  # start-up: today's earlier filings are history, not news
            self._primed = True
            for acc, *_ in filings:
                self._done[acc] = time.time()
            return []
        out: list[NewsItem] = []
        for acc, cik, _issuer, updated in filings[: self.max_fetch]:
            if acc in self._done:
                continue
            self._done[acc] = time.time()
            try:
                body = await self.ctx.http.get(
                    ARCHIVE_URL.format(cik=int(cik), acc_nodash=acc.replace("-", ""), acc=acc),
                    headers=self.sec_headers,
                )
            except Exception as exc:  # noqa: BLE001 - one unreadable filing must not stop the rest
                log.info("[%s] Form 4 %s unreadable: %s", self.name, acc, exc)
                continue
            item = self.purchase_item(body.text(), acc, updated)
            if item is not None:
                out.append(item)
        if len(self._done) > 5000:
            for k in list(self._done)[:2000]:
                del self._done[k]
        return out

    def filings(self, body: bytes) -> list[tuple[str, str, str, float | None]]:
        """[(accession, issuer CIK, issuer name, updated)] for Form 4s whose issuer has a ticker."""
        out: list[tuple[str, str, str, float | None]] = []
        seen: set[str] = set()
        for e in parse_feed(body).entries:
            m = _TITLE_RE.match(strip_html(e.get("title"), None))
            acc = _ACC_RE.search(e.get("id", ""))
            if not m or not acc or m.group("role").lower() != "issuer":
                continue
            cik = m.group("cik")
            if acc.group(1) in seen or (self.only_tickers and cik not in self._cik_to_ticker):
                continue
            seen.add(acc.group(1))
            updated = struct_to_ts(e.get("updated_parsed"))
            out.append((acc.group(1), cik, m.group("company").title(), updated))
        return out

    def purchase_item(self, doc: str, acc: str, published: float | None = None) -> NewsItem | None:
        buy = parse_form4(doc)
        if buy is None or buy.value < self.min_value:
            return None
        ticker = buy.ticker or self._cik_to_ticker.get(buy.cik, "")
        now = time.time()
        hist = [(t, o) for t, o in self._recent.get(ticker, []) if now - t < CLUSTER_WINDOW_S]
        others = {o for _, o in hist if o != buy.owner}
        hist.append((now, buy.owner))
        self._recent[ticker] = hist[-20:]
        cap = None
        uni = getattr(self.ctx, "universe", None)
        li = uni.get(ticker) if uni is not None and ticker else None
        if li is not None:
            cap = li.market_cap
        share = buy.value / cap if cap else None
        boost = 8.0
        if buy.role in ("CEO", "CFO", "President", "Chair"):
            boost += 6
        if buy.value >= 1e6:
            boost += 6
        if share is not None:
            boost += 15 if share >= 0.01 else 10 if share >= 0.002 else 4 if share >= 0.0005 else 0
        if others:
            boost += 10
        name = _owner_name(buy.owner)
        price = f"${buy.avg_price:,.2f}" if buy.low == buy.high else f"${buy.low:,.2f}-${buy.high:,.2f}"
        tick = f" ({ticker})" if ticker else ""
        title = (
            f"{buy.role} {name} buys {_money(buy.value)} of {buy.issuer.title()}{tick} stock "
            f"at {price} (Form 4, open market)"
        )
        bits = [f"{buy.shares:,.0f} shares on {', '.join(buy.dates[:3])}"]
        if share is not None:
            bits.append(f"{share * 100:.2f}% of the company's ${cap / 1e9:,.2f}B value" if cap else "")
        if others:
            bits.append(
                f"cluster: also bought this week by {', '.join(sorted(_owner_name(o) for o in others)[:3])}"
            )
        item = self.make_item(
            title=title,
            url=f"https://www.sec.gov/Archives/edgar/data/{int(buy.cik)}/{acc.replace('-', '')}/",
            summary=" · ".join(b for b in bits if b),
            published=published,
            uid=f"sec4:{acc}",
            tickers=[ticker] if ticker else [],
            author=name,
        )
        item.extra.update(
            {
                "form": "4",
                "insider_buy": {
                    "owner": name,
                    "role": buy.role,
                    "value": round(buy.value),
                    "shares": buy.shares,
                    "avg_price": round(buy.avg_price, 4),
                    "share_of_cap": round(share, 6) if share is not None else None,
                    "cluster": sorted(others),
                },
            }
        )
        item.extra["boost"] = item.extra.get("boost", 0) + boost
        return item
