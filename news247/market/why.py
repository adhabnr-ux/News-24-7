"""Why is it moving? An active lookup the moment a stock moves with no headline.

The radar and the options tape often see a move before any feed carries its reason. Waiting
for the next poll of every source wastes the minutes that matter, so when a big move arrives
with no known catalyst, two targeted reads go out at once:

* **The company's own EDGAR filings** (``browse-edgar?action=getcompany&CIK=<ticker>``): an
  8-K accepted at 16:01, a 13D, an offering prospectus or an insider's Form 4 filed today.
* **A Google News search for the company's name** over the last day, which finds the scoop,
  the trade-press story or the wire rewrite the general feeds haven't reached yet.

Whatever is found goes through the normal pipeline (scored, stored, alerted if it is big
enough) and the best explanation is attached to the move's alert as "Why". Lookups are
limited per company and per hour so a volatile morning can't flood SEC or Google.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Any
from urllib.parse import quote_plus

from ..models import NewsItem, SourceTier
from ..sources.rss import parse_feed
from ..sources.sec_edgar import ITEM_INFO
from ..util import strip_html, struct_to_ts
from .universe import display_name, name_key

log = logging.getLogger(__name__)

EDGAR_COMPANY_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={symbol}&type=&dateb="
    "&owner=include&count=20&output=atom"
)
GNEWS_URL = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-US&gl=US&ceid=US:en"
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_COMPANY = re.compile(r"<conformed-name>(.*?)</conformed-name>", re.S)
_ITEM_NUM = re.compile(r"\b(\d\.\d\d)\b")
_SUFFIX_WORDS = re.compile(
    r"[,.]?\s+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|l\.p|n\.v|s\.a|"
    r"holdings?|group)\.?$",
    re.I,
)

# Forms that explain a move, with a label and a source boost (8-K items add their own).
FORM_INFO: dict[str, tuple[str, float]] = {
    "8-K": ("current report", 6),
    "6-K": ("foreign issuer report", 8),
    "SC 13D": ("activist stake", 15),
    "SCHEDULE 13D": ("activist stake", 15),
    "SC 13G": ("passive stake", 4),
    "SCHEDULE 13G": ("passive stake", 4),
    "SC TO-T": ("tender offer", 25),
    "SC 14D9": ("response to a tender offer", 12),
    "425": ("merger communication", 10),
    "424B5": ("prospectus supplement (likely an offering)", 12),
    "424B3": ("prospectus (possible resale or offering)", 4),
    "S-1": ("registration (offering)", 8),
    "S-3": ("shelf registration (offering capacity)", 6),
    "4": ("insider transaction (Form 4)", 0),
    "DEFM14A": ("merger proxy", 6),
    "PREM14A": ("merger proxy (preliminary)", 6),
    "NT 10-K": ("late annual report", 15),
    "NT 10-Q": ("late quarterly report", 12),
    "15-12B": ("deregistration", 15),
    "25-NSE": ("delisting", 15),
}


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
    return strip_html(m.group(1), None).strip() if m else ""


def parse_company_filings(body: str, symbol: str, since: float) -> list[NewsItem]:
    """EDGAR's per-company Atom feed -> one item per filing accepted since ``since``."""
    company = (
        display_name(strip_html(_COMPANY.search(body).group(1), None)) if _COMPANY.search(body) else symbol
    )
    company = company.title() if company.isupper() else company
    out: list[NewsItem] = []
    for block in _ENTRY.findall(body):
        form = _tag(block, "filing-type") or _tag(block, "title").split(" - ")[0].strip()
        acc = _tag(block, "accession-number") or _tag(block, "accession-nunber")
        updated = _tag(block, "updated")
        ts = _iso_ts(updated)
        if ts is None or ts < since or not form:
            continue
        items_desc = _tag(block, "items-desc")
        boost = FORM_INFO.get(form.upper(), ("", 0))[1]
        labels: list[str] = []
        if form.upper() in ("8-K", "8-K/A"):
            nums = list(dict.fromkeys(_ITEM_NUM.findall(items_desc)))
            boost += sum(ITEM_INFO.get(n, ("", 0))[1] for n in nums)
            labels = [f"{n} {ITEM_INFO[n][0]}" for n in nums if n in ITEM_INFO and ITEM_INFO[n][1] > 0]
        elif form.upper() in FORM_INFO:
            labels = [FORM_INFO[form.upper()][0]]
        title = f"{company} ({symbol}) files {form}" + (f": {'; '.join(labels)}" if labels else "")
        link = re.search(r'<link[^>]*href="([^"]+)"', block)
        item = NewsItem(
            source="why-edgar",
            title=title,
            url=_tag(block, "filing-href") or (link.group(1) if link else ""),
            summary=items_desc,
            published=ts,
            tier=SourceTier.PRIMARY,
            uid=f"sec:{acc}" if acc else "",  # the same uid the 8-K feed uses: never alerted twice
            tickers=[symbol],
        )
        item.extra.update({"form": form, "boost": boost, "why_for": symbol})
        out.append(item)
    return out


def _iso_ts(s: str) -> float | None:
    from datetime import datetime

    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def search_name(name: str) -> str:
    """'Crown Castle Inc. Common Stock' -> 'Crown Castle' (what a headline would say)."""
    n = display_name(name)
    for _ in range(3):
        stripped = _SUFFIX_WORDS.sub("", n).strip(" ,.")
        if stripped == n or not stripped:
            break
        n = stripped
    return n


def parse_news_search(body: bytes, symbol: str, name: str, since: float) -> list[NewsItem]:
    """Google News results that actually name the company, newest first."""
    key = name_key(name)
    want = re.compile(rf"(?<![\w-]){re.escape(key)}(?![\w-])") if key else None
    tick = re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w])")
    out: list[NewsItem] = []
    for e in parse_feed(body).entries:
        title = strip_html(e.get("title"), None)
        published = struct_to_ts(e.get("published_parsed")) or struct_to_ts(e.get("updated_parsed"))
        if not title or (published is not None and published < since):
            continue
        norm = re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", title.lower().replace("&", " and ")))
        if not ((want and want.search(norm)) or tick.search(title)):
            continue  # a search hit that doesn't name the company is not its explanation
        item = NewsItem(
            source="why-gnews",
            title=title,
            url=e.get("link", ""),
            published=published,
            tier=SourceTier.MEDIA,
            tickers=[symbol],
        )
        item.extra["why_for"] = symbol
        out.append(item)
    return out


class WhyHunter:
    def __init__(
        self,
        http: Any,
        user_agent: str,
        per_hour: int = 30,
        cooldown_s: float = 2 * 3600,
    ) -> None:
        self.http = http
        self.user_agent = user_agent
        self.per_hour = per_hour
        self.cooldown_s = cooldown_s
        self._last: dict[str, float] = {}
        self._recent: deque[float] = deque()
        self.stats = {"hunts": 0, "found": 0, "skipped": 0, "errors": 0}

    def allow(self, symbol: str, now: float | None = None) -> bool:
        now = now or time.time()
        while self._recent and now - self._recent[0] > 3600:
            self._recent.popleft()
        if now - self._last.get(symbol, 0.0) < self.cooldown_s or len(self._recent) >= self.per_hour:
            self.stats["skipped"] += 1
            return False
        self._last[symbol] = now
        self._recent.append(now)
        return True

    async def hunt(self, symbol: str, name: str, since: float) -> list[NewsItem]:
        """Today's filings and news for one company. Never raises: a failed read is logged."""
        self.stats["hunts"] += 1
        found: list[NewsItem] = []
        try:
            resp = await self.http.get(
                EDGAR_COMPANY_URL.format(symbol=quote_plus(symbol)), headers={"User-Agent": self.user_agent}
            )
            found += parse_company_filings(resp.text(), symbol, since)
        except Exception as exc:  # noqa: BLE001 - one source failing must not stop the other
            self.stats["errors"] += 1
            log.info("why: EDGAR lookup for %s failed: %s", symbol, exc)
        q = search_name(name) if name and name.upper() != symbol else ""
        query = f'"{q}"' if q else f'"{symbol}" stock'
        try:
            resp = await self.http.get(GNEWS_URL.format(q=quote_plus(query)))
            found += parse_news_search(resp.body, symbol, q or symbol, since)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            log.info("why: news search for %s failed: %s", symbol, exc)
        if found:
            self.stats["found"] += 1
        return found
