"""The trend lane: what people are doing, before the earnings show it (Chris Camillo's method).

Camillo's edge was never secret information. It was noticing a real change in consumer
behavior (a product taking off, a habit spreading, a brand losing customers), tying it to the
listed company whose revenue it moves, and checking that the stock hadn't caught up. In 2026 the
professional versions of that data (foot traffic, app downloads, card spend) are sold to hedge
funds, so the edge that is left is narrower: speed of mapping, materiality, and catching the
cases where the data and the price disagree (Oct 2026: Burger King visits +7% while QSR fell 9%).

Four kinds of evidence, each with its own test:

``attention``  Wikipedia page views for the brand (Wikimedia's free API): the 7-day average at
               2x+ the prior 8 weeks, 4+ robust standard deviations up, on 4 of the last 7 days.
               Direction-free: a scandal draws attention too.
``app``        The US App Store top-free chart (Apple's free feed, every 6 h): an app climbing
               into the top 50 from outside the top 150 and holding there.
``alt_data``   News citing the trackers (Placer.ai, Sensor Tower, Circana, Second Measure...)
               with numbers on visits, downloads or spending. Direction from the numbers.
``demand``     Sell-outs, waitlists, shortages and restocks of a named brand.

Filters, all required before anything reaches you:

* **Mapped and listed**: the brand map (``data/trends.yaml``) or an exact name match to a
  listed company. Not a common stock in the universe: dropped.
* **Material**: the brand is the company (``core``, up to $150B), one of its few headline brands
  (``major``, up to $60B), or a name match (``auto``, up to $20B). A viral product inside a
  $3T company is not an edge.
* **Not priced in**: the stock hasn't already moved 15% in the trend's direction since the
  evidence began.
* **Fresh**: one signal per company per 30 days, unless it gets stronger.

Severity: two independent kinds agreeing on a direction -> HIGH (pushed). One kind with a
direction, or attention alone -> MEDIUM (in the app). Anything that fails materiality or is
priced in -> LOW (on the trend board, never pinged). Every signal is recorded with the price
then, and scored against SPY 30, 60 and 90 days later (``news247 trends --scorecard``), so the
edge is measured rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import TrendsConfig, load_package_yaml
from .market.prices import CHART_URL, parse_chart
from .market.universe import fmt_cap

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

WIKI_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
    "{article}/daily/{start}/{end}"
)
# titles -> the article they really are (a redirect's own page views are near zero, so reading
# "Hoka_One_One" instead of the article it points to would make a brand silently invisible)
WIKI_TITLES_URL = "https://en.wikipedia.org/w/api.php"
WIKI_UA = "News247 trend lane (github.com/adhabnr-ux/News-24-7)"
QUIET_VIEWS = 50.0  # an article this quiet is almost certainly the wrong one
APPS_URL = "https://rss.applemarketingtools.com/api/v2/us/apps/top-free/200/apps.json"
KINDS = ("attention", "app", "alt_data", "demand")
KIND_LABEL = {
    "attention": "Wikipedia attention",
    "app": "App Store climb",
    "alt_data": "tracker data",
    "demand": "sell-out / waitlist",
}
NEWS_THEMES = {"consumer_data_signal": "alt_data", "demand_signal": "demand"}
_RANK = {"core": 3, "major": 2, "auto": 1}
_SEV = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


# --------------------------------------------------------------------------- the brand map


@dataclass
class Brand:
    brand: str
    ticker: str
    weight: str = "core"  # core | major
    wiki: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)


def load_brands(data: dict[str, Any] | None = None) -> list[Brand]:
    data = data if data is not None else load_package_yaml("trends.yaml")
    out = []
    for b in data.get("brands", []):
        out.append(
            Brand(
                brand=str(b["brand"]),
                ticker=str(b["ticker"]).upper(),
                weight=str(b.get("weight", "core")),
                wiki=[str(w) for w in b.get("wiki", [])],
                apps=[str(a) for a in b.get("apps", [])],
                terms=[str(t).lower() for t in b.get("terms", [])],
            )
        )
    return out


# --------------------------------------------------------------------------- pure tests


def wiki_stats(views: list[float]) -> dict[str, float] | None:
    """The last 7 days against the 8 weeks before: 7-day average, baseline median, ratio and a
    robust z-score (median absolute deviation, floored at 5% of the median)."""
    if len(views) < 35:
        return None
    recent, base = views[-7:], views[-63:-7]
    med = statistics.median(base)
    mad = statistics.median(abs(v - med) for v in base) * 1.4826
    avg7 = sum(recent) / 7
    return {
        "avg7": avg7,
        "baseline": med,
        "ratio": avg7 / med if med > 0 else 0.0,
        "z": (avg7 - med) / max(mad, med * 0.05, 1.0),
    }


def wiki_spike(
    views: list[float],
    min_views: float = 1500.0,
    ratio: float = 2.0,
    z: float = 4.0,
    sustained: int = 4,
) -> dict[str, Any] | None:
    """Daily views, oldest first (at least 35 days). The last 7 days against the 8 weeks before:
    a spike when the 7-day average is ``ratio``x the baseline median, ``z`` robust standard
    deviations above it, and at least ``sustained`` of the 7 days are 1.5x the baseline."""
    st = wiki_stats(views)
    if st is None:
        return None
    avg7, med, r, zs = st["avg7"], st["baseline"], st["ratio"], st["z"]
    if avg7 < min_views or med <= 0:
        return None
    days = [i for i, v in enumerate(views[-7:]) if v >= 1.5 * med]
    if r < ratio or zs < z or len(days) < sustained:
        return None
    return {
        "ratio": round(r, 2),
        "z": round(zs, 1),
        "avg7": round(avg7),
        "baseline": round(med),
        "days_up": len(days),
        "first_day": 7 - days[0],  # how many days ago the run started
    }


def resolve_titles(data: dict[str, Any], asked: list[str]) -> tuple[dict[str, str], set[str]]:
    """MediaWiki ``action=query&redirects=1`` (formatversion 2) -> ({asked title: canonical
    title with underscores}, {asked titles with no article})."""
    q = (data or {}).get("query") or {}
    hop = {}
    for key in ("normalized", "redirects"):
        for r in q.get(key) or []:
            hop[str(r.get("from", ""))] = str(r.get("to", ""))
    missing = {str(p.get("title", "")) for p in q.get("pages") or [] if p.get("missing") or p.get("invalid")}
    canonical: dict[str, str] = {}
    gone: set[str] = set()
    for art in asked:
        t = art.replace("_", " ")
        for _ in range(4):  # normalized -> redirect (-> redirect)
            if t not in hop:
                break
            t = hop[t]
        if t in missing:
            gone.add(art)
        else:
            canonical[art] = t.replace(" ", "_")
    return canonical, gone


def parse_app_chart(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Apple's marketing-tools RSS (v2 JSON): rank, id, name and developer for each app."""
    results = ((data or {}).get("feed") or {}).get("results") or []
    out = []
    for i, r in enumerate(results, 1):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        out.append(
            {
                "rank": i,
                "id": str(r["id"]),
                "name": str(r.get("name", "")),
                "artist": str(r.get("artistName", "")),
            }
        )
    return out


def app_climb(
    history: list[tuple[float, int | None]],
    top: int = 50,
    from_rank: int = 150,
    hold: int = 3,
    now: float | None = None,
    watched_since: float | None = None,
) -> dict[str, Any] | None:
    """``history``: (snapshot time, rank or None when outside the chart), oldest first. A climb
    when the last ``hold`` snapshots are all inside the top ``top`` and two weeks ago the app was
    outside the top ``from_rank`` or off the chart. An app first seen recently counts as off the
    chart back then only if the chart itself was being watched (``watched_since``)."""
    if len(history) < hold:
        return None
    now = now or history[-1][0]
    last = [r for _, r in history[-hold:]]
    if any(r is None or r > top for r in last):
        return None
    old = [r for t, r in history if now - t >= 12 * 86400]
    if old:
        then = old[-1]
    elif watched_since is not None and now - watched_since >= 12 * 86400:
        then = None  # new to the chart: it wasn't in the top 200 while we watched
    else:
        return None
    if then is not None and then <= from_rank:
        return None
    since = history[-1][0]
    for t, r in reversed(history):  # back to where the current run inside the top began
        if r is None or r > top:
            break
        since = t
    return {"rank": last[-1], "was": then, "since": since}


_UP = re.compile(
    r"\b(rose|rise[sn]?|rising|grew|grow(s|ing|th)?|gain(s|ed|ing)?|jump(s|ed|ing)?|surg(e|es|ed|ing)|"
    r"climb(s|ed|ing)?|increas(e|es|ed|ing)|higher|accelerat\w*|soar(s|ed|ing)?|spik(e|es|ed|ing)|record|"
    r"best|top(s|ped)?|boom\w*|up\s+(\d+|double|triple|sharply|strongly)|outpac\w*|beat(s|ing)?)\b|\+\d",
    re.I,
)
_DOWN = re.compile(
    r"\b(fell|fall(s|ing|en)?|declin\w*|drop(s|ped|ping)?|slump\w*|slid(e|es|ing)?|lower|decreas\w*|"
    r"slow(s|ed|ing|down|er)?|weak\w*|plung\w*|tumbl\w*|lost|los(es|ing)|worst|sank|sink(s|ing)?|"
    r"shrink\w*|shrank|down\s+(\d+|double|triple|sharply|steeply)|lag(s|ged|ging)?|miss(es|ed)?|cool(s|ed|ing)|"
    r"waning|fad(e|es|ed|ing))\b|(?<![\w.])-\d|\u2212\d",
    re.I,
)


# A customer metric in a headline that names a mapped brand: trade press often reports the
# tracker's numbers without naming the tracker ("Taco Bell's traffic still down double digits").
_METRIC = re.compile(
    r"\b(foot ?traffic|footfall|visits?|visitors|downloads?|installs?|sign-?ups|"
    r"(daily|monthly|weekly) (active )?users|app users|card spend\w*|spending at)\b",
    re.I,
)
# ...but not the company's own results: that is the earnings report, not a lead on it
_EARNINGS = re.compile(
    r"\b(earnings|quarterly|results|reports? (q[1-4]|first|second|third|fourth)|same-store|"
    r"comparable|comps|guidance|outlook|eps|website traffic|web traffic|traffic stop|traffic jam)\b",
    re.I,
)


def trend_direction(text: str) -> str | None:
    """Which way a tracker's numbers point ("visits rose 7%" -> up, "traffic fell 12%" -> down).
    The news scorer's direction words are about stocks; these are about customers."""
    up, down = len(_UP.findall(text)), len(_DOWN.findall(text))
    if up > down:
        return "up"
    if down > up:
        return "down"
    return None


# --------------------------------------------------------------------------- evidence & signals


@dataclass
class Evidence:
    kind: str  # attention | app | alt_data | demand
    ticker: str
    brand: str
    weight: str  # core | major | auto
    direction: str | None  # up | down | None (attention)
    started: float  # when the change began (for the priced-in test)
    detected: float
    detail: str
    url: str = ""


@dataclass
class TrendSignal:
    id: str
    ticker: str
    brands: list[str]
    severity: str
    direction: str | None
    kinds: list[str]
    weight: str
    market_cap: float | None
    move_pct: float | None  # the stock since the evidence began
    reasons: list[str]
    evidence: list[dict[str, Any]]
    created: float
    price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def headline(self) -> str:
        arrow = {"up": "▲", "down": "▼"}.get(self.direction or "", "◆")
        kinds = " + ".join(KIND_LABEL[k] for k in self.kinds)
        move = f" · stock {self.move_pct:+.1f}% since" if self.move_pct is not None else ""
        return f"{arrow} Trend: {', '.join(self.brands)} ({self.ticker}) · {kinds}{move}"


def judge(
    evidence: list[Evidence],
    market_cap: float | None,
    move_pct: float | None,
    cfg: TrendsConfig,
) -> tuple[str, str | None, list[str]]:
    """(severity, direction, reasons) for one company's evidence inside the window."""
    kinds = sorted({e.kind for e in evidence}, key=KINDS.index)
    dirs = {e.direction for e in evidence if e.direction}
    direction = next(iter(dirs)) if len(dirs) == 1 else ("mixed" if dirs else None)
    weight = max((e.weight for e in evidence), key=lambda w: _RANK.get(w, 0))
    cap_limit = {"core": cfg.core_max_cap, "major": cfg.major_max_cap}.get(weight, cfg.auto_max_cap)
    reasons: list[str] = []
    material = market_cap is not None and market_cap <= cap_limit
    if market_cap is None:
        reasons.append("size unknown: can't judge materiality")
    elif not material:
        reasons.append(
            f"{fmt_cap(market_cap)} is too big for a {weight} brand to move it (limit {fmt_cap(cap_limit)})"
        )
    priced = False
    if move_pct is not None:
        if direction == "up":
            priced = move_pct >= cfg.priced_in_pct
        elif direction == "down":
            priced = move_pct <= -cfg.priced_in_pct
        else:
            priced = abs(move_pct) >= cfg.priced_in_pct
        if priced:
            reasons.append(f"priced in: the stock already moved {move_pct:+.1f}% since the evidence began")
    confirmed = len(kinds) >= 2 and direction in ("up", "down")
    if direction == "mixed":
        reasons.append("the evidence points both ways")
    if not material or priced or direction == "mixed":
        return "LOW", direction, reasons
    if confirmed:
        reasons.append(f"{len(kinds)} independent kinds of evidence agree ({', '.join(kinds)})")
        return "HIGH", direction, reasons
    reasons.append("one kind of evidence: shown, not pushed, until a second confirms")
    return "MEDIUM", direction, reasons


# --------------------------------------------------------------------------- the desk


class TrendDesk:
    """Collects evidence, applies the filters, emits signals and keeps score."""

    def __init__(
        self,
        cfg: TrendsConfig,
        http: Any,
        universe: Any,
        storage: Any,
        brands: list[Brand] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.universe = universe
        self.storage = storage
        self.brands = brands if brands is not None else load_brands()
        self.by_ticker: dict[str, list[Brand]] = {}
        self._terms: dict[str, Brand] = {}
        for b in self.brands:
            self.by_ticker.setdefault(b.ticker, []).append(b)
            for t in b.terms:
                self._terms.setdefault(t, b)
        alts = "|".join(re.escape(t) for t in sorted(self._terms, key=len, reverse=True))
        self._term_re = re.compile(rf"(?<![\w])({alts})(?![\w])") if alts else None
        self.state_path = state_path
        self.evidence: dict[str, list[Evidence]] = {}
        self.app_history: dict[str, list[tuple[float, int | None]]] = {}
        self.app_names: dict[str, tuple[str, str]] = {}  # app id -> (name, developer)
        self.chart_since: float | None = None  # first snapshot of the current unbroken watch
        self.last_snapshot = 0.0
        self.last_wiki_day = ""
        self.last_apps = 0.0
        self.last_marks_day = ""
        self.missing_articles: set[str] = set()
        self.canonical: dict[str, str] = {}  # title in trends.yaml -> the article it resolves to
        self.quiet_articles: set[str] = set()
        self.last_resolve_day = ""
        self.attention: dict[str, dict[str, Any]] = {}  # article -> latest 7-day vs baseline read
        self.last_chart: list[dict[str, Any]] = []
        self.stats = {
            "wiki_reads": 0,
            "wiki_errors": 0,
            "app_snapshots": 0,
            "app_errors": 0,
            "news": 0,
            "signals": 0,
        }
        storage.db.executescript(
            """CREATE TABLE IF NOT EXISTS trend_signals (
                   id TEXT PRIMARY KEY, ticker TEXT NOT NULL, created REAL NOT NULL, severity TEXT NOT NULL,
                   direction TEXT, price REAL, payload TEXT NOT NULL);
               CREATE INDEX IF NOT EXISTS idx_trend_ticker ON trend_signals(ticker, created);"""
        )
        self._load()

    # ------------------------------------------------------------------ state

    def _load(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.app_history = {k: [tuple(x) for x in v] for k, v in data.get("app_history", {}).items()}  # type: ignore[misc]
        self.app_names = {k: tuple(v) for k, v in data.get("app_names", {}).items()}  # type: ignore[misc]
        self.evidence = {k: [Evidence(**e) for e in v] for k, v in data.get("evidence", {}).items()}
        self.chart_since = data.get("chart_since")
        self.last_snapshot = float(data.get("last_snapshot") or 0.0)
        self.last_wiki_day = data.get("last_wiki_day", "")
        self.last_marks_day = data.get("last_marks_day", "")

    def save(self) -> None:
        if not self.state_path:
            return
        cutoff = time.time() - 30 * 86400
        history = {k: [x for x in v if x[0] >= cutoff] for k, v in self.app_history.items()}
        history = {k: v for k, v in history.items() if any(r is not None for _, r in v)}  # gone a month
        data = {
            "app_history": history,
            "app_names": {k: v for k, v in self.app_names.items() if k in history},
            "chart_since": self.chart_since,
            "last_snapshot": self.last_snapshot,
            "evidence": {k: [asdict(e) for e in v] for k, v in self.evidence.items()},
            "last_wiki_day": self.last_wiki_day,
            "last_marks_day": self.last_marks_day,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("trends: could not save state: %s", exc)

    # ------------------------------------------------------------------ evidence

    def add(self, ev: Evidence) -> bool:
        """Keep one piece of evidence per (kind, brand) inside the window; True if it's new."""
        lst = self.evidence.setdefault(ev.ticker, [])
        cutoff = ev.detected - self.cfg.window_days * 86400
        lst[:] = [e for e in lst if e.detected >= cutoff]
        if any(e.kind == ev.kind and e.brand == ev.brand for e in lst):
            return False
        lst.append(ev)
        return True

    def _listing(self, ticker: str) -> Any | None:
        li = self.universe.get(ticker) if self.universe is not None else None
        return li if li is not None and getattr(li, "common", True) else None

    async def resolve_wiki_titles(self) -> bool:
        """Follow every mapped title's normalization and redirects to its real article, 50 titles
        a request; titles with no article go to ``missing_articles``. False if unreachable."""
        titles = list(dict.fromkeys(w for b in self.brands for w in b.wiki))
        ok = True
        for i in range(0, len(titles), 50):
            batch = titles[i : i + 50]
            try:
                resp = await self.http.get(
                    WIKI_TITLES_URL,
                    headers={"User-Agent": WIKI_UA},
                    params={
                        "action": "query",
                        "format": "json",
                        "formatversion": "2",
                        "redirects": "1",
                        "titles": "|".join(t.replace("_", " ") for t in batch),
                    },
                )
                canonical, gone = resolve_titles(resp.json(), batch)
            except Exception as exc:  # noqa: BLE001 - fall back to the titles as written
                log.info("trends: title lookup failed: %s", exc)
                ok = False
                continue
            self.canonical.update(canonical)
            self.missing_articles |= gone
            self.missing_articles -= set(canonical)
        return ok

    def redirected(self) -> dict[str, str]:
        """Titles in trends.yaml that are really another article (worth correcting there)."""
        return {a: c for a, c in self.canonical.items() if a != c}

    async def scan_wiki(self, now: float | None = None) -> list[str]:
        """One read per mapped article (70 days of daily views). Returns tickers with new evidence."""
        now = now or time.time()
        end = datetime.fromtimestamp(now, ET).date() - timedelta(days=1)
        start = end - timedelta(days=69)
        if self.last_resolve_day != end.isoformat() and await self.resolve_wiki_titles():
            self.last_resolve_day = end.isoformat()
        touched: list[str] = []
        reads = self.stats["wiki_reads"]
        for b in self.brands:
            for art in b.wiki:
                if art in self.missing_articles:
                    continue
                url = WIKI_URL.format(
                    article=quote(self.canonical.get(art, art), safe=""),
                    start=start.strftime("%Y%m%d00"),
                    end=end.strftime("%Y%m%d00"),
                )
                try:
                    resp = await self.http.get(url, headers={"User-Agent": WIKI_UA})
                    items = (resp.json() or {}).get("items") or []
                except Exception as exc:  # noqa: BLE001 - one article must not stop the scan
                    self.stats["wiki_errors"] += 1
                    if getattr(exc, "status", None) == 404:  # a wrong title in trends.yaml
                        self.missing_articles.add(art)
                    log.info("trends: wiki %s failed: %s", art, exc)
                    continue
                self.stats["wiki_reads"] += 1
                views = [float(i.get("views", 0)) for i in items]
                st = wiki_stats(views)
                if views and sum(views) / len(views) < QUIET_VIEWS:
                    self.quiet_articles.add(art)  # a redirect or the wrong article: check trends.yaml
                else:
                    self.quiet_articles.discard(art)
                if st is not None:
                    self.attention[art] = {
                        "brand": b.brand,
                        "ticker": b.ticker,
                        "ratio": round(st["ratio"], 2),
                        "z": round(st["z"], 1),
                        "avg7": round(st["avg7"]),
                        "baseline": round(st["baseline"]),
                    }
                spike = wiki_spike(
                    views,
                    self.cfg.wiki_min_views,
                    self.cfg.wiki_ratio,
                    self.cfg.wiki_z,
                    self.cfg.wiki_sustained_days,
                )
                if spike is None:
                    continue
                ev = Evidence(
                    kind="attention",
                    ticker=b.ticker,
                    brand=b.brand,
                    weight=b.weight,
                    direction=None,
                    started=now - spike["first_day"] * 86400,
                    detected=now,
                    detail=(
                        f"Wikipedia views {spike['ratio']}x normal ({spike['avg7']:,}/day vs {spike['baseline']:,}), "
                        f"up on {spike['days_up']} of the last 7 days"
                    ),
                    url=f"https://en.wikipedia.org/wiki/{quote(art)}",
                )
                if self.add(ev):
                    touched.append(b.ticker)
                await asyncio.sleep(0.2)  # Wikimedia asks for gentle clients
        if self.stats["wiki_reads"] > reads:  # a scan that read nothing (offline) is retried
            self.last_wiki_day = end.isoformat()
        return touched

    def _brand_for_app(self, name: str, artist: str) -> tuple[str, str, str] | None:
        """(ticker, brand, weight) for an app: the brand map first, then the developer's exact
        listed name ("Robinhood Markets, Inc." -> HOOD)."""
        hay = f" {name.lower()} | {artist.lower()} "
        for b in self.brands:
            for a in b.apps:
                if re.search(rf"(?<![\w]){re.escape(a.lower())}(?![\w])", hay):
                    return b.ticker, b.brand, b.weight
        if self.universe is not None and artist:
            li = self.universe.lookup_name(artist)
            if li is not None:
                return li.symbol, name, "auto"
        return None

    def record_apps(self, chart: list[dict[str, Any]], now: float | None = None) -> list[str]:
        """Add one chart snapshot; return tickers with a new climb."""
        now = now or time.time()
        self.stats["app_snapshots"] += 1
        if self.chart_since is None or now - self.last_snapshot > 2 * 86400:
            self.chart_since = now  # first snapshot, or back after days offline: watching starts over
        self.last_snapshot = now
        ranks = {a["id"]: a["rank"] for a in chart}
        for a in chart:
            self.app_names[a["id"]] = (a["name"], a["artist"])
        for app_id in set(self.app_history) | set(ranks):
            self.app_history.setdefault(app_id, []).append((now, ranks.get(app_id)))
        touched = []
        for app_id, hist in self.app_history.items():
            if app_id not in ranks:
                continue
            climb = app_climb(
                hist, self.cfg.app_top, self.cfg.app_from, self.cfg.app_hold_snapshots, now, self.chart_since
            )
            if climb is None:
                continue
            name, artist = self.app_names.get(app_id, ("", ""))
            mapped = self._brand_for_app(name, artist)
            if mapped is None:
                continue
            ticker, brand, weight = mapped
            was = f"#{climb['was']}" if climb["was"] else "outside the top 200"
            ev = Evidence(
                kind="app",
                ticker=ticker,
                brand=brand,
                weight=weight,
                direction="up",
                started=climb["since"],
                detected=now,
                detail=f"{name} #{climb['rank']} on the US App Store free chart (two weeks ago {was})",
                url=f"https://apps.apple.com/us/app/id{app_id}",
            )
            if self.add(ev):
                touched.append(ticker)
        return touched

    async def scan_apps(self, now: float | None = None) -> list[str]:
        try:
            resp = await self.http.get(APPS_URL)
            chart = parse_app_chart(resp.json())
        except Exception as exc:  # noqa: BLE001
            self.stats["app_errors"] += 1
            log.info("trends: app chart failed: %s", exc)
            return []
        if not chart:
            self.stats["app_errors"] += 1
            return []
        self.last_apps = now or time.time()
        self.last_chart = chart
        return self.record_apps(chart, now)

    def mapped_apps(self, chart: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """The chart's apps that belong to a listed company, with their rank."""
        out = []
        for a in chart if chart is not None else self.last_chart:
            m = self._brand_for_app(a["name"], a["artist"])
            if m is not None:
                out.append(
                    {"rank": a["rank"], "app": a["name"], "ticker": m[0], "brand": m[1], "weight": m[2]}
                )
        return out

    def heating_up(self, limit: int = 10) -> list[dict[str, Any]]:
        """Mapped brands by Wikipedia attention vs their own 8-week baseline (not yet a signal)."""
        rows = sorted(self.attention.values(), key=lambda r: -r["ratio"])
        return [r for r in rows if r["avg7"] >= self.cfg.wiki_min_views / 2][:limit]

    def from_news(self, item: Any, analysis: Any) -> list[str]:
        """Tracker data and sell-out headlines become evidence for the brands they name: a news
        theme (the tracker or the sell-out is named), or a mapped brand with a customer metric
        that points somewhere ("Taco Bell's traffic still down double digits")."""
        kinds = [NEWS_THEMES[t] for t in analysis.themes if t in NEWS_THEMES]
        title = item.title
        if _EARNINGS.search(title):  # the company's own results are the report, not a lead on it
            kinds = [k for k in kinds if k != "alt_data"]
        brands = self._brands_in(title)
        if not kinds:
            metric = _METRIC.search(title) or self._brand_traffic(title)
            if not (brands and metric and not _EARNINGS.search(title) and trend_direction(title)):
                return []
            kinds = ["alt_data"]
        self.stats["news"] += 1
        hits = [(b.ticker, b.brand, b.weight) for b in brands]
        if not hits:
            for t in analysis.tickers[:3]:
                li = self._listing(t)
                if li is not None and t not in ("SPY", "QQQ", "IWM", "DIA"):
                    hits.append((t, li.name, "auto"))
                    break
        numbers = trend_direction(item.title)
        direction = numbers or (analysis.direction if analysis.direction in ("up", "down") else None)
        touched = []
        now = min(item.published or item.detected, item.detected)  # when it was reported
        for ticker, brand, weight in hits:
            for kind in kinds:
                # tracker data needs numbers that point somewhere; a sell-out is demand unless the
                # headline says otherwise ("sales slow after the sell-out")
                d = direction if kind == "alt_data" else (numbers or "up")
                ev = Evidence(kind, ticker, brand, weight, d, now, now, item.title[:160], item.url)
                if self.add(ev):
                    touched.append(ticker)
        return touched

    def _brand_traffic(self, title: str) -> bool:
        """Bare "traffic" counts only when it's the brand's ("Taco Bell's traffic", "traffic at
        Chipotle"), not the roads ("Lyft says traffic in San Francisco is up 5%")."""
        if self._term_re is None:
            return False
        t = self._term_re.pattern
        return bool(
            re.search(
                rf"{t}(?:'s|’s)?\s+(?:restaurant\s+|store\s+|guest\s+|customer\s+)?traffic\b", title.lower()
            )
            or re.search(rf"\btraffic\s+(?:at|to)\s+{t}", title.lower())
        )

    def _brands_in(self, title: str) -> list[Brand]:
        if self._term_re is None:
            return []
        found: dict[str, Brand] = {}
        for m in self._term_re.finditer(title.lower()):
            b = self._terms[m.group(1)]
            found.setdefault(b.brand, b)
        return list(found.values())

    # ------------------------------------------------------------------ signals

    async def stock_move(
        self, ticker: str, since: float, until: float | None = None
    ) -> tuple[float | None, float | None]:
        """(% move from ``since`` to ``until`` (default: the latest bar), price then) from Yahoo
        daily bars."""
        try:
            resp = await self.http.get(
                CHART_URL.format(symbol=ticker), params={"range": "6mo", "interval": "1d"}
            )
            snap = parse_chart(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.info("trends: price %s failed: %s", ticker, exc)
            return None, None
        pts = (snap or {}).get("points") or []
        if not pts:
            return None, None
        before = [p for t, p in pts if t <= since]
        ref = before[-1] if before else pts[0][1]
        upto = [p for t, p in pts if until is None or t <= until]
        last = upto[-1] if upto else pts[-1][1]
        return ((last / ref - 1.0) * 100.0 if ref else None), last

    def _last_signal(self, ticker: str) -> tuple[float, str] | None:
        row = self.storage.db.execute(
            "SELECT created, severity FROM trend_signals WHERE ticker=? ORDER BY created DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    async def evaluate(self, ticker: str, now: float | None = None) -> TrendSignal | None:
        now = now or time.time()
        evs = [e for e in self.evidence.get(ticker, []) if now - e.detected <= self.cfg.window_days * 86400]
        if not evs:
            return None
        li = self._listing(ticker)
        if li is None:
            return None  # not a listed common stock: no trade, no signal
        since = min(e.started for e in evs)
        move, price = await self.stock_move(ticker, since, now)
        severity, direction, reasons = judge(evs, li.market_cap, move, self.cfg)
        last = self._last_signal(ticker)
        if last and now - last[0] < self.cfg.cooldown_days * 86400 and _SEV[severity] <= _SEV.get(last[1], 0):
            return None  # nothing stronger than what was already reported this month
        kinds = sorted({e.kind for e in evs}, key=KINDS.index)
        sig = TrendSignal(
            id=uuid.uuid4().hex[:12],
            ticker=ticker,
            brands=sorted({e.brand for e in evs}),
            severity=severity,
            direction=direction,
            kinds=kinds,
            weight=max((e.weight for e in evs), key=lambda w: _RANK.get(w, 0)),
            market_cap=li.market_cap,
            move_pct=round(move, 2) if move is not None else None,
            reasons=reasons,
            evidence=[asdict(e) for e in sorted(evs, key=lambda e: e.detected)],
            created=now,
            price=price,
        )
        self.storage.db.execute(
            "INSERT INTO trend_signals (id, ticker, created, severity, direction, price, payload) VALUES (?,?,?,?,?,?,?)",
            (sig.id, ticker, now, severity, direction, price, json.dumps(sig.to_dict())),
        )
        self.stats["signals"] += 1
        return sig

    def board(self, days: float = 30, now: float | None = None) -> list[dict[str, Any]]:
        """The latest signal per company inside ``days``, strongest first."""
        now = now or time.time()
        rows = self.storage.db.execute(
            "SELECT payload FROM trend_signals WHERE created >= ? ORDER BY created DESC",
            (now - days * 86400,),
        ).fetchall()
        seen: dict[str, dict[str, Any]] = {}
        for (payload,) in rows:
            d = json.loads(payload)
            seen.setdefault(d["ticker"], d)
        return sorted(seen.values(), key=lambda d: (-_SEV[d["severity"]], -d["created"]))

    # ------------------------------------------------------------------ the scorecard

    async def mark_outcomes(self, now: float | None = None, horizons: tuple[int, ...] = (30, 60, 90)) -> int:
        """Score each signal against SPY at 30/60/90 days. Returns how many marks were added."""
        now = now or time.time()
        rows = self.storage.db.execute(
            "SELECT id, ticker, created, payload FROM trend_signals WHERE created <= ?",
            (now - min(horizons) * 86400,),
        ).fetchall()
        added = 0
        cache: dict[str, list[tuple[float, float]]] = {}
        for sid, ticker, created, payload in rows:
            d = json.loads(payload)
            marks = d.setdefault("marks", {})
            due = [h for h in horizons if str(h) not in marks and now - created >= h * 86400]
            if not due:
                continue
            for h in due:
                stock = await self._return_between(ticker, created, created + h * 86400, cache)
                spy = await self._return_between("SPY", created, created + h * 86400, cache)
                if stock is None or spy is None:
                    continue
                marks[str(h)] = {
                    "stock": round(stock, 2),
                    "spy": round(spy, 2),
                    "excess": round(stock - spy, 2),
                }
                added += 1
            self.storage.db.execute("UPDATE trend_signals SET payload=? WHERE id=?", (json.dumps(d), sid))
        return added

    async def _return_between(
        self, ticker: str, t0: float, t1: float, cache: dict[str, list[tuple[float, float]]] | None = None
    ) -> float | None:
        cache = cache if cache is not None else {}
        if ticker not in cache:
            try:
                resp = await self.http.get(
                    CHART_URL.format(symbol=ticker), params={"range": "1y", "interval": "1d"}
                )
                cache[ticker] = (parse_chart(resp.json()) or {}).get("points") or []
            except Exception:  # noqa: BLE001 - marked on a later day
                cache[ticker] = []
        pts = cache[ticker]
        a = [p for t, p in pts if t <= t0]
        b = [p for t, p in pts if t <= t1]
        if not a or not b or not a[-1]:
            return None
        return (b[-1] / a[-1] - 1.0) * 100.0

    def scorecard(self) -> dict[str, Any]:
        """Hit rate and average excess return vs SPY, by severity and horizon. A hit: the stock
        beat SPY in the signal's direction (for direction-free attention, any 5%+ excess move)."""
        out: dict[str, Any] = {}
        for (payload,) in self.storage.db.execute("SELECT payload FROM trend_signals").fetchall():
            d = json.loads(payload)
            for h, m in (d.get("marks") or {}).items():
                key = f"{d['severity']}/{h}d"
                s = out.setdefault(key, {"n": 0, "hits": 0, "excess": 0.0})
                ex = m["excess"]
                hit = (
                    ex > 0
                    if d.get("direction") == "up"
                    else ex < 0
                    if d.get("direction") == "down"
                    else abs(ex) >= 5
                )
                s["n"] += 1
                s["hits"] += int(hit)
                s["excess"] += ex if d.get("direction") != "down" else -ex
        for s in out.values():
            s["hit_rate"] = round(s["hits"] / s["n"], 3) if s["n"] else None
            s["avg_excess"] = round(s["excess"] / s["n"], 2) if s["n"] else None
            del s["excess"]
        total = self.storage.db.execute("SELECT COUNT(*) FROM trend_signals").fetchone()[0]
        return {"signals": total, "by": dict(sorted(out.items()))}

    # ------------------------------------------------------------------ loop

    async def run(self, on_signals: Any, stop: asyncio.Event) -> None:
        """Wikipedia once a day (after 06:00 ET, when yesterday's views are final), the App Store
        chart every 6 hours, outcome marks once a day."""
        while not stop.is_set():
            now = time.time()
            dt = datetime.fromtimestamp(now, ET)
            touched: list[str] = []
            try:
                yesterday = (dt.date() - timedelta(days=1)).isoformat()
                if dt.hour >= 6 and self.last_wiki_day != yesterday:
                    touched += await self.scan_wiki(now)
                if now - self.last_apps >= 6 * 3600:
                    touched += await self.scan_apps(now)
                signals = [s for s in [await self.evaluate(t, now) for t in dict.fromkeys(touched)] if s]
                if signals:
                    await on_signals(signals)
                if dt.hour >= 7 and self.last_marks_day != dt.date().isoformat():
                    self.last_marks_day = dt.date().isoformat()
                    await self.mark_outcomes(now)
                self.save()
            except Exception as exc:  # noqa: BLE001 - the lane must never stop the engine
                log.warning("trends loop: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1800)
            except asyncio.TimeoutError:
                pass

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "brands": len(self.brands),
            "companies": len(self.by_ticker),
            "apps_tracked": len(self.app_history),
            "last_wiki_day": self.last_wiki_day or None,
            "last_app_snapshot": self.last_apps or None,
            "missing_articles": sorted(self.missing_articles),
            "quiet_articles": sorted(self.quiet_articles),
            "redirected_titles": self.redirected(),
            "heating_up": self.heating_up(5),
            **self.stats,
        }


def format_board(board: list[dict[str, Any]], card: dict[str, Any]) -> str:
    """Plain text for ``news247 trends``."""
    lines = []
    if not board:
        lines.append("No trend signals in the window yet.")
    for r in board:
        sig = TrendSignal(**{k: r[k] for k in TrendSignal.__dataclass_fields__ if k in r})
        when = datetime.fromtimestamp(r["created"], ET).strftime("%b %d %H:%M")
        lines.append(f"[{r['severity']:<6}] {when} ET  {sig.headline}")
        for e in r["evidence"]:
            lines.append(f"           • {KIND_LABEL[e['kind']]}: {e['detail']}")
        for why in r["reasons"]:
            lines.append(f"           → {why}")
        for h, m in sorted((r.get("marks") or {}).items(), key=lambda kv: int(kv[0])):
            lines.append(
                f"           {h}d: stock {m['stock']:+.1f}% vs SPY {m['spy']:+.1f}% (excess {m['excess']:+.1f})"
            )
    lines.append("")
    lines.append(f"Scorecard: {card['signals']} signals recorded")
    if not card["by"]:
        lines.append("  no outcomes yet (each signal is marked 30, 60 and 90 days after it fires)")
    for key, s in card["by"].items():
        lines.append(
            f"  {key:<11} n={s['n']:<4} hit rate {s['hit_rate']:.0%}  avg excess vs SPY {s['avg_excess']:+.1f}%"
        )
    return "\n".join(lines)


def signal_alert_text(sig: TrendSignal) -> tuple[str, str]:
    """(title, body) for an alert."""
    lines = []
    for e in sig.evidence:
        lines.append(f"• {KIND_LABEL[e['kind']]}: {e['detail']}")
    cap = f"{fmt_cap(sig.market_cap)}" if sig.market_cap else "size unknown"
    move = f"{sig.move_pct:+.1f}%" if sig.move_pct is not None else "n/a"
    lines.append(f"{sig.ticker} · {cap} · {sig.weight} brand · stock since the evidence began: {move}")
    lines.extend(f"→ {r}" for r in sig.reasons)
    lines.append("Trend signals are leads to research, not results; the scorecard tracks how they do.")
    return sig.headline, "\n".join(lines)
