"""Posts from specific accounts: X/Twitter (API v2) and Mastodon-compatible servers
(including Truth Social)."""

from __future__ import annotations

import logging
from typing import Any

from ..models import NewsItem, SourceTier
from ..util import parse_datetime, strip_html
from .base import PollingSource

log = logging.getLogger(__name__)


class XSource(PollingSource):
    """X (Twitter) accounts via the v2 recent-search endpoint.

    One request covers many accounts (``from:a OR from:b …``), which fits the Basic tier's
    rate limit while polling every ~15-20 s. Needs ``bearer_token`` (developer.x.com).
    Options: ``accounts`` (usernames), ``include_replies`` (default false).
    """

    type_name = "x"
    default_interval = 20.0
    default_tier = SourceTier.PRIMARY
    URL = "https://api.x.com/2/tweets/search/recent"
    MAX_QUERY = 512

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.token = cfg.get("bearer_token", "")
        if not self.token:
            raise ValueError(f"source '{self.name}': x needs bearer_token")
        self.accounts: list[str] = [a.lstrip("@") for a in cfg.get("accounts", [])]
        if not self.accounts:
            raise ValueError(f"source '{self.name}': x needs accounts")
        self.include_replies = bool(cfg.get("include_replies", False))
        self.queries = self.build_queries()
        self._since: dict[str, str] = {}

    def build_queries(self) -> list[str]:
        suffix = " -is:retweet" + ("" if self.include_replies else " -is:reply")
        queries, cur = [], []
        for acct in self.accounts:
            trial = cur + [f"from:{acct}"]
            if len(f"({' OR '.join(trial)}){suffix}") > self.MAX_QUERY and cur:
                queries.append(f"({' OR '.join(cur)}){suffix}")
                cur = [f"from:{acct}"]
            else:
                cur = trial
        if cur:
            queries.append(f"({' OR '.join(cur)}){suffix}")
        return queries

    async def fetch(self) -> list[NewsItem]:
        out: list[NewsItem] = []
        for q in self.queries:
            params: dict[str, Any] = {
                "query": q,
                "max_results": 25,
                "tweet.fields": "created_at,author_id,entities",
                "expansions": "author_id",
                "user.fields": "username,name",
            }
            if q in self._since:
                params["since_id"] = self._since[q]
            resp = await self.ctx.http.get(
                self.URL, params=params, headers={"Authorization": f"Bearer {self.token}"}
            )
            data = resp.json()
            newest = data.get("meta", {}).get("newest_id")
            if newest:
                self._since[q] = newest
            out.extend(self.parse(data))
        return out

    def parse(self, data: dict[str, Any]) -> list[NewsItem]:
        return parse_tweets(self, data)


def parse_tweets(src: Any, data: dict[str, Any]) -> list[NewsItem]:
    """X API v2 tweet payload (search or stream) -> NewsItems. ``author`` is the @handle so
    ``vip_authors`` can match it."""
    tweets = data.get("data") or []
    if isinstance(tweets, dict):  # the filtered stream sends one tweet per message
        tweets = [tweets]
    users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
    out = []
    for tw in tweets:
        user = users.get(tw.get("author_id"), {})
        handle = user.get("username", "unknown")
        text = tw.get("text", "")
        cashtags = [c["tag"].upper() for c in (tw.get("entities") or {}).get("cashtags", []) if c.get("tag")]
        item = src.make_item(
            title=f"@{handle}: {text}",
            url=f"https://x.com/{handle}/status/{tw['id']}",
            published=parse_datetime(tw.get("created_at")),
            author=handle,
            uid=f"x:{tw['id']}",
            tickers=cashtags,
        )
        item.extra["display_name"] = user.get("name", handle)
        out.append(item)
    return out


class MastodonSource(PollingSource):
    """Statuses from accounts on a Mastodon-API server (Mastodon, Truth Social, …).

    Options: ``instance`` (base URL), ``accounts`` (list of account IDs or handles).
    Truth Social sits behind Cloudflare and may refuse datacenter IPs; run from home or
    pair with a mirror feed.
    """

    type_name = "mastodon"
    default_interval = 30.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        cfg = {"browser_headers": True, **cfg}
        super().__init__(cfg, ctx)
        self.instance = str(cfg.get("instance", "")).rstrip("/")
        self.accounts: list[str] = [str(a) for a in cfg.get("accounts", [])]
        if not self.instance or not self.accounts:
            raise ValueError(f"source '{self.name}': mastodon needs instance and accounts")
        self._ids: dict[str, str] = {}
        self._since: dict[str, str] = {}

    async def _resolve(self, acct: str) -> str:
        if acct.isdigit():
            return acct
        if acct not in self._ids:
            resp = await self.ctx.http.get(
                f"{self.instance}/api/v1/accounts/lookup",
                params={"acct": acct.lstrip("@")},
                headers=self.headers,
            )
            self._ids[acct] = str(resp.json()["id"])
        return self._ids[acct]

    async def fetch(self) -> list[NewsItem]:
        out: list[NewsItem] = []
        for acct in self.accounts:
            acct_id = await self._resolve(acct)
            params: dict[str, Any] = {"limit": 20, "exclude_replies": "true"}
            if acct_id in self._since:
                params["since_id"] = self._since[acct_id]
            resp = await self.ctx.http.get(
                f"{self.instance}/api/v1/accounts/{acct_id}/statuses", params=params, headers=self.headers
            )
            statuses = resp.json()
            if statuses:
                self._since[acct_id] = str(max(statuses, key=lambda s: int(s["id"]))["id"])
            out.extend(self.parse(statuses))
        return out

    def parse(self, statuses: list[dict[str, Any]]) -> list[NewsItem]:
        out = []
        for st in statuses:
            if st.get("reblog"):
                st = st["reblog"]
            text = strip_html(st.get("content", ""), 1000)
            card = st.get("card") or {}
            if not text and card.get("title"):
                text = f"[link] {card['title']}"
            if not text and st.get("media_attachments"):
                text = "[media post]"
            if not text:
                continue
            acct = st.get("account", {})
            handle = acct.get("acct") or acct.get("username") or "unknown"
            out.append(
                self.make_item(
                    title=f"@{handle}: {text}",
                    url=st.get("url") or st.get("uri", ""),
                    published=parse_datetime(st.get("created_at")),
                    author=acct.get("display_name", handle),
                    uid=f"{self.name}:{st.get('id')}",
                )
            )
        return out
