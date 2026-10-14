"""SEC Form 4: insiders buying their own stock on the open market, the minute it is filed.

Executives sell for a hundred reasons (taxes, houses, diversification) and buy for one. An
open-market purchase (transaction code P) is one of the few public signals of what the people
with the best information think. Form 4 must be filed within two business days of the trade,
and EDGAR shows it the minute it is accepted.

What makes a purchase worth acting on (the research the boosts below follow):

* **Opportunistic, not routine.** Cohen, Malloy and Pomorski ("Decoding Inside Information",
  Journal of Finance 2012): about half of insider trades come from "routine" traders who trade
  in the same calendar month year after year, and they predict nothing. Trades by everyone else
  ("opportunistic") earned 82 basis points a month value-weighted (180 equal-weighted) and
  predicted firm news. Each buy is classified from the insider's own EDGAR history: a trade
  (code P or S) in the same calendar month in each of the three previous years is routine.
* **Not on autopilot.** Since April 2023 Form 4 carries a Rule 10b5-1 box (``aff10b5One``):
  a purchase made under a plan set up months earlier says little about today.
* **Purchases, small companies, several insiders, senior insiders, big money.** Lakonishok and
  Lee (Review of Financial Studies 2001): the information is in purchases, not sales, and mostly
  in smaller companies; Seyhun (1986, 1998): larger trades by more senior insiders say more.
  Several insiders buying the same week is a cluster.
* **Size against their own stake.** A CEO adding 2% to a large holding is housekeeping; one
  doubling it is a statement. Read from ``sharesOwnedFollowingTransaction``.
* **Really open-market.** Code P also covers shares bought in the company's own offering, a
  private placement, a rights offering or an employee purchase plan. When the filing's
  footnotes say so, it is not an insider buy and is dropped.

Each poll reads EDGAR's current Form 4 list (conditional GET), then every new filing whose
issuer has a ticker, several at a time. A purchase of ``min_value``+ is then placed in context:
the insider's history (data.sec.gov submissions, at most a few filings read), the price now
against what the insider paid, and how far the stock is from its 3-month high.

Options: ``min_value`` (default $250,000), ``count`` (filings per poll, default 100),
``max_fetch`` (filings read per poll, default 100), ``concurrency`` (default 4),
``history`` (look up routine/opportunistic, default true), ``history_reads`` (old filings read
per insider at most, default 6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..models import NewsItem, SourceTier
from ..util import strip_html, struct_to_ts
from .rss import parse_feed
from .sec_edgar import _ACC_RE, _TITLE_RE, CURRENT_URL, SECEdgarSource

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"
_XML_RE = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)
CLUSTER_WINDOW_S = 7 * 86400
HISTORY_TTL_S = 7 * 86400
# footnotes that make a code-P purchase something other than an insider buying in the market
_NOT_MARKET = re.compile(
    r"\b(?:public offering|underwritten (?:offering|public)|initial public offering|in the (?:company's |issuer's )?ipo|"
    r"private placement|registered direct|rights offering|subscription agreement|pipe financing|"
    r"securities purchase agreement|dividend reinvestment|drip\b|employee stock purchase|espp\b|"
    r"directed share program|concurrent private)",
    re.I,
)
_PLAN = re.compile(r"10b5-1", re.I)


@dataclass
class Purchase:
    issuer: str
    ticker: str
    cik: str
    owner: str
    role: str  # "CEO", "Director", "10% owner", ...
    owner_cik: str = ""
    shares: float = 0.0
    value: float = 0.0
    low: float = 0.0
    high: float = 0.0
    dates: list[str] = field(default_factory=list)
    before: float | None = None  # shares held (same holding line) before the first purchase
    after: float | None = None  # ... and after the last
    plan: bool = False  # made under a Rule 10b5-1 plan (the box, or a footnote saying so)
    offering: str = ""  # the footnote wording when the "purchase" was an offering, not the market

    @property
    def avg_price(self) -> float:
        return self.value / self.shares if self.shares else 0.0

    @property
    def stake_increase(self) -> float | None:
        """Purchased shares / shares held before, on the holding line the purchases were made in.
        None when the filing doesn't say; inf for a first position."""
        if self.before is None or self.after is None:
            return None
        bought = self.after - self.before
        if bought <= 0:
            return None
        return float("inf") if self.before <= 0 else bought / self.before


@dataclass
class InsiderContext:
    """What a purchase means, beyond the filing itself (looked up after it is read)."""

    pattern: str = ""  # "opportunistic" | "routine" | "new" (under 3 years of history) | ""
    pattern_note: str = ""
    price_now: float | None = None
    price_source: str = ""
    off_high_pct: float | None = None  # % below the 3-month high (negative = below)


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


def _ownership_xml(doc: str) -> ET.Element | None:
    m = _XML_RE.search(doc)
    if not m:
        return None
    try:
        return ET.fromstring(m.group(0))
    except ET.ParseError:
        return None


def _footnotes(root: ET.Element) -> dict[str, str]:
    return {
        fn.get("id", ""): " ".join("".join(fn.itertext()).split())
        for fn in root.findall("footnotes/footnote")
        if fn.get("id")
    }


def open_market_codes(doc: str) -> set[str]:
    """The non-derivative transaction codes in a Form 4 ("P", "S", "A", "M", ...)."""
    root = _ownership_xml(doc)
    if root is None:
        return set()
    return {
        _text(tx, "transactionCoding/transactionCode")
        for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction")
    } - {""}


def parse_form4(doc: str) -> Purchase | None:
    """The open-market purchases in one Form 4 (code P, acquired), summed. None when there are
    none (sales, grants, exercises, gifts and tax withholding are ignored)."""
    root = _ownership_xml(doc)
    if root is None:
        return None
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")
    if issuer is None or owner is None:
        return None
    oid = owner.find("reportingOwnerId")
    buy = Purchase(
        issuer=_text(issuer, "issuerName"),
        ticker=_text(issuer, "issuerTradingSymbol").upper(),
        cik=_text(issuer, "issuerCik").zfill(10),
        owner=_text(oid, "rptOwnerName"),
        owner_cik=_text(oid, "rptOwnerCik").zfill(10) if _text(oid, "rptOwnerCik") else "",
        role=_role(owner),
        plan=(root.findtext("aff10b5One") or "").strip().lower() in ("1", "true"),
    )
    notes = _footnotes(root)
    prices: list[float] = []
    lines: dict[str, list[float | None]] = {}  # D / I -> [before, after]
    offering: list[str] = []
    market = 0
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _text(tx, "transactionCoding/transactionCode") != "P":
            continue
        if _text(tx, "transactionAmounts/transactionAcquiredDisposedCode") not in ("A", ""):
            continue
        shares = _num(_text(tx, "transactionAmounts/transactionShares"))
        price = _num(_text(tx, "transactionAmounts/transactionPricePerShare"))
        if shares <= 0 or price <= 0:
            continue
        said = " ".join(notes.get(f.get("id", ""), "") for f in tx.iter("footnoteId"))
        m = _NOT_MARKET.search(said)
        if m:
            offering.append(m.group(0))
            continue
        if _PLAN.search(said):
            buy.plan = True
        market += 1
        buy.shares += shares
        buy.value += shares * price
        prices.append(price)
        d = _text(tx, "transactionDate")
        if d and d not in buy.dates:
            buy.dates.append(d)
        post_raw = _text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")
        nature = _text(tx, "ownershipNature/directOrIndirectOwnership") or "D"
        if post_raw:
            post = _num(post_raw)
            line = lines.setdefault(nature, [None, None])
            if line[0] is None:
                line[0] = max(0.0, post - shares)
            line[1] = post
    if not market:
        if offering:  # an offering or plan purchase dressed as code P: report why it was dropped
            log.debug("Form 4 purchase skipped (%s): %s", offering[0], buy.issuer)
        return None
    buy.low, buy.high = min(prices), max(prices)
    line = lines.get("D") or next(iter(lines.values()), None)
    if line is not None:
        buy.before, buy.after = line[0], line[1]
    return buy


def classify_history(
    filings: list[tuple[date, str]], month_trades: dict[int, bool | None], when: date
) -> tuple[str, str]:
    """Cohen-Malloy-Pomorski: an insider with three years of history who traded (P or S) in this
    calendar month in each of the three previous years is "routine"; one with the history but
    not that pattern is "opportunistic"; under three years of history, "new".

    ``filings``: (report date, accession) of the insider's Form 4s; ``month_trades``: year ->
    whether a same-month filing that year had an open-market trade (None: no filing that month)."""
    if not filings:
        return "", ""
    first = min(d for d, _ in filings)
    if (when - first).days < 3 * 365 - 31:
        return "new", f"Form 4 history since {first:%b %Y}: too short to call routine or not"
    years = [when.year - k for k in (1, 2, 3)]
    if all(month_trades.get(y) for y in years):
        return "routine", f"traded in {when:%B} in each of {years[-1]}-{years[0]} (a routine trader)"
    traded = [y for y in years if month_trades.get(y)]
    if traded:
        note = f"no {when:%B} pattern: traded in {when:%B} only in {', '.join(map(str, sorted(traded)))}"
    else:
        note = f"no {when:%B} trades in {years[-1]}-{years[0]}"
    return "opportunistic", note


def _owner_name(raw: str) -> str:
    """EDGAR writes owners "Last First Middle"; show "First Last"."""
    parts = raw.replace(",", " ").split()
    if len(parts) >= 2 and raw.isupper():
        return " ".join(p.title() for p in [*parts[1:], parts[0]])
    return raw.title() if raw.isupper() else raw


def _money(v: float) -> str:
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K"


def _stake(inc: float | None) -> str:
    if inc is None:
        return ""
    if inc == float("inf"):
        return "a new position"
    return f"+{inc * 100:,.0f}% to their stake"


class SECInsiderSource(SECEdgarSource):
    type_name = "sec_insider"
    default_interval = 10.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        cfg = {"forms": ["4"], **cfg}
        super().__init__(cfg, ctx)
        self.count = int(cfg.get("count", 100))
        self.min_value = float(cfg.get("min_value", 250_000))
        self.max_fetch = int(cfg.get("max_fetch", 100))
        self.concurrency = max(1, int(cfg.get("concurrency", 4)))
        self.history = bool(cfg.get("history", True))
        self.history_reads = int(cfg.get("history_reads", 6))
        self._done: dict[str, float] = {}  # accession -> when read
        self._recent: dict[str, list[tuple[float, str]]] = {}  # ticker -> [(when, owner)] buys
        self._patterns: dict[tuple[str, str], tuple[float, str, str]] = {}  # (owner, YYYY-MM) -> result
        self._primed = False
        self.reads = 0
        self.purchases = 0

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
        new = [f for f in filings[: self.max_fetch] if f[0] not in self._done]
        for acc, *_ in new:
            self._done[acc] = time.time()
        sem = asyncio.Semaphore(self.concurrency)

        async def read(acc: str, cik: str, updated: float | None) -> NewsItem | None:
            async with sem:
                try:
                    body = await self.ctx.http.get(
                        ARCHIVE_URL.format(cik=int(cik), acc_nodash=acc.replace("-", ""), acc=acc),
                        headers=self.sec_headers,
                    )
                except Exception as exc:  # noqa: BLE001 - one unreadable filing must not stop the rest
                    log.info("[%s] Form 4 %s unreadable: %s", self.name, acc, exc)
                    return None
                self.reads += 1
                doc = body.text()
                buy = parse_form4(doc)
                if buy is None or buy.value < self.min_value:
                    return None
                self.purchases += 1
                info = await self.context_for(buy)
                return self.purchase_item(doc, acc, updated, info=info)

        results = await asyncio.gather(*(read(acc, cik, upd) for acc, cik, _co, upd in new))
        if len(self._done) > 5000:
            for k in list(self._done)[:2000]:
                del self._done[k]
        return [r for r in results if r is not None]

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

    # ------------------------------------------------------------------ context

    async def context_for(self, buy: Purchase, today: date | None = None) -> InsiderContext:
        """History (routine or opportunistic), today's price and the 3-month high. Every part is
        optional: a lookup that fails leaves its field empty, never the alert."""
        info = InsiderContext()
        ticker = buy.ticker or self._cik_to_ticker.get(buy.cik, "")
        when = _first_date(buy) or today or datetime.now().date()
        if self.history and buy.owner_cik:
            try:
                info.pattern, info.pattern_note = await self.pattern(buy.owner_cik, when)
            except Exception as exc:  # noqa: BLE001 - context is a nice-to-have
                log.info("[%s] history of %s unavailable: %s", self.name, buy.owner_cik, exc)
        live = getattr(self.ctx, "live", None)
        q = live.get(ticker, 900) if live is not None and ticker else None
        if q is not None:
            info.price_now, info.price_source = q.price, "live"
        uni = getattr(self.ctx, "universe", None)
        li = uni.get(ticker) if uni is not None and ticker else None
        if info.price_now is None and li is not None and li.price:
            info.price_now, info.price_source = li.price, "last close"
        if ticker:
            try:
                info.off_high_pct = await self.off_high(ticker, info.price_now)
            except Exception as exc:  # noqa: BLE001
                log.debug("[%s] 3-month range of %s unavailable: %s", self.name, ticker, exc)
        return info

    async def pattern(self, owner_cik: str, when: date) -> tuple[str, str]:
        key = (owner_cik, f"{when:%Y-%m}")
        hit = self._patterns.get(key)
        if hit and time.time() - hit[0] < HISTORY_TTL_S:
            return hit[1], hit[2]
        resp = await self.ctx.http.get(SUBMISSIONS_URL.format(cik=owner_cik), headers=self.sec_headers)
        filings = submissions_form4(resp.json())
        month: dict[int, bool | None] = {}
        reads = 0
        for year in (when.year - 1, when.year - 2, when.year - 3):
            same = [acc for d, acc in filings if d.year == year and d.month == when.month]
            month[year] = False if same else None
            for acc in same[:2]:
                if reads >= self.history_reads:
                    break
                reads += 1
                body = await self.ctx.http.get(
                    ARCHIVE_URL.format(cik=int(owner_cik), acc_nodash=acc.replace("-", ""), acc=acc),
                    headers=self.sec_headers,
                )
                if open_market_codes(body.text()) & {"P", "S"}:
                    month[year] = True
                    break
        result = classify_history(filings, month, when)
        self._patterns[key] = (time.time(), *result)
        if len(self._patterns) > 2000:
            self._patterns.pop(next(iter(self._patterns)))
        return result

    async def off_high(self, ticker: str, price: float | None) -> float | None:
        resp = await self.ctx.http.get(
            SPARK_URL,
            params={"symbols": ticker, "range": "3mo", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout_s=15,
        )
        from ..market.prices import parse_spark

        snap = parse_spark(resp.json()).get(ticker) or {}
        closes = [p for _, p in snap.get("points") or [] if p]
        if not closes:
            return None
        now = price or closes[-1]
        return (now / max(closes) - 1.0) * 100.0

    # ------------------------------------------------------------------ the item

    def purchase_item(
        self, doc: str, acc: str, published: float | None = None, info: InsiderContext | None = None
    ) -> NewsItem | None:
        buy = parse_form4(doc)
        if buy is None or buy.value < self.min_value:
            return None
        info = info or InsiderContext()
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
        why: list[str] = []
        boost = 8.0
        if buy.role in ("CEO", "CFO", "President", "Chair"):
            boost += 6
        if buy.value >= 1e6:
            boost += 6
        if share is not None:
            boost += 15 if share >= 0.01 else 10 if share >= 0.002 else 4 if share >= 0.0005 else 0
        if others:
            boost += 10
        inc = buy.stake_increase
        if inc is not None:
            if inc == float("inf"):
                boost += 3
            elif inc >= 1.0:
                boost += 8
                why.append("doubled (or more) their holding")
            elif inc >= 0.25:
                boost += 5
            elif inc >= 0.10:
                boost += 2
        if buy.plan:
            boost -= 15
            why.append("made under a Rule 10b5-1 plan set up in advance: says little about today")
        if info.pattern == "routine":
            boost -= 12
            why.append("routine trader: their same-month trades predict nothing (Cohen-Malloy-Pomorski)")
        elif info.pattern == "opportunistic":
            boost += 8
            why.append(
                "opportunistic: the kind of insider trade that predicted returns (Cohen-Malloy-Pomorski)"
            )
        if info.off_high_pct is not None and info.off_high_pct <= -20:
            boost += 3
            why.append(f"buying {abs(info.off_high_pct):.0f}% below the 3-month high")
        name = _owner_name(buy.owner)
        price = f"${buy.avg_price:,.2f}" if buy.low == buy.high else f"${buy.low:,.2f}-${buy.high:,.2f}"
        tick = f" ({ticker})" if ticker else ""
        tags = [
            t
            for t in (
                _stake(inc),
                "10b5-1 plan" if buy.plan else "",
                info.pattern if info.pattern in ("opportunistic", "routine") else "",
            )
            if t
        ]
        title = (
            f"{buy.role} {name} buys {_money(buy.value)} of {buy.issuer.title()}{tick} stock "
            f"at {price} (Form 4, open market{''.join(', ' + t for t in tags)})"
        )
        bits = [f"{buy.shares:,.0f} shares on {', '.join(buy.dates[:3])}"]
        if share is not None and cap:
            bits.append(f"{share * 100:.2f}% of the company's ${cap / 1e9:,.2f}B value")
        if buy.after is not None:
            bits.append(f"now holds {buy.after:,.0f} shares")
        if others:
            bits.append(
                f"cluster: also bought this week by {', '.join(sorted(_owner_name(o) for o in others)[:3])}"
            )
        vs = None
        if info.price_now and buy.avg_price:
            vs = (info.price_now / buy.avg_price - 1.0) * 100.0
            where = "still at or below" if vs <= 2 else "above"
            bits.append(
                f"{ticker or 'stock'} ${info.price_now:,.2f} ({info.price_source}): {vs:+.1f}% vs the insider's "
                f"${buy.avg_price:,.2f}, {where} their price"
            )
        if info.pattern_note:
            bits.append(info.pattern_note)
        bits.extend(why)
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
                    "stake_increase": (None if inc is None else -1 if inc == float("inf") else round(inc, 4)),
                    "holds_after": buy.after,
                    "plan_10b5_1": buy.plan,
                    "pattern": info.pattern,
                    "price_now": info.price_now,
                    "vs_insider_pct": round(vs, 2) if vs is not None else None,
                    "off_3m_high_pct": round(info.off_high_pct, 1) if info.off_high_pct is not None else None,
                    "trade_dates": list(buy.dates),
                },
            }
        )
        item.extra["boost"] = item.extra.get("boost", 0) + boost
        return item


def _first_date(buy: Purchase) -> date | None:
    for d in sorted(buy.dates):
        try:
            return date.fromisoformat(d[:10])
        except ValueError:
            continue
    return None


def submissions_form4(doc: Any) -> list[tuple[date, str]]:
    """data.sec.gov submissions JSON -> [(report date, accession)] of the filer's Form 4s."""
    if isinstance(doc, (bytes, str)):
        doc = json.loads(doc)
    recent = ((doc or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    reports = recent.get("reportDate") or []
    filed = recent.get("filingDate") or []
    out: list[tuple[date, str]] = []
    for i, form in enumerate(forms):
        if form not in ("4", "4/A") or i >= len(accs):
            continue
        raw = (reports[i] if i < len(reports) and reports[i] else "") or (filed[i] if i < len(filed) else "")
        try:
            out.append((date.fromisoformat(raw[:10]), accs[i]))
        except ValueError:
            continue
    return out
