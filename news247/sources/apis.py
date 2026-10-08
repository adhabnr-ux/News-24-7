"""JSON API pollers: Hacker News, Reddit (OAuth), Finnhub market news."""

from __future__ import annotations

import base64
import time
from typing import Any

from ..http import HTTPError
from ..models import NewsItem, SourceTier
from ..util import strip_html
from .base import PollingSource


class HackerNewsSource(PollingSource):
    """Hacker News via Algolia. Tech launches (OpenAI, Anthropic, Google…) usually reach the
    front page within minutes. Options: ``tags`` (default "front_page"), ``min_points``,
    ``query`` (optional search string)."""

    type_name = "hackernews"
    default_interval = 30.0
    default_tier = SourceTier.SOCIAL
    URL = "https://hn.algolia.com/api/v1/search_by_date"

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.tags = cfg.get("tags", "front_page")
        self.min_points = int(cfg.get("min_points", 0))
        self.query = cfg.get("query", "")
        self.hits = int(cfg.get("hits", 50))

    async def fetch(self) -> list[NewsItem]:
        params: dict[str, Any] = {"tags": self.tags, "hitsPerPage": self.hits}
        if self.query:
            params["query"] = self.query
        if self.min_points:
            params["numericFilters"] = f"points>={self.min_points}"
        resp = await self.ctx.http.get(self.URL, params=params, headers=self.headers)
        return self.parse(resp.json())

    def parse(self, data: dict[str, Any]) -> list[NewsItem]:
        out = []
        for hit in data.get("hits", []):
            title = hit.get("title") or hit.get("story_title")
            if not title:
                continue
            oid = hit.get("objectID")
            discussion = f"https://news.ycombinator.com/item?id={oid}"
            item = self.make_item(
                title=title,
                url=hit.get("url") or discussion,
                published=float(hit["created_at_i"]) if hit.get("created_at_i") else None,
                author=hit.get("author", ""),
            )
            item.extra.update(
                {"points": hit.get("points"), "comments": hit.get("num_comments"), "discussion": discussion}
            )
            out.append(item)
        return out


class RedditSource(PollingSource):
    """Subreddit "new" listings. Reddit closed unauthenticated JSON in 2026, so this needs a
    (free) script app: ``client_id``, ``client_secret`` and ideally ``username``/``password``.
    Options: ``subreddits`` (list), ``min_score``."""

    type_name = "reddit"
    default_interval = 45.0
    default_tier = SourceTier.SOCIAL

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.subreddits: list[str] = list(cfg.get("subreddits", ["wallstreetbets", "stocks"]))
        self.client_id = cfg.get("client_id", "")
        self.client_secret = cfg.get("client_secret", "")
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self.min_score = int(cfg.get("min_score", 0))
        self._token: str | None = None
        self._token_exp = 0.0
        if not (self.client_id and self.client_secret):
            raise ValueError(
                f"source '{self.name}': reddit needs client_id and client_secret (reddit.com/prefs/apps)"
            )

    async def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        data = (
            {"grant_type": "password", "username": self.username, "password": self.password}
            if self.username
            else {"grant_type": "client_credentials"}
        )
        resp = await self.ctx.http.post(
            "https://www.reddit.com/api/v1/access_token",
            data=data,
            headers={"Authorization": f"Basic {basic}", "User-Agent": self.ctx.user_agent},
        )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_exp = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    async def fetch(self) -> list[NewsItem]:
        token = await self._auth()
        multi = "+".join(self.subreddits)
        try:
            resp = await self.ctx.http.get(
                f"https://oauth.reddit.com/r/{multi}/new",
                params={"limit": 50, "raw_json": 1},
                headers={"Authorization": f"Bearer {token}", "User-Agent": self.ctx.user_agent},
            )
        except HTTPError as exc:
            if exc.status == 401:
                self._token = None
            raise
        return self.parse(resp.json())

    def parse(self, data: dict[str, Any]) -> list[NewsItem]:
        out = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            if d.get("score", 0) < self.min_score or d.get("stickied"):
                continue
            permalink = "https://www.reddit.com" + d.get("permalink", "")
            item = self.make_item(
                title=d.get("title", ""),
                url=permalink,
                summary=strip_html(d.get("selftext", ""), 400),
                published=float(d["created_utc"]) if d.get("created_utc") else None,
                author=f"r/{d.get('subreddit', '')} u/{d.get('author', '')}",
            )
            item.extra.update({"score": d.get("score"), "link": d.get("url")})
            out.append(item)
        return out


class FinnhubNewsSource(PollingSource):
    """Finnhub market news (free API key). Options: ``token``, ``category``
    (general|merger|forex|crypto)."""

    type_name = "finnhub_news"
    default_interval = 30.0
    default_tier = SourceTier.MEDIA

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.token = cfg.get("token", "")
        if not self.token:
            raise ValueError(f"source '{self.name}': finnhub_news needs a token (free at finnhub.io)")
        self.category = cfg.get("category", "general")
        self._min_id = 0

    async def fetch(self) -> list[NewsItem]:
        params: dict[str, Any] = {"category": self.category, "token": self.token}
        if self._min_id:
            params["minId"] = self._min_id
        resp = await self.ctx.http.get("https://finnhub.io/api/v1/news", params=params)
        return self.parse(resp.json())

    def parse(self, data: list[dict[str, Any]]) -> list[NewsItem]:
        out = []
        for d in data or []:
            self._min_id = max(self._min_id, int(d.get("id", 0)))
            related = [t.strip().upper() for t in str(d.get("related", "")).split(",") if t.strip()]
            item = self.make_item(
                title=d.get("headline", ""),
                url=d.get("url", ""),
                summary=strip_html(d.get("summary", "")),
                published=float(d["datetime"]) if d.get("datetime") else None,
                author=d.get("source", ""),
                tickers=related,
            )
            out.append(item)
        return out
