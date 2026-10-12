"""The Camillo desk: Chris Camillo's social arbitrage, run as rules over the trend lane.

The trend lane (trends.py) collects the evidence and filters it. This module adds the pieces
that make it Camillo's method rather than a generic trend screen:

* **TickerTags chatter.** Camillo's own company, TickerTags, mapped words to tickers and
  tracked how fast people talked about them. ``ChatterTags`` does the same over Bluesky's
  public firehose (Jetstream, free): every brand term in ``data/trends.yaml`` is a tag, counted
  per day against the posts sampled, so a rising share of the conversation is a signal and a
  busier network isn't.
* **The catalyst.** The trade works when the market learns what you already know, usually at
  the company's next report. ``EarningsBook`` reads Nasdaq's free earnings calendar for every
  company on the board.
* **The checklist.** Each idea is shown against the method's tests, pass or fail, with the
  numbers behind each (``checklist``). The playbook text (principles, rules in his words,
  case studies with sources) lives in ``data/camillo.yaml``.
* **The attention trade.** "Where people have their attention, even if the information is not
  truthful" (Iced Coffee Hour, Aug 2026): he measures how fast a market narrative is
  accelerating, because a fast one in a levered market precedes a sharp move. ``Narrative`` /
  ``attention_trade`` / ``NarrativeWatch`` measure each narrative in ``data/camillo.yaml`` over the
  headlines this system reads and over Bluesky, and alert when one accelerates.
* **The thesis can break.** He exits "in an instant" when something goes against the thesis
  (Sweetgreen: a lettuce outbreak made his menu thesis irrelevant). ``thesis_check`` looks for
  that kind of shock in the brand's headlines since the evidence began; ``other_side`` shows the
  headlines arguing against each idea, because he studies the other side more than his own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiohttp

from .config import load_package_yaml

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
JETSTREAM_HOSTS = (
    "wss://jetstream2.us-east.bsky.network/subscribe",
    "wss://jetstream1.us-east.bsky.network/subscribe",
    "wss://jetstream1.us-west.bsky.network/subscribe",
    "wss://jetstream2.us-west.bsky.network/subscribe",
)
_TIMES = {"time-pre-market": "before the open", "time-after-hours": "after the close"}


def load_playbook(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return data if data is not None else (load_package_yaml("camillo.yaml") or {})


# --------------------------------------------------------------------------- the catalyst


def parse_nasdaq_earnings(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Nasdaq's earnings calendar for one day -> [(symbol, "before the open" | "after the close" | "")]."""
    rows = (((data or {}).get("data") or {}).get("rows")) or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        if sym:
            out.append((sym, _TIMES.get(str(r.get("time") or ""), "")))
    return out


class EarningsBook:
    """The next report date for every listed company, from Nasdaq's day-by-day calendar."""

    def __init__(self, http: Any, days: int = 75) -> None:
        self.http = http
        self.days = days
        self.next: dict[str, dict[str, str]] = {}  # symbol -> {"date", "when"}
        self.last_day = ""
        self.errors = 0

    def load(self, data: dict[str, Any]) -> None:
        self.next = dict(data.get("next") or {})
        self.last_day = str(data.get("last_day") or "")

    def dump(self) -> dict[str, Any]:
        return {"next": self.next, "last_day": self.last_day}

    async def refresh(self, today: date | None = None, pause: float = 0.3) -> int:
        """Read the next ``days`` weekdays; returns how many days were read."""
        today = today or datetime.now(ET).date()
        found: dict[str, dict[str, str]] = {}
        read = 0
        for i in range(self.days):
            d = today + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            try:
                resp = await self.http.get(
                    EARNINGS_URL, params={"date": d.isoformat()}, headers=NASDAQ_HEADERS
                )
                rows = parse_nasdaq_earnings(resp.json())
            except Exception as exc:  # noqa: BLE001 - a missing day must not stop the book
                self.errors += 1
                log.info("camillo: earnings %s failed: %s", d, exc)
                continue
            read += 1
            for sym, when in rows:
                found.setdefault(sym, {"date": d.isoformat(), "when": when})
            if pause:
                await asyncio.sleep(pause)
        if read:
            # keep dates we knew that are still ahead (a day that failed to load today)
            for sym, v in self.next.items():
                if sym not in found and v.get("date", "") >= today.isoformat():
                    found[sym] = v
            self.next = found
            self.last_day = today.isoformat()
        return read

    def lookup(self, symbol: str, today: date | None = None) -> dict[str, Any] | None:
        v = self.next.get(symbol.upper())
        if not v:
            return None
        today = today or datetime.now(ET).date()
        days = (date.fromisoformat(v["date"]) - today).days
        if days < 0:
            return None
        return {**v, "in_days": days}


# --------------------------------------------------------------------------- the attention trade
#
# "I'm really paying attention more so than ever to the attention trade, meaning like where people
# have their attention, even if the information is not truthful." He wants to "measure the
# acceleration of that narrative because if it starts to accelerate quickly then that is a
# signal, especially if we get into a levered state again, that we could see a massive
# correction." His miss: "I knew Kimi was coming… but I did not place enough emphasis on the
# acceleration of the Kimi narrative as it was happening." (Iced Coffee Hour, Aug 2026.)
#
# A narrative here is a set of words (``data/camillo.yaml`` -> ``narratives``). Its pulse is its
# share of everything read each day (headlines this system stored; Bluesky posts sampled), the
# last 3 days against the median of the 14 before, the same shape as the chatter test.


def _terms_re(terms: list[str]) -> re.Pattern[str] | None:
    """Whole-word alternation; a trailing ``*`` is a stem ("deleverag*")."""
    alts = []
    for t in sorted({str(t).strip().lower() for t in terms if str(t).strip()}, key=len, reverse=True):
        stem = t.endswith("*")
        body = re.escape(t.rstrip("*")).replace(r"\ ", r"[\s-]+")
        alts.append(body + (r"\w*" if stem else ""))
    if not alts:
        return None
    return re.compile(rf"(?<![\w])(?:{'|'.join(alts)})(?![\w])", re.I)


@dataclass
class Narrative:
    """A market narrative: a headline or post belongs to it when it says one of ``any`` and,
    when ``with_`` is set, also one of ``with_``."""

    id: str
    name: str
    side: str = "fear"  # fear | hope: which way it pushes the names in ``hits``
    sector: str = ""
    any: list[str] = field(default_factory=list)
    with_: list[str] = field(default_factory=list)
    hits: list[str] = field(default_factory=list)  # the listed names it moves most (this desk's list)
    note: str = ""

    def __post_init__(self) -> None:
        self._any = _terms_re(self.any)
        self._with = _terms_re(self.with_)

    def matches(self, text: str) -> bool:
        if not text or self._any is None or not self._any.search(text):
            return False
        return self._with is None or bool(self._with.search(text))

    def like_terms(self) -> list[str]:
        """SQL LIKE fragments that every match must contain one of (a cheap prefilter)."""
        return [str(t).strip().rstrip("*") for t in self.any if len(str(t).strip().rstrip("*")) >= 2]


def load_narratives(playbook: dict[str, Any] | None = None) -> list[Narrative]:
    out = []
    for n in load_playbook(playbook).get("narratives") or []:
        out.append(
            Narrative(
                id=str(n["id"]),
                name=str(n["name"]),
                side=str(n.get("side") or "fear"),
                sector=str(n.get("sector") or ""),
                any=list(n.get("any") or []),
                with_=list(n.get("with") or []),
                hits=[str(t).upper() for t in n.get("hits") or []],
                note=str(n.get("note") or ""),
            )
        )
    return out


def narrative_pulse(
    series: list[tuple[str, int, int]],
    ratio: float = 2.0,
    min_count: int = 8,
    min_total: int = 20,
    per: float = 1000.0,
) -> dict[str, Any]:
    """``series``: (day, matching, everything read) oldest first. Returns the narrative's level:

    * ``accelerating``: the last 3 days' share is ``ratio``x the median of the 14 days before (at
      least 5 of them read), with ``min_count`` matches, and the latest day is not below the
      first of the three (still rising, not fading);
    * ``elevated``: at least 1.5x with half the matches, or at ``ratio``x but fading;
    * ``quiet`` otherwise, or ``learning`` until there is enough history.
    """
    days = [(d, n, t) for d, n, t in series if t >= min_total]
    recent, base = days[-3:], days[-17:-3]
    out: dict[str, Any] = {
        "level": "learning",
        "ratio": None,
        "count": sum(n for _, n, _ in recent),
        "share": None,
        "baseline": None,
        "days": [(d, n) for d, n, _ in days[-7:]],
    }
    if len(recent) < 3 or len(base) < 5:
        return out
    shares = [n / t * per for _, n, t in recent]
    med = statistics.median(n / t * per for _, n, t in base)
    floor = max(med, 0.5 * per / statistics.median(t for _, _, t in base))  # half a match a day
    share = sum(n for _, n, _ in recent) / sum(t for _, _, t in recent) * per
    r = share / floor
    rising = shares[-1] >= shares[0]
    count = out["count"]
    if r >= ratio and count >= min_count and rising:
        level = "accelerating"
    elif (r >= 1.5 and count >= max(2, min_count // 2)) or (r >= ratio and count >= min_count):
        level = "elevated"
    else:
        level = "quiet"
    out.update(
        level=level,
        ratio=round(r, 1),
        share=round(share, 2),
        baseline=round(med, 2),
        rising=rising,
    )
    return out


def narrative_series(
    storage: Any, narratives: list[Narrative], now: float, days: int = 17
) -> tuple[dict[str, list[tuple[str, int, int]]], dict[str, list[dict[str, Any]]], dict[str, int]]:
    """Per narrative: (day, headlines matching, headlines read) for the last ``days`` days (ET),
    the latest matching headlines, and how many distinct sources told it in the last 3 days."""
    since = now - days * 86400
    totals: dict[str, int] = {}
    for (det,) in storage.db.execute("SELECT detected FROM items WHERE detected >= ?", (since,)):
        d = datetime.fromtimestamp(det, ET).date().isoformat()
        totals[d] = totals.get(d, 0) + 1
    order = sorted(totals)
    recent_days = set(order[-3:])
    series: dict[str, list[tuple[str, int, int]]] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, int] = {}
    for n in narratives:
        terms = n.like_terms()
        counts: dict[str, int] = {}
        srcs: set[str] = set()
        ex: list[dict[str, Any]] = []
        if terms:
            where = " OR ".join("title LIKE ?" for _ in terms)
            rows = storage.db.execute(
                f"SELECT title, source, url, detected FROM items WHERE detected >= ? AND ({where}) "  # noqa: S608
                "ORDER BY detected DESC",
                (since, *[f"%{t}%" for t in terms]),
            ).fetchall()
            for title, source, url, det in rows:
                if not n.matches(title or ""):
                    continue
                d = datetime.fromtimestamp(det, ET).date().isoformat()
                counts[d] = counts.get(d, 0) + 1
                if d in recent_days:
                    srcs.add(source)
                if len(ex) < 3:
                    ex.append({"title": title, "source": source, "url": url or "", "detected": det})
        series[n.id] = [(d, counts.get(d, 0), totals[d]) for d in order]
        examples[n.id] = ex
        sources[n.id] = len(srcs)
    return series, examples, sources


_LEVEL = {"accelerating": 3, "elevated": 2, "quiet": 1, "learning": 0}


def attention_trade(
    storage: Any,
    narratives: list[Narrative],
    chatter: Any = None,
    now: float | None = None,
    ratio: float = 2.0,
    min_count: int = 8,
) -> list[dict[str, Any]]:
    """Every narrative's pulse in the news (headlines read) and on Bluesky (posts sampled),
    the fastest first."""
    now = now or time.time()
    if storage is None or not narratives:
        return []
    series, examples, sources = narrative_series(storage, narratives, now)
    today = datetime.fromtimestamp(now, ET).date().isoformat()
    rows = []
    for n in narratives:
        news = narrative_pulse(series[n.id], ratio, min_count)
        social = None
        if chatter is not None and getattr(chatter, "narratives", None):
            social = narrative_pulse(chatter.nseries(n.id, today), ratio, min_count, min_total=5000, per=1e5)
        best = max([news, social] if social else [news], key=lambda p: _LEVEL[p["level"]])
        rows.append(
            {
                "id": n.id,
                "name": n.name,
                "side": n.side,
                "sector": n.sector,
                "note": n.note,
                "hits": n.hits,
                "level": best["level"],
                "news": news,
                "social": social,
                "sources_3d": sources[n.id],
                "examples": examples[n.id],
            }
        )
    rows.sort(key=lambda r: (-_LEVEL[r["level"]], -(r["news"]["ratio"] or 0)))
    return rows


class NarrativeWatch:
    """Alerts once per wave when a narrative starts accelerating. Several fear narratives in the
    same sector accelerating together is his FUD cycle forming: that one is HIGH."""

    def __init__(self, narratives: list[Narrative], cfg: Any = None) -> None:
        self.narratives = narratives
        self.ratio = getattr(cfg, "narrative_ratio", 2.0)
        self.min_count = getattr(cfg, "narrative_min", 8)
        self.cooldown = getattr(cfg, "narrative_cooldown_days", 3.0) * 86400
        self.alerted: dict[str, float] = {}
        self.rows: list[dict[str, Any]] = []
        self.at = 0.0

    def load(self, data: dict[str, Any]) -> None:
        self.alerted = {k: float(v) for k, v in (data.get("alerted") or {}).items()}

    def dump(self) -> dict[str, Any]:
        return {"alerted": self.alerted}

    def scan(
        self, storage: Any, chatter: Any = None, now: float | None = None, max_age: float = 600
    ) -> list[dict[str, Any]]:
        now = now or time.time()
        if not self.rows or now - self.at >= max_age:
            self.rows = attention_trade(storage, self.narratives, chatter, now, self.ratio, self.min_count)
            self.at = now
        return self.rows

    def check(self, storage: Any, chatter: Any = None, now: float | None = None) -> list[dict[str, Any]]:
        """The narratives that just started accelerating (not alerted inside the cooldown),
        each with its severity and the others accelerating with it."""
        now = now or time.time()
        rows = self.scan(storage, chatter, now, max_age=0)
        hot = [r for r in rows if r["level"] == "accelerating"]
        out = []
        for r in hot:
            if now - self.alerted.get(r["id"], 0.0) < self.cooldown:
                continue
            together = [
                x for x in hot if x["id"] != r["id"] and x["side"] == r["side"] and x["sector"] == r["sector"]
            ]
            severity = "HIGH" if r["side"] == "fear" and together else "MEDIUM"
            self.alerted[r["id"]] = now
            out.append({**r, "severity": severity, "together": [x["name"] for x in together]})
        return out


def narrative_alert_text(row: dict[str, Any]) -> tuple[str, str]:
    news, social = row["news"], row.get("social")
    arrow = "▼" if row["side"] == "fear" else "▲"
    title = f"{arrow} Narrative accelerating: {row['name']}"
    lines = [
        f"In the news: {news['count']} headlines from {row['sources_3d']} sources in 3 days, "
        f"{news['ratio']}x its share of the 14 days before."
    ]
    if social and social.get("ratio") is not None and social["level"] in ("accelerating", "elevated"):
        lines.append(f"On Bluesky: {social['count']} posts in 3 days, {social['ratio']}x normal.")
    if row.get("together"):
        lines.append("Accelerating with it: " + "; ".join(row["together"]) + ". A fear cycle is forming.")
    if row.get("hits"):
        lines.append(
            ("Names it hits: " if row["side"] == "fear" else "Names it helps: ") + ", ".join(row["hits"])
        )
    if row.get("note"):
        lines.append(row["note"])
    for ex in row.get("examples", [])[:2]:
        lines.append(f"• {ex['title']} ({ex['source']})")
    lines.append(
        "Camillo's rule: measure how fast the narrative is accelerating; a fast one in a levered market "
        "can drive a sharp move whether or not it is true. Get ahead of it, don't argue with it."
    )
    return title, "\n".join(lines)


# --------------------------------------------------------------------------- TickerTags chatter


def chatter_spike(
    series: list[tuple[str, int, int]],
    ratio: float = 3.0,
    min_mentions: int = 30,
    min_posts: int = 5000,
) -> dict[str, Any] | None:
    """``series``: (day, mentions, posts sampled) oldest first. A spike when the last 3 days'
    share of the conversation is ``ratio``x the median of the 14 days before (at least 7 of
    them sampled), on at least 2 of the 3 days, with ``min_mentions`` mentions in all."""
    days = [(d, m, p) for d, m, p in series if p >= min_posts]
    if len(days) < 10:
        return None
    recent, base = days[-3:], days[-17:-3]
    if len(base) < 7:
        return None
    rate = [m / p * 1e5 for _, m, p in recent]
    base_rates = [m / p * 1e5 for _, m, p in base]
    med = statistics.median(base_rates)
    floor = max(med, 0.5 * 1e5 / statistics.median(p for _, _, p in base))  # half a mention a day
    mentions = sum(m for _, m, _ in recent)
    avg = sum(rate) / 3
    up_days = [i for i, r in enumerate(rate) if r >= 2 * floor]
    if mentions < min_mentions or avg < ratio * floor or len(up_days) < 2:
        return None
    return {
        "ratio": round(avg / floor, 1),
        "mentions": mentions,
        "rate": round(avg, 2),
        "baseline": round(med, 2),
        "first_day": recent[up_days[0]][0],
    }


class ChatterTags:
    """Brand terms counted across Bluesky's public posts (sampled), per day."""

    def __init__(
        self,
        brands: list[Any],
        sample_s: float = 600.0,
        hosts: tuple[str, ...] = JETSTREAM_HOSTS,
        narratives: list[Narrative] | None = None,
    ) -> None:
        self.brands = {b.brand: b for b in brands}
        self.narratives = list(narratives or [])
        self.ncounts: dict[str, dict[str, int]] = {}  # day -> narrative id -> posts
        terms: dict[str, str] = {}
        for b in brands:
            for t in b.terms:
                terms.setdefault(t.lower(), b.brand)
        self.term_brand = terms
        alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
        self.term_re = re.compile(rf"(?<![\w#@])({alts})(?![\w])", re.I) if alts else None
        self.sample_s = sample_s
        self.hosts = hosts
        self.counts: dict[str, dict[str, int]] = {}  # day -> brand -> mentions
        self.posts: dict[str, int] = {}  # day -> posts sampled
        self.last_sample = 0.0
        self.errors = 0
        self.examples: dict[str, list[str]] = {}  # brand -> a few recent posts (shown on the page)

    # ------------------------------------------------------------------ counting

    def count(self, text: str, day: str) -> list[str]:
        """One post: returns the brands it mentions (each counted once per post)."""
        self.posts[day] = self.posts.get(day, 0) + 1
        if text and self.narratives:
            nb = None
            for n in self.narratives:
                if n.matches(text):
                    nb = nb if nb is not None else self.ncounts.setdefault(day, {})
                    nb[n.id] = nb.get(n.id, 0) + 1
        if self.term_re is None or not text:
            return []
        hit = {self.term_brand[m.group(1).lower()] for m in self.term_re.finditer(text)}
        bucket = self.counts.setdefault(day, {})
        for brand in hit:
            bucket[brand] = bucket.get(brand, 0) + 1
            ex = self.examples.setdefault(brand, [])
            ex.append(text[:200])
            del ex[:-3]
        return sorted(hit)

    def series(self, brand: str, today: str | None = None) -> list[tuple[str, int, int]]:
        days = sorted(self.posts)
        if today:
            days = [d for d in days if d <= today]
        return [(d, self.counts.get(d, {}).get(brand, 0), self.posts[d]) for d in days]

    def nseries(self, narrative_id: str, today: str | None = None) -> list[tuple[str, int, int]]:
        """(day, posts matching the narrative, posts sampled), oldest first."""
        days = sorted(self.posts)
        if today:
            days = [d for d in days if d <= today]
        return [(d, self.ncounts.get(d, {}).get(narrative_id, 0), self.posts[d]) for d in days]

    def prune(self, keep_days: int = 30) -> None:
        cutoff = (datetime.now(ET).date() - timedelta(days=keep_days)).isoformat()
        for d in [d for d in self.posts if d < cutoff]:
            self.posts.pop(d, None)
            self.counts.pop(d, None)
            self.ncounts.pop(d, None)

    def heating_up(self, limit: int = 8, min_posts: int = 5000) -> list[dict[str, Any]]:
        """Brands by their last-3-day share of the conversation vs the 14 days before."""
        rows = []
        for brand, b in self.brands.items():
            s = [(d, m, p) for d, m, p in self.series(brand) if p >= min_posts]
            if len(s) < 4:
                continue
            recent, base = s[-3:], s[-17:-3]
            mentions = sum(m for _, m, _ in recent)
            if mentions < 5:
                continue
            rate = sum(m for _, m, _ in recent) / sum(p for _, _, p in recent) * 1e5
            base_rate = (sum(m for _, m, _ in base) / sum(p for _, _, p in base) * 1e5) if base else 0.0
            rows.append(
                {
                    "brand": brand,
                    "ticker": b.ticker,
                    "mentions": mentions,
                    "rate": round(rate, 2),
                    "ratio": round(rate / base_rate, 2) if base_rate else None,
                    "example": (self.examples.get(brand) or [""])[-1],
                }
            )
        rows.sort(key=lambda r: (-(r["ratio"] or 0), -r["mentions"]))
        return rows[:limit]

    def load(self, data: dict[str, Any]) -> None:
        self.counts = {d: dict(v) for d, v in (data.get("counts") or {}).items()}
        self.posts = {d: int(v) for d, v in (data.get("posts") or {}).items()}
        self.ncounts = {d: dict(v) for d, v in (data.get("ncounts") or {}).items()}
        self.last_sample = float(data.get("last_sample") or 0.0)

    def dump(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "posts": self.posts,
            "ncounts": self.ncounts,
            "last_sample": self.last_sample,
        }

    # ------------------------------------------------------------------ the firehose

    @staticmethod
    def post_text(evt: dict[str, Any]) -> str | None:
        """The text of a new post in a Jetstream event, else None (likes, deletes, profiles)."""
        if evt.get("kind") != "commit":
            return None
        c = evt.get("commit") or {}
        if c.get("operation") != "create" or c.get("collection") != "app.bsky.feed.post":
            return None
        rec = c.get("record") or {}
        langs = rec.get("langs") or []
        if langs and not any(str(lang).lower().startswith("en") for lang in langs):
            return None  # US brands: count English posts only
        return str(rec.get("text") or "")

    async def sample(self, session: Any, now: float | None = None) -> int:
        """Read the firehose for ``sample_s`` seconds; returns posts counted."""
        now = now or time.time()
        day = datetime.fromtimestamp(now, ET).date().isoformat()
        n = 0
        for i, host in enumerate(self.hosts):
            url = f"{host}?{urlencode([('wantedCollections', 'app.bsky.feed.post')])}"
            try:
                async with session.ws_connect(url, heartbeat=30, max_msg_size=4 * 1024 * 1024) as ws:
                    end = time.monotonic() + self.sample_s
                    while time.monotonic() < end:
                        try:
                            msg = await asyncio.wait_for(
                                ws.receive(), timeout=max(1.0, end - time.monotonic())
                            )
                        except asyncio.TimeoutError:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        try:
                            text = self.post_text(json.loads(msg.data))
                        except ValueError:
                            continue
                        if text is not None:
                            self.count(text, day)
                            n += 1
                break
            except Exception as exc:  # noqa: BLE001 - try the next host
                self.errors += 1
                log.info("camillo: jetstream %s failed: %s", host, exc)
                if i == len(self.hosts) - 1:
                    return n
        self.last_sample = now
        self.prune()
        return n


# --------------------------------------------------------------------------- information parity
#
# "I invest when there's an information imbalance, and I exit the investment when we reach
# information parity." (Camillo, Stansberry Investor Hour ep. 192, Feb 2021.) His book's test
# for parity, as summarized by readers of Laughing at Wall Street (2011): list the "hype"
# (bullish) quotes and the "gloom and doom" (bearish) quotes from journalists and analysts, one
# per source, and divide. Above 2 the story is known (parity: exit); below 2 there is still an
# imbalance; below 0.5 a strong one. Here the quotes are the headlines this system has read.

_HYPE = re.compile(
    r"\b(?:soar\w*|surg\w*|jump\w*|rall(?:y|ies|ied)|rocket\w*|record|boom\w*|craze|frenzy|viral|sold\s+out|"
    r"sells?\s+out|sellout|waitlist|hot(?:test)?|beats?|tops?|outperform\w*|upgrade\w*|winner|wins?|"
    r"strong(?:er)?|accelerat\w*|grow(?:s|ing|th)|rise[sn]?|rising|rose|gain(?:s|ed)?|climb\w*)\b",
    re.I,
)
_GLOOM = re.compile(
    r"\b(?:plung\w*|tumbl\w*|slump\w*|sink\w*|sank|crash\w*|fall(?:s|ing|en)?|fell|drop(?:s|ped)?|declin\w*|"
    r"slow(?:s|ed|ing|down)?|weak\w*|miss(?:es|ed)?|downgrade\w*|cut(?:s|ting)?|struggl\w*|loser|lose[s]?|"
    r"losing|lost|boycott\w*|backlash|closures?|bankrupt\w*|fad(?:e|es|ed|ing)?|lawsuit|recall\w*|warn\w*|"
    r"slid(?:e|es|ing)?|outbreak)\b",
    re.I,
)


def consensus_score(headlines: list[tuple[str, str]], direction: str | None = "up") -> dict[str, Any]:
    """``headlines``: (title, source) about the idea from the last 30 days. Hype and gloom are
    counted once per source each; the score is the trend's side over the other side (for a
    short idea, gloom over hype). None of either: no one is telling the story yet."""
    hype: set[str] = set()
    gloom: set[str] = set()
    for title, source in headlines:
        h, g = bool(_HYPE.search(title)), bool(_GLOOM.search(title))
        if h and not g:
            hype.add(source)
        elif g and not h:
            gloom.add(source)
    with_trend, against = (gloom, hype) if direction == "down" else (hype, gloom)
    score = len(with_trend) / max(len(against), 1)
    if len(with_trend) + len(against) == 0:
        level = "strong imbalance"
    elif score > 2:
        level = "parity"
    elif score < 0.5:
        level = "strong imbalance"
    else:
        level = "imbalance"
    return {
        "score": round(score, 2),
        "hype": len(hype),
        "gloom": len(gloom),
        "headlines": len(headlines),
        "level": level,
    }


def coverage_items(storage: Any, terms: list[str], since: float, limit: int = 400) -> list[dict[str, Any]]:
    """The stored headlines naming any of ``terms`` since ``since``, newest first:
    {"title", "source", "url", "detected"}."""
    terms = [t for t in dict.fromkeys(t.strip() for t in terms) if len(t) >= 3]
    if not terms or storage is None:
        return []
    where = " OR ".join("title LIKE ?" for _ in terms)
    rows = storage.db.execute(
        f"SELECT title, source, url, detected FROM items WHERE detected >= ? AND ({where}) "  # noqa: S608
        "ORDER BY detected DESC LIMIT ?",
        (since, *[f"%{t}%" for t in terms], limit),
    ).fetchall()
    rx = re.compile("|".join(rf"(?<![\w]){re.escape(t)}(?![\w])" for t in terms), re.I)
    return [
        {"title": r[0], "source": r[1], "url": r[2] or "", "detected": r[3]}
        for r in rows
        if rx.search(r[0] or "")
    ]


def coverage(storage: Any, terms: list[str], since: float, limit: int = 400) -> list[tuple[str, str]]:
    """(title, source) of the stored headlines naming any of ``terms`` since ``since``."""
    return [(i["title"], i["source"]) for i in coverage_items(storage, terms, since, limit)]


# --------------------------------------------------------------------------- the other side
#
# "I will always spend more time trying to understand the other side of my trades than I do my
# own thesis." And: "if I see something that goes against my thesis, I will immediately exit my
# trade." (Camillo, Iced Coffee Hour, Aug 2026.) His Sweetgreen example: he was trading a new
# wrap; a cyclospora outbreak in lettuce then became "the only Sweetgreen trade", and his thesis
# "became irrelevant". A shock like that is what ``thesis_check`` looks for.

_SHOCK = re.compile(
    r"\b(?:outbreak|e\.?\s?coli|salmonella|listeria|cyclospora|norovirus|food[- ]?borne|food poisoning|"
    r"recall(?:s|ed|ing)?|lawsuits?|sued|class[- ]action|investigation|probe[sd]?|subpoena\w*|boycott\w*|"
    r"data breach|hacked|bankrupt\w*|chapter 11|cease[- ]and[- ]desist|bans?|banned|banning|force majeure|"
    r"(?:ceo|chief executive|founder)\s+(?:resign\w*|steps?\s+down|stepping\s+down|depart\w*|exit\w*|ousted|fired|out)|"
    r"cuts?\s+(?:its\s+|full[- ]year\s+|annual\s+)?(?:guidance|outlook|forecast)|"
    r"(?:lowers|slashes|withdraws|pulls)\s+(?:its\s+)?(?:guidance|outlook|forecast)|"
    r"short[- ]seller|short report)\b",
    re.I,
)


def thesis_check(items: list[dict[str, Any]], direction: str | None) -> dict[str, Any]:
    """Has something new broken the thesis? ``items``: the brand's headlines since the evidence
    began. A shock (an outbreak, a recall, a lawsuit, a CEO exit, a guidance cut) breaks a long
    idea; for a short idea it is the thesis playing out, not breaking."""
    shocks = [i for i in items if _SHOCK.search(i.get("title") or "")]
    if not shocks:
        return {
            "pass": True,
            "shocks": [],
            "detail": "no outbreak, recall, lawsuit, exit or guidance cut in the headlines since",
        }
    first = shocks[0]
    if direction == "down":
        return {
            "pass": True,
            "shocks": shocks[:3],
            "detail": f"{len(shocks)} shock headline{'s' if len(shocks) > 1 else ''} since, which a short idea expects: "
            f"\u201c{first['title']}\u201d ({first['source']})",
        }
    return {
        "pass": False,
        "shocks": shocks[:3],
        "detail": f"\u201c{first['title']}\u201d ({first['source']}): check whether it makes the thesis "
        "irrelevant, and exit if it does",
    }


def other_side(items: list[dict[str, Any]], direction: str | None, k: int = 3) -> list[dict[str, Any]]:
    """The headlines arguing against the idea (gloom for a long, hype for a short), one per
    source, newest first: read these before trusting it."""
    want, other = (_HYPE, _GLOOM) if direction == "down" else (_GLOOM, _HYPE)
    out, seen = [], set()
    for i in items:
        t = i.get("title") or ""
        if want.search(t) and not other.search(t) and i.get("source") not in seen:
            seen.add(i.get("source"))
            out.append({k2: i.get(k2) for k2 in ("title", "source", "url", "detected")})
            if len(out) >= k:
                break
    return out


# --------------------------------------------------------------------------- the checklist


def checklist(
    sig: dict[str, Any],
    cfg: Any,
    earnings: dict[str, Any] | None,
    consensus: dict[str, Any] | None = None,
    thesis: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The method's tests for one idea, each pass/fail with the numbers behind it."""
    kinds = sig.get("kinds") or []
    direction = sig.get("direction")
    cap = sig.get("market_cap")
    weight = sig.get("weight", "auto")
    limit = {"core": cfg.core_max_cap, "major": cfg.major_max_cap}.get(weight, cfg.auto_max_cap)
    move = sig.get("move_pct")
    priced = False
    if move is not None:
        if direction == "up":
            priced = move >= cfg.priced_in_pct
        elif direction == "down":
            priced = move <= -cfg.priced_in_pct
        else:
            priced = abs(move) >= cfg.priced_in_pct
    from .trends import KIND_LABEL

    def cap_text(v: float | None) -> str:
        from .market.universe import fmt_cap

        return fmt_cap(v) if v else "size unknown"

    rows = [
        {
            "id": "change",
            "rule": "People are doing something new",
            "pass": bool(kinds),
            "detail": ", ".join(KIND_LABEL.get(k, k) for k in kinds) or "no evidence",
        },
        {
            "id": "confirmed",
            "rule": "Seen in two independent places",
            "pass": len(kinds) >= 2,
            "detail": f"{len(kinds)} kind{'s' if len(kinds) != 1 else ''} of evidence",
        },
        {
            "id": "direction",
            "rule": "It points one way",
            "pass": direction in ("up", "down"),
            "detail": {"up": "more customers", "down": "fewer customers", "mixed": "both ways"}.get(
                direction or "", "attention only: no direction yet"
            ),
        },
        {
            "id": "material",
            "rule": "Big enough to move this company",
            "pass": cap is not None and cap <= limit,
            "detail": f"{cap_text(cap)}, {weight} brand (limit {cap_text(limit)})",
        },
        {
            "id": "imbalance",
            "rule": "An information imbalance (the story isn't told yet)",
            "pass": consensus is None or consensus["level"] != "parity",
            "detail": (
                "no headline in 30 days is telling this story yet (strong imbalance)"
                if consensus is not None and consensus["hype"] + consensus["gloom"] == 0
                else f"consensus score {consensus['score']}: {consensus['hype']} hype vs {consensus['gloom']} gloom "
                f"sources in 30 days ({consensus['level']}; parity above 2)"
                if consensus is not None
                else "no headlines read"
            ),
        },
        {
            "id": "unpriced",
            "rule": "The price hasn't caught up",
            "pass": move is not None and not priced,
            "detail": (
                f"stock {move:+.1f}% since the evidence began (out at {cfg.priced_in_pct:.0f}% the trend's way)"
                if move is not None
                else "no price read"
            ),
        },
        {
            "id": "catalyst",
            "rule": "A report ahead will reveal it",
            "pass": earnings is not None and earnings["in_days"] <= 120,
            "detail": (
                f"next report {earnings['date']}"
                + (f" {earnings['when']}" if earnings.get("when") else "")
                + f" (in {earnings['in_days']} days)"
                if earnings
                else "next report date unknown"
            ),
        },
        {
            "id": "thesis",
            "rule": "Nothing new has broken the thesis",
            "pass": thesis is None or bool(thesis["pass"]),
            "detail": thesis["detail"] if thesis is not None else "no headlines read",
        },
    ]
    return rows


def conviction(checks: list[dict[str, Any]], severity: str) -> dict[str, Any]:
    """His sizing rule, shown as a label (the desk never sizes or places a trade). High
    conviction comes "a few times a year" after 50-100 hours of research; anything less he now
    trades at "two to 3% of what I would normally trade" (Iced Coffee Hour, Aug 2026)."""
    passed = sum(c["pass"] for c in checks)
    if severity == "HIGH" and passed == len(checks):
        return {
            "level": "high",
            "rule": "Every test passes. His high-conviction trades came a few times a year, after 50-100 hours "
            "of his own research and more time on the bear case than the bull case. The desk's evidence is "
            "where that research starts, not a substitute for it.",
        }
    return {
        "level": "low",
        "rule": f"{passed} of {len(checks)} tests pass: low conviction. His rule for these: trade 2-3% of a "
        "high-conviction size, or not at all.",
    }


def exit_plan(sig: dict[str, Any], earnings: dict[str, Any] | None) -> str:
    """Camillo exits at information parity, win or lose, with no price target; at once when the
    thesis breaks; and by halves when unsure ("buy half, sell half")."""
    when = f"the {earnings['date']} report" if earnings else "the next report"
    side = {"up": "long", "down": "short"}.get(sig.get("direction") or "", "")
    return (
        f"{('A ' + side + ' idea. ') if side else ''}Out at information parity, win or lose, with no price "
        f"target: when the consensus score passes 2 (the press and analysts are telling the story), when "
        f"{when} shows it, or when the evidence stops. Out at once if something breaks the thesis. "
        f"Unsure? Sell half."
    )


def page(
    desk: Any, playbook: dict[str, Any] | None = None, days: float = 45, now: float | None = None
) -> dict[str, Any]:
    """Everything the Camillo page shows."""
    now = now or time.time()
    earnings = getattr(desk, "earnings", None)
    chatter = getattr(desk, "chatter", None)
    watch = getattr(desk, "narratives", None)
    ideas = []
    since = now - 30 * 86400
    for r in desk.board(days, now):
        e = earnings.lookup(r["ticker"]) if earnings is not None else None
        terms = [t for b in desk.by_ticker.get(r["ticker"], []) for t in [b.brand, *b.terms]] or list(
            r["brands"]
        )
        items = coverage_items(desk.storage, terms, since)
        cons = consensus_score([(i["title"], i["source"]) for i in items], r.get("direction"))
        began = min(
            [float(x["started"]) for x in r.get("evidence") or [] if x.get("started")] or [r["created"]]
        )
        thesis = thesis_check([i for i in items if i["detected"] >= began], r.get("direction"))
        checks = checklist(r, desk.cfg, e, cons, thesis)
        ideas.append(
            {
                **r,
                "earnings": e,
                "consensus": cons,
                "checklist": checks,
                "passed": sum(c["pass"] for c in checks),
                "conviction": conviction(checks, r["severity"]),
                "other_side": other_side(items, r.get("direction")),
                "exit": exit_plan(r, e),
            }
        )
    ideas.sort(
        key=lambda r: (-{"HIGH": 2, "MEDIUM": 1, "LOW": 0}[r["severity"]], -r["passed"], -r["created"])
    )
    return {
        "playbook": load_playbook(playbook),
        "ideas": ideas,
        "narratives": watch.scan(desk.storage, chatter, now) if watch is not None else [],
        "attention": desk.heating_up(8),
        "chatter": chatter.heating_up(8) if chatter is not None else [],
        "scorecard": desk.scorecard(),
        "status": {
            **desk.status(),
            "chatter": {
                "enabled": chatter is not None,
                "last_sample": chatter.last_sample if chatter is not None else None,
                "days": len(chatter.posts) if chatter is not None else 0,
                "posts_today": chatter.posts.get(datetime.now(ET).date().isoformat(), 0) if chatter else 0,
                "errors": chatter.errors if chatter is not None else 0,
            },
            "earnings": {
                "enabled": earnings is not None,
                "companies": len(earnings.next) if earnings is not None else 0,
                "last_day": earnings.last_day if earnings is not None else None,
            },
            "narratives": {
                "enabled": watch is not None,
                "tracked": len(watch.narratives) if watch is not None else 0,
                "last_scan": watch.at or None if watch is not None else None,
            },
        },
    }
