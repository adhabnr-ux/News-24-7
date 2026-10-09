"""RSS / Atom feeds (company newsrooms, wires, regulators, media)."""

from __future__ import annotations

from typing import Any

import feedparser

from ..models import NewsItem, SourceTier
from ..util import strip_html, struct_to_ts
from .base import PollingSource


def parse_feed(body: bytes) -> Any:
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        exc = parsed.get("bozo_exception")
        raise ValueError(f"unparseable feed: {exc}")
    return parsed


class RSSSource(PollingSource):
    """Generic RSS/Atom poller with conditional GET.

    Options: ``url`` (required), ``interval``, ``tier``, ``entities``, ``tickers``,
    ``include``/``exclude`` regexes, ``browser_headers``, ``headers``.
    """

    type_name = "rss"
    default_interval = 30.0
    default_tier = SourceTier.MEDIA

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        if not cfg.get("url"):
            raise ValueError(f"source '{self.name}': rss needs a url")
        self.url: str = cfg["url"]
        self.max_entries = int(cfg.get("max_entries", 100))
        # Some feeds (e.g. scraped mirrors) only carry a date, stamped 00:00 UTC. Treat those as
        # "time unknown" so a fresh post isn't mistaken for a 20-hour-old one and dropped.
        self.date_only = bool(cfg.get("date_only", False))

    async def fetch(self) -> list[NewsItem]:
        resp = await self.ctx.http.get(self.url, headers=self.headers, conditional=True)
        if resp.not_modified:
            return []
        parsed = parse_feed(resp.body)
        return [i for i in (self.entry_to_item(e) for e in parsed.entries[: self.max_entries]) if i]

    def entry_to_item(self, e: Any) -> NewsItem | None:
        title = strip_html(e.get("title"), None)
        if not title:
            return None
        published = struct_to_ts(e.get("published_parsed")) or struct_to_ts(e.get("updated_parsed"))
        listed_date = None
        if self.date_only and published is not None and published % 86400 == 0:
            listed_date, published = published, None
        summary = strip_html(e.get("summary") or e.get("description"))
        link = e.get("link") or ""
        uid = e.get("id") or e.get("guid") or ""
        tickers: list[str] = []
        for tag in e.get("tags") or []:
            term = str(tag.get("term") or "")
            # GlobeNewswire/PRN put "NASDAQ:ABCD" / "NYSE:XYZ" in categories
            if ":" in term:
                exch, _, sym = term.partition(":")
                if exch.strip().upper().replace(" ", "") in {
                    "NASDAQ",
                    "NYSE",
                    "NYSEAMERICAN",
                    "AMEX",
                    "OTC",
                    "TSX",
                    "CBOE",
                }:
                    sym = sym.strip().upper()
                    if sym.replace(".", "").replace("-", "").isalnum() and len(sym) <= 6:
                        tickers.append(sym)
        item = self.make_item(
            title=title,
            url=link,
            summary=summary,
            published=published,
            author=strip_html(e.get("author"), 120),
            uid=f"{self.name}:{uid}" if uid and not link else "",
            tickers=tickers,
        )
        if listed_date is not None:
            item.extra["listed_date"] = listed_date
        return item
