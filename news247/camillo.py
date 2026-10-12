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
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
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
        self, brands: list[Any], sample_s: float = 600.0, hosts: tuple[str, ...] = JETSTREAM_HOSTS
    ) -> None:
        self.brands = {b.brand: b for b in brands}
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

    def prune(self, keep_days: int = 30) -> None:
        cutoff = (datetime.now(ET).date() - timedelta(days=keep_days)).isoformat()
        for d in [d for d in self.posts if d < cutoff]:
            self.posts.pop(d, None)
            self.counts.pop(d, None)

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
        self.last_sample = float(data.get("last_sample") or 0.0)

    def dump(self) -> dict[str, Any]:
        return {"counts": self.counts, "posts": self.posts, "last_sample": self.last_sample}

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
    r"losing|lost|boycott\w*|backlash|closures?|bankrupt\w*|fad(?:e|es|ed|ing)?|lawsuit|recall\w*|warn\w*)\b",
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


def coverage(storage: Any, terms: list[str], since: float, limit: int = 400) -> list[tuple[str, str]]:
    """(title, source) of the stored headlines naming any of ``terms`` since ``since``."""
    terms = [t for t in dict.fromkeys(t.strip() for t in terms) if len(t) >= 3]
    if not terms or storage is None:
        return []
    where = " OR ".join("title LIKE ?" for _ in terms)
    rows = storage.db.execute(
        f"SELECT title, source FROM items WHERE detected >= ? AND ({where}) ORDER BY detected DESC LIMIT ?",  # noqa: S608
        (since, *[f"%{t}%" for t in terms], limit),
    ).fetchall()
    rx = re.compile("|".join(rf"(?<![\w]){re.escape(t)}(?![\w])" for t in terms), re.I)
    return [(r[0], r[1]) for r in rows if rx.search(r[0] or "")]


# --------------------------------------------------------------------------- the checklist


def checklist(
    sig: dict[str, Any],
    cfg: Any,
    earnings: dict[str, Any] | None,
    consensus: dict[str, Any] | None = None,
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
    ]
    return rows


def exit_plan(sig: dict[str, Any], earnings: dict[str, Any] | None) -> str:
    """Camillo exits at information parity, win or lose, with no price target."""
    when = f"the {earnings['date']} report" if earnings else "the next report"
    side = {"up": "long", "down": "short"}.get(sig.get("direction") or "", "")
    return (
        f"{('A ' + side + ' idea. ') if side else ''}Out at information parity, win or lose, with no price "
        f"target: when the consensus score passes 2 (the press and analysts are telling the story), when "
        f"{when} shows it, or when the evidence stops."
    )


def page(desk: Any, playbook: dict[str, Any] | None = None, days: float = 45) -> dict[str, Any]:
    """Everything the Camillo page shows."""
    earnings = getattr(desk, "earnings", None)
    chatter = getattr(desk, "chatter", None)
    ideas = []
    since = time.time() - 30 * 86400
    for r in desk.board(days):
        e = earnings.lookup(r["ticker"]) if earnings is not None else None
        terms = [t for b in desk.by_ticker.get(r["ticker"], []) for t in [b.brand, *b.terms]] or list(
            r["brands"]
        )
        cons = consensus_score(coverage(desk.storage, terms, since), r.get("direction"))
        checks = checklist(r, desk.cfg, e, cons)
        ideas.append(
            {
                **r,
                "earnings": e,
                "consensus": cons,
                "checklist": checks,
                "passed": sum(c["pass"] for c in checks),
                "exit": exit_plan(r, e),
            }
        )
    ideas.sort(
        key=lambda r: (-{"HIGH": 2, "MEDIUM": 1, "LOW": 0}[r["severity"]], -r["passed"], -r["created"])
    )
    return {
        "playbook": load_playbook(playbook),
        "ideas": ideas,
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
        },
    }
