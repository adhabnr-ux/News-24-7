"""Watch any web page for new links — for newsrooms that have no RSS feed (e.g. Anthropic).

Every poll extracts the links whose path matches ``link_pattern``; links never seen before
are new posts. The first poll only records the baseline, so nothing is replayed at start-up.
Titles come from a heading or a "title"-classed element inside the link, falling back to
its longest text run.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from ..models import NewsItem, SourceTier
from .base import PollingSource

_DATE_RE = re.compile(
    r"^(?:\w{3,9}\.? \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2}|\d{1,2} \w{3,9} \d{4}|\d{1,2}/\d{1,2}/\d{2,4})$"
)
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []  # open <a> elements
        self._title_depth: list[str] = []  # open title-ish elements inside current <a>
        self._time_attr: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._stack.append({"href": a["href"], "chunks": [], "titles": [], "time": None})
            return
        if not self._stack:
            return
        cls = (a.get("class") or "").lower()
        if tag in _HEADING_TAGS or "title" in cls or "headline" in cls:
            self._title_depth.append(tag)
        if tag == "time":
            self._stack[-1]["time"] = a.get("datetime")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._stack:
            link = self._stack.pop()
            self._title_depth.clear()
            self.links.append(link)
            return
        if self._title_depth and self._title_depth[-1] == tag:
            self._title_depth.pop()

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        text = " ".join(data.split())
        if not text:
            return
        cur = self._stack[-1]
        cur["chunks"].append(text)
        if self._title_depth:
            cur["titles"].append(text)
        if cur["time"] is None and _DATE_RE.match(text):
            cur["time"] = text


def extract_links(html: str, base_url: str, pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    """Return [{url, title, time}] for links matching ``pattern`` (first occurrence wins)."""
    parser = _LinkExtractor()
    parser.feed(html)
    parser.close()
    out: dict[str, dict[str, Any]] = {}
    for link in parser.links:
        url = urljoin(base_url, link["href"]).split("#")[0]
        if not pattern.search(url):
            continue
        title = " ".join(link["titles"]).strip()
        if not title:
            candidates = [c for c in link["chunks"] if not _DATE_RE.match(c)]
            title = max(candidates, key=len, default="")
        title = title.strip()
        if url in out:
            if not out[url]["title"] and title:
                out[url]["title"] = title
            continue
        out[url] = {"url": url, "title": title, "time": link["time"]}
    # Raw-text fallback (sitemaps, JSON blobs): any absolute URL in the document that matches.
    if not out:
        for m in re.finditer(r"https?://[^\s\"'<>]+", html):
            url = m.group(0).rstrip(".,);")
            if pattern.search(url) and url not in out:
                out[url] = {"url": url, "title": "", "time": None}
    return list(out.values())


def _title_from_url(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").replace("_", " ").strip().capitalize()


class PageWatchSource(PollingSource):
    """Options: ``url`` (page to watch), ``link_pattern`` (regex on absolute link URLs)."""

    type_name = "pagewatch"
    default_interval = 30.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        cfg = {"browser_headers": True, **cfg}
        super().__init__(cfg, ctx)
        if not cfg.get("url") or not cfg.get("link_pattern"):
            raise ValueError(f"source '{self.name}': pagewatch needs url and link_pattern")
        self.url: str = cfg["url"]
        self.pattern = re.compile(cfg["link_pattern"])

    async def fetch(self) -> list[NewsItem]:
        resp = await self.ctx.http.get(self.url, headers=self.headers, conditional=True)
        if resp.not_modified:
            return []
        items = self.parse(resp.text())
        if not items:
            # a listing page always shows recent posts: zero matches means the site changed or
            # link_pattern is wrong, so say so on the dashboard instead of silently finding nothing
            raise ValueError(f"no links on {self.url} match link_pattern {self.pattern.pattern!r}")
        return items

    def parse(self, html: str) -> list[NewsItem]:
        # A listing page only shows a date, never a precise time, so new posts are emitted
        # with an unknown publish time (detection time is what matters here).
        items = []
        for link in extract_links(html, self.url, self.pattern):
            item = self.make_item(title=link["title"] or _title_from_url(link["url"]), url=link["url"])
            if link["time"]:
                item.extra["listed_date"] = link["time"]
            items.append(item)
        return items
