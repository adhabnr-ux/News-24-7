"""Every listed US stock and how big it is.

The point of knowing a company's size: the same headline means wildly different things
depending on who it is about. A $40M Army contract is a rounding error for Lockheed and a
+60% day for a $70M drone maker; an FDA approval barely moves Lilly and doubles a $200M
biotech. ``Universe`` holds symbol → (name, market cap, price, sector) for ~7,000 Nasdaq,
NYSE and NYSE American listings so the scorer can size news against the company it is about.

Data comes from Nasdaq's public stock screener (the JSON behind nasdaq.com/market-activity/
stocks/screener: free, no key, the whole market in one ~2 MB request). It is cached in the
data directory so a restart, or a day the endpoint is down, still knows every company.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
# api.nasdaq.com answers browsers only: without these it stalls or returns 403
SCREENER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
CACHE_NAME = "universe.json.gz"

# size bands, in USD of market capitalization
BANDS: tuple[tuple[float, str], ...] = (
    (50e6, "nano cap"),
    (300e6, "micro cap"),
    (2e9, "small cap"),
    (10e9, "mid cap"),
    (200e9, "large cap"),
)

# the share class / security type tail Nasdaq appends to every name
_NAME_TAIL = re.compile(
    r"\s+(?:(?:class|series)\s+[a-z0-9]+\s+)?"
    r"(?:common\s+stock|ordinary\s+shares?|common\s+shares?|subordinate\s+voting\s+shares|"
    r"american\s+depositary\s+(?:shares|receipts?)|ads|shares\s+of\s+beneficial\s+interest|"
    r"units?|warrants?|rights?|depositary\s+shares?|capital\s+stock)\b.*$",
    re.I,
)
_NOT_COMMON = re.compile(
    r"\b(warrants?|units?|rights?|preferred|preference|notes\s+due|debentures?|"
    r"depositary\s+shares?\s+(?:each\s+)?representing|perpetual|senior\s+notes|trust\s+preferred)\b",
    re.I,
)
# corporate suffixes dropped when matching a headline's subject against listing names
_SUFFIX = re.compile(
    r"[,.]?\s+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|l\.p|n\.v|nv|s\.a|sa|"
    r"ag|se|holdings?|group|hldgs|international|intl|technologies|technology|the)\.?$",
    re.I,
)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'.-]*")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*|&")
_CAMEL = re.compile(r"^[a-zA-Z]+[a-z][A-Z][A-Za-z]*$")  # SoundHound, iLearningEngines, CoreWeave
# Words that begin many company names and are ordinary headline vocabulary. A company is only
# recognized by its first word alone when that word is distinctive ("Capricor", "Navitas").
COMMON_WORDS = frozenset(
    """
    about above across action active acquire acquisition adaptive advanced advisors aerospace africa
    against agency agricultural airlines alliance allied alpha alternative america american americas
    analytics ancient annual apollo applied archer arctic armada artificial asset assets associated
    atlantic atlas audio aurora automotive avenue balance banking banks bancorp bancshares beacon
    beyond biotech blockchain bridge bright brands broad builders business california canada canadian
    capital cardinal carbon cardiac career cargo catalyst cellular center central century champion
    change charter chemical children china chinese circle citizens civil classic clean clear clinical
    cloud coastal colony columbia commerce commercial community company compass computer concord
    consolidated consumer continental core corporate country covenant creative credit critical cross
    crown crystal cyber daily danger delta dental design diamond digital direct discovery diversified
    domestic dragon dynamic eagle eastern economic electric electronic elite emerald emerging empire
    energy engine enterprise entertainment environmental equity essential europe european evolution
    excel executive exchange express federal fidelity financial first flagship florida focus forward
    founders freedom frontier fusion future galaxy gaming garden general genesis genetic global golden
    granite great green growth guardian harbor harmony health healthcare heritage highland holdings
    horizon hospitality housing hudson huntington ideal image impact income independence independent
    industrial industries infinity innovative insight institutional insurance integrated intelligent
    interactive international investment investors island jupiter keystone kingdom landmark latin
    leader legacy liberty lithium logistics lucky machine magnolia marine maritime market marketing
    materials matrix medical medicine mercury meridian metals metro micro midwest millennium mineral
    minerals mining mission mobile modern momentum national natural nature network networks northern
    northwest nova nuclear ocean omega optical orion pacific paramount partners patriot peak people
    phoenix pinnacle pioneer planet platinum plaza point power precision premier premium prime pro
    progress prospect provident public quantum radiant rapid real regional reliance renewable republic
    research resource resources retail rising river robotics royal safety sapphire science scientific
    secure security select senior sierra sigma signal silver simple smart social software solar
    solutions southern southwest sovereign space spectrum spirit sports standard star state sterling
    stone strategic strategy summit sun superior supreme systems target technologies technology texas
    therapeutics titan total tower trade trans travel trinity trust union united universal urban
    valley vanguard vector venture ventures victory vision vista water western wireless world
    """.split()  # noqa: SIM905 - a word list reads better as prose
)


def display_name(raw: str) -> str:
    """'Acme Biotech, Inc. Common Stock' -> 'Acme Biotech, Inc.'"""
    name = _NAME_TAIL.sub("", (raw or "").strip())
    return name.strip(" ,-") or (raw or "").strip()


def name_key(name: str) -> str:
    """Normalize a company name for matching: lower-case words without corporate suffixes."""
    key = display_name(name).lower().replace("&", " and ")
    key = re.sub(r"[^\w\s-]", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    for _ in range(4):
        stripped = _SUFFIX.sub("", key).strip(" ,.")
        if stripped == key or not stripped:
            break
        key = stripped
    return key


def money(text: Any) -> float | None:
    """'$12.34' / '1234.5' / 'NA' / '' -> float or None (non-positive counts as unknown)."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    s = str(text).strip().replace("$", "").replace(",", "")
    if not s or s.upper() in ("NA", "N/A", "--", "-"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def pct(text: Any) -> float | None:
    """'-0.451%' / '12.5' -> float; 'NA' / '' -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace("%", "").replace(",", "")
    if not s or s.upper() in ("NA", "N/A", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _year(text: Any) -> int | None:
    try:
        y = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return y if 1900 < y < 2200 else None


def fmt_cap(cap: float | None) -> str:
    """$1.2T / $34B / $180M / $9.5M"""
    if not cap:
        return "?"
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if cap >= div:
            v = cap / div
            return f"${v:.1f}{unit}" if v < 10 else f"${v:.0f}{unit}"
    return f"${cap / 1e3:.0f}K"


def band(cap: float | None) -> str:
    if not cap:
        return "unknown size"
    for limit, label in BANDS:
        if cap < limit:
            return label
    return "mega cap"


@dataclass(frozen=True)
class Listing:
    symbol: str
    name: str
    market_cap: float | None = None  # USD
    price: float | None = None  # last sale
    change_pct: float | None = None  # today vs previous close
    volume: float | None = None  # shares today
    sector: str = ""
    industry: str = ""
    country: str = ""
    ipo_year: int | None = None
    common: bool = True  # False for warrants, units, rights, preferreds, notes

    @property
    def band(self) -> str:
        return band(self.market_cap)

    @property
    def cap_label(self) -> str:
        return fmt_cap(self.market_cap)

    @property
    def dollar_volume(self) -> float | None:
        if self.price and self.volume:
            return self.price * self.volume
        return None

    @property
    def turnover(self) -> float | None:
        """Share of the company that changed hands today (dollar volume / market cap)."""
        dv = self.dollar_volume
        if dv and self.market_cap:
            return dv / self.market_cap
        return None

    @property
    def pump_profile(self) -> str:
        """Nasdaq: ~70% of its manipulation referrals are recent China/Hong Kong micro-cap IPOs
        (it raised the minimum IPO size for them in 2025). Such names need more than a headline."""
        recent = self.ipo_year is not None and self.ipo_year >= time.gmtime().tm_year - 3
        if (
            recent
            and self.country.lower() in ("china", "hong kong", "macau", "singapore", "malaysia")
            and (self.market_cap or 0) < 300e6
        ):
            return f"recent {self.country} micro-cap IPO (manipulation-prone profile)"
        return ""

    @property
    def biotech(self) -> bool:
        text = f"{self.sector} {self.industry} {self.name}".lower()
        return any(
            w in text
            for w in ("biotech", "pharma", "therapeut", "biological", "medicinal", "bioscien", "biopharm")
        )

    def brief(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "market_cap": self.market_cap,
            "cap": self.cap_label,
            "band": self.band,
            "price": self.price,
            "sector": self.sector,
            "industry": self.industry,
        }


def parse_screener(data: dict[str, Any]) -> list[Listing]:
    """Nasdaq screener JSON (both the ``download=true`` and the paged table shape) -> listings."""
    payload = data.get("data") or {}
    rows = payload.get("rows")
    if rows is None:
        rows = (payload.get("table") or {}).get("rows")
    out: list[Listing] = []
    for r in rows or []:
        sym = str(r.get("symbol") or "").strip().upper().replace("/", "-").replace("^", "-P")
        if not sym or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
            continue
        raw_name = str(r.get("name") or "").strip()
        out.append(
            Listing(
                symbol=sym,
                name=display_name(raw_name),
                market_cap=money(r.get("marketCap")),
                price=money(r.get("lastsale")),
                change_pct=pct(r.get("pctchange")),
                volume=money(r.get("volume")),
                sector=str(r.get("sector") or "").strip(),
                industry=str(r.get("industry") or "").strip(),
                country=str(r.get("country") or "").strip(),
                ipo_year=_year(r.get("ipoyear")),
                common=not _NOT_COMMON.search(raw_name),
            )
        )
    return out


def _share_classes(a: str, b: str) -> bool:
    """GOOG/GOOGL, BRK-A/BRK-B, FOX/FOXA, LBRDA/LBRDK: one company, several tickers."""
    a, b = sorted((a, b), key=len)
    if a.split("-")[0] == b.split("-")[0]:
        return True
    return b.startswith(a) or (len(a) == len(b) >= 3 and a[:-1] == b[:-1])


class Universe:
    """Symbol -> Listing, plus a name index to recognize a headline's subject company."""

    def __init__(self, listings: Iterable[Listing] = (), *, loaded_at: float = 0.0, source: str = "") -> None:
        self.by_symbol: dict[str, Listing] = {}
        self._by_name: dict[str, str] = {}
        self._ambiguous: set[str] = set()
        self._first: dict[str, list[tuple[tuple[str, ...], str]]] = {}
        self._solo: dict[str, str] = {}
        self._short: dict[str, str] = {}
        self.loaded_at = loaded_at
        self.source = source
        self.replace(listings, loaded_at=loaded_at, source=source)

    # ------------------------------------------------------------------ building

    def replace(
        self, listings: Iterable[Listing], *, loaded_at: float | None = None, source: str = ""
    ) -> None:
        by_symbol: dict[str, Listing] = {}
        by_name: dict[str, str] = {}
        ambiguous: set[str] = set()
        short: dict[str, str] = {}
        for li in listings:
            by_symbol[li.symbol] = li
            if not li.common:
                continue
            key = name_key(li.name)
            if len(key) < 4:
                # "Arm", "IBM": too short to spot in a headline, fine for an exact full-name lookup
                if key:
                    short.setdefault(key, li.symbol)
                continue
            other = by_name.get(key)
            if other and other != li.symbol:
                # two share classes of one company (GOOG/GOOGL) are fine: keep the bigger one;
                # two different companies with one name are ambiguous: match neither
                o = by_symbol[other]
                if _share_classes(o.symbol, li.symbol):
                    if (li.market_cap or 0) > (o.market_cap or 0):
                        by_name[key] = li.symbol
                    continue
                ambiguous.add(key)
                continue
            by_name[key] = li.symbol
        for key in ambiguous:
            by_name.pop(key, None)
        self.by_symbol, self._by_name, self._ambiguous = by_symbol, by_name, ambiguous
        self._short = short
        # first word -> [(all words, symbol)] for names of 2+ words, to spot companies mid-headline
        first: dict[str, list[tuple[tuple[str, ...], str]]] = {}
        for key, sym in by_name.items():
            words = tuple(key.split())
            if len(words) >= 2:
                first.setdefault(words[0], []).append((words, sym))
        for lst in first.values():
            lst.sort(key=lambda e: len(e[0]), reverse=True)
        self._first = first
        # a distinctive first word owned by exactly one company ("capricor" -> CAPR)
        owners: dict[str, set[str]] = {}
        for key, sym in by_name.items():
            word = key.split()[0]
            owners.setdefault(word, set()).add(sym)
        self._solo = {
            w: next(iter(syms))
            for w, syms in owners.items()
            if len(syms) == 1 and len(w) >= 6 and w.isalpha() and w not in COMMON_WORDS
        }
        self.loaded_at = time.time() if loaded_at is None else loaded_at
        if source:
            self.source = source

    @classmethod
    def from_screener(cls, data: dict[str, Any], loaded_at: float | None = None) -> Universe:
        return cls(
            parse_screener(data), loaded_at=time.time() if loaded_at is None else loaded_at, source="nasdaq"
        )

    @classmethod
    def stub(cls, entries: dict[str, dict[str, Any]]) -> Universe:
        """{SYM: {name, mcap, price, sector, industry}} — for the backtest and tests."""
        return cls(
            (
                Listing(
                    symbol=sym.upper(),
                    name=str(v.get("name", sym)),
                    market_cap=money(v.get("mcap", v.get("market_cap"))),
                    price=money(v.get("price")),
                    sector=str(v.get("sector", "")),
                    industry=str(v.get("industry", "")),
                    country=str(v.get("country", "")),
                    ipo_year=_year(v.get("ipo_year")),
                )
                for sym, v in entries.items()
            ),
            loaded_at=time.time(),
            source="stub",
        )

    # ------------------------------------------------------------------ cache

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "loaded_at": self.loaded_at,
            "source": self.source,
            "listings": [asdict(li) for li in self.by_symbol.values()],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(doc, separators=(",", ":")).encode()))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Universe | None:
        try:
            doc = json.loads(gzip.decompress(path.read_bytes()))
            listings = [Listing(**li) for li in doc.get("listings", [])]
        except (OSError, ValueError, TypeError) as exc:
            if path.exists():
                log.warning("universe cache %s unreadable: %s", path, exc)
            return None
        return cls(listings, loaded_at=float(doc.get("loaded_at", 0)), source=str(doc.get("source", "cache")))

    # ------------------------------------------------------------------ lookups

    def __len__(self) -> int:
        return len(self.by_symbol)

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self.by_symbol

    def __bool__(self) -> bool:  # an empty universe is still a universe object
        return True

    def get(self, symbol: str) -> Listing | None:
        return self.by_symbol.get(symbol.upper().replace(".", "-"))

    @property
    def age(self) -> float:
        return time.time() - self.loaded_at if self.loaded_at else float("inf")

    def lookup_name(self, name: str) -> Listing | None:
        """Exact (normalized) full-name lookup, e.g. a 13F's "SOUNDHOUND AI INC" -> SOUN."""
        key = name_key(name)
        sym = self._by_name.get(key) or self._short.get(key)
        return self.by_symbol.get(sym) if sym else None

    def find_issuer(self, title: str) -> Listing | None:
        """The listed company a headline is about, from the words it starts with.

        Press releases lead with the issuer ("Soleno Therapeutics Announces FDA Approval…"),
        so the longest leading run of words that equals a listing's name identifies it. One word
        alone only counts when it IS the whole name (e.g. "Opendoor") — never a common word that
        merely begins one."""
        words = _WORD.findall(title[:120])
        if not words:
            return None
        lead: list[str] = []
        for w in words[:7]:
            if not (w[0].isupper() or w[0].isdigit()) and w.lower() not in ("and", "of", "&", "de", "la"):
                break
            lead.append(w)
        for n in range(len(lead), 0, -1):
            cand = " ".join(lead[:n])
            key = name_key(cand)
            if not key or (n == 1 and (len(key) < 5 or lead[0].isupper())):
                continue  # one short word or an acronym ("FDA", "OPEC") is never enough
            sym = self._by_name.get(key)
            if sym:
                return self.by_symbol[sym]
        return None

    def find_named(self, text: str, limit: int = 4) -> list[Listing]:
        """Listed companies named anywhere in a headline, e.g. "Nvidia discloses stake in Serve
        Robotics" -> Serve Robotics, "FDA panel votes against Capricor's deramiocel" -> Capricor.
        Either the full (2+ word) name, or a distinctive first word only one company owns
        (6+ letters, not ordinary vocabulary). Names must be written capitalized (or CamelCase,
        "iLearningEngines"), so ordinary phrases that happen to equal a name don't count."""
        tokens = [
            (m.group(0), m.group(0).lower().replace("&", "and").removesuffix("'s"))
            for m in _TOKEN.finditer(text[:300])
        ]
        found: dict[str, Listing] = {}
        i = 0
        while i < len(tokens) and len(found) < limit:
            raw, low = tokens[i]
            step = 1
            proper = raw[0].isupper() or bool(_CAMEL.match(raw))
            if proper and low in self._first:
                for words, sym in self._first[low]:
                    n = len(words)
                    if tuple(t[1] for t in tokens[i : i + n]) == words:
                        found.setdefault(sym, self.by_symbol[sym])
                        step = n
                        break
            if step == 1 and proper and low in self._solo and not raw.isupper():
                sym = self._solo[low]
                found.setdefault(sym, self.by_symbol[sym])
            i += step
        return list(found.values())
