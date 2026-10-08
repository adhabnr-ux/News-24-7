"""X (Twitter) filtered stream — posts are pushed to us within a few seconds of being sent.

Uses the official API v2 filtered stream, available on X's pay-per-use plan (billed per post
delivered, ~$0.005 each in 2026). Rules are built from ``accounts`` and kept in sync on every
(re)connect: stale News247 rules are deleted, current ones added.

Watch two kinds of accounts:
  * first-party: @OpenAI, @sama, @AnthropicAI … (put them in ``vip_accounts`` so every post is
    texted to you instantly, whatever its score)
  * headline squawks: @DeItaone, @FirstSquawk, @financialjuice … — they relay newspaper scoops
    (FT, WSJ, Bloomberg, The Information) within seconds; their posts are scored normally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any

import aiohttp

from ..models import NewsItem, SourceTier
from .base import Emit, Source, _sleep_or_stop
from .social import parse_tweets

log = logging.getLogger(__name__)

API = "https://api.x.com/2"
RULE_TAG = "news247"
MAX_RULE = 512


def build_rules(accounts: list[str], include_replies: bool = False, max_len: int = MAX_RULE) -> list[str]:
    suffix = " -is:retweet" + ("" if include_replies else " -is:reply")
    rules: list[str] = []
    cur: list[str] = []
    for acct in accounts:
        term = f"from:{acct.lstrip('@')}"
        if cur and len(f"({' OR '.join([*cur, term])}){suffix}") > max_len:
            rules.append(f"({' OR '.join(cur)}){suffix}")
            cur = []
        cur.append(term)
    if cur:
        rules.append(f"({' OR '.join(cur)}){suffix}")
    return rules


class XStreamSource(Source):
    type_name = "x_stream"
    default_tier = SourceTier.MEDIA

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        vip = [str(a).lstrip("@") for a in cfg.get("vip_accounts", [])]
        cfg = {**cfg, "vip_authors": [*cfg.get("vip_authors", []), *vip]}
        super().__init__(cfg, ctx)
        self.token = cfg.get("bearer_token", "")
        if not self.token:
            raise ValueError(f"source '{self.name}': x_stream needs bearer_token (developer.x.com)")
        self.accounts = list(dict.fromkeys([*vip, *(str(a).lstrip("@") for a in cfg.get("accounts", []))]))
        if not self.accounts:
            raise ValueError(f"source '{self.name}': x_stream needs accounts and/or vip_accounts")
        self.rules = build_rules(
            self.accounts, bool(cfg.get("include_replies", False)), int(cfg.get("max_rule_length", MAX_RULE))
        )
        self.api = str(cfg.get("api_base", API)).rstrip("/")
        # posts from first-party accounts are primary sources; squawks are media-tier relays
        self._vip_lower = {a.lower() for a in vip}

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def sync_rules(self) -> None:
        resp = await self.ctx.http.get(f"{self.api}/tweets/search/stream/rules", headers=self.auth)
        existing = resp.json().get("data") or []
        wanted = set(self.rules)
        stale = [
            r["id"]
            for r in existing
            if r.get("tag", "").startswith(RULE_TAG) and r.get("value") not in wanted
        ]
        have = {r.get("value") for r in existing}
        if stale:
            await self.ctx.http.post(
                f"{self.api}/tweets/search/stream/rules", json={"delete": {"ids": stale}}, headers=self.auth
            )
        add = [{"value": v, "tag": f"{RULE_TAG}-{i}"} for i, v in enumerate(self.rules) if v not in have]
        if add:
            await self.ctx.http.post(
                f"{self.api}/tweets/search/stream/rules", json={"add": add}, headers=self.auth
            )

    def handle_line(self, line: bytes) -> list[NewsItem]:
        line = line.strip()
        if not line:  # keep-alive heartbeat
            return []
        msg = json.loads(line)
        if "errors" in msg and "data" not in msg:
            raise ConnectionError(f"stream error: {msg['errors']}")
        items = parse_tweets(self, msg)
        for it in items:
            if it.author.lower() in self._vip_lower:
                it.tier = SourceTier.PRIMARY  # the company/person itself
            else:
                it.extra["relay"] = True  # a squawk re-posting news: fast but unverified
        return items

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        attempt = 0
        params = {
            "tweet.fields": "created_at,author_id,entities",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        while not stop.is_set():
            try:
                await self.sync_rules()
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90)
                async with self.ctx.http.session.get(
                    f"{self.api}/tweets/search/stream", params=params, headers=self.auth, timeout=timeout
                ) as resp:
                    if resp.status == 429:
                        raise ConnectionRefusedError(
                            "429: too many connections/requests (another stream open?)"
                        )
                    if resp.status >= 400:
                        raise ConnectionError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
                    self.health.connected = True
                    self._note_ok()
                    attempt = 0
                    log.info(
                        "[%s] X stream connected (%d accounts, %d rules)",
                        self.name,
                        len(self.accounts),
                        len(self.rules),
                    )
                    while not stop.is_set():
                        line = await resp.content.readline()
                        if not line:
                            raise ConnectionError("stream ended")
                        self.health.last_ok = time.time()
                        try:
                            items = self.handle_line(line)
                        except ValueError:
                            continue
                        self.health.polls += 1
                        for it in items:
                            await self._deliver(it, emit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.connected = False
                self._note_error(exc)
                attempt += 1
                # X asks clients to back off exponentially; 429 needs at least a minute
                base = 60.0 if isinstance(exc, ConnectionRefusedError) else 2.0
                delay = min(600.0, base * 2 ** min(attempt - 1, 6)) * random.uniform(0.8, 1.2)
                log.warning("[%s] X stream error (%s); reconnecting in %.0fs", self.name, exc, delay)
                await _sleep_or_stop(stop, delay)
        self.health.connected = False

    async def check(self) -> list[NewsItem]:
        resp = await self.ctx.http.get(f"{self.api}/tweets/search/stream/rules", headers=self.auth)
        rules = resp.json().get("data") or []
        mine = [r for r in rules if re.match(RULE_TAG, r.get("tag", ""))]
        return [self.make_item(title=f"stream rule: {r['value'][:80]}", uid=f"rule:{r['id']}") for r in mine]
