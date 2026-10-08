"""Bluesky in real time via Jetstream (free, no account needed, push — not polling).

Follows specific accounts (``accounts``: handles or DIDs) and/or watches the whole network
for ``keywords``. A post shows up here within about a second of being published.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..models import NewsItem, SourceTier
from ..util import parse_datetime
from .base import Emit, Source, _sleep_or_stop

log = logging.getLogger(__name__)

JETSTREAM_HOSTS = [
    "wss://jetstream2.us-east.bsky.network/subscribe",
    "wss://jetstream1.us-east.bsky.network/subscribe",
    "wss://jetstream1.us-west.bsky.network/subscribe",
    "wss://jetstream2.us-west.bsky.network/subscribe",
]
RESOLVE_URL = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle"
AUTHOR_FEED_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"


class BlueskySource(Source):
    type_name = "bluesky"
    default_tier = SourceTier.SOCIAL

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.accounts: list[str] = list(cfg.get("accounts", []))
        kws = [k for k in cfg.get("keywords", []) if k]
        self.keyword_re = re.compile("|".join(rf"\b{re.escape(k)}\b" for k in kws), re.I) if kws else None
        if not self.accounts and not self.keyword_re:
            raise ValueError(f"source '{self.name}': bluesky needs accounts and/or keywords")
        self.include_replies = bool(cfg.get("include_replies", False))
        self.hosts: list[str] = list(cfg.get("hosts", JETSTREAM_HOSTS))
        self.dids: dict[str, str] = {}  # did -> handle
        self._cursor: int | None = None

    async def resolve_accounts(self) -> None:
        for acct in self.accounts:
            if acct in self.dids.values() or acct in self.dids:
                continue
            if acct.startswith("did:"):
                self.dids[acct] = acct
                continue
            try:
                resp = await self.ctx.http.get(RESOLVE_URL, params={"handle": acct.lstrip("@")})
                self.dids[resp.json()["did"]] = acct.lstrip("@")
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] cannot resolve Bluesky handle %s: %s", self.name, acct, exc)

    def stream_url(self, host: str) -> str:
        params = [("wantedCollections", "app.bsky.feed.post")]
        # with keywords we must see every post; otherwise only the followed accounts
        if not self.keyword_re:
            params += [("wantedDids", d) for d in self.dids]
        if self._cursor:
            params.append(("cursor", str(self._cursor - 5_000_000)))  # rewind 5 s on reconnect
        return f"{host}?{urlencode(params)}"

    def handle_event(self, evt: dict[str, Any]) -> NewsItem | None:
        if evt.get("kind") != "commit":
            return None
        self._cursor = evt.get("time_us") or self._cursor
        commit = evt.get("commit") or {}
        if commit.get("operation") != "create" or commit.get("collection") != "app.bsky.feed.post":
            return None
        rec = commit.get("record") or {}
        if rec.get("reply") and not self.include_replies:
            return None
        text = (rec.get("text") or "").strip()
        did = evt.get("did", "")
        followed = did in self.dids
        if not followed and not (self.keyword_re and self.keyword_re.search(text)):
            return None
        if not text:
            embed = rec.get("embed") or {}
            ext = embed.get("external") or {}
            text = ext.get("title") or "[media post]"
        handle = self.dids.get(did, did)
        rkey = commit.get("rkey", "")
        item = self.make_item(
            title=f"@{handle}: {text}",
            url=f"https://bsky.app/profile/{handle}/post/{rkey}",
            published=parse_datetime(rec.get("createdAt")),
            author=handle,
            uid=f"bsky:{did}:{rkey}",
        )
        if not followed:
            item.tier = SourceTier.SOCIAL
        return item

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            await self.resolve_accounts()
            if self.accounts and not self.dids and not self.keyword_re:
                self._note_error(RuntimeError("no Bluesky handles could be resolved"))
                await _sleep_or_stop(stop, 300)
                continue
            host = self.hosts[attempt % len(self.hosts)]
            try:
                async with self.ctx.http.session.ws_connect(
                    self.stream_url(host), heartbeat=30, receive_timeout=120, max_msg_size=4 * 1024 * 1024
                ) as ws:
                    self.health.connected = True
                    self._note_ok()
                    log.info("[%s] connected to %s (%d accounts)", self.name, host, len(self.dids))
                    attempt = 0
                    stop_task = asyncio.ensure_future(stop.wait())
                    try:
                        while not stop.is_set():
                            recv = asyncio.ensure_future(ws.receive())
                            done, _ = await asyncio.wait(
                                {recv, stop_task}, return_when=asyncio.FIRST_COMPLETED
                            )
                            if stop_task in done:
                                recv.cancel()
                                break
                            msg = recv.result()
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.health.polls += 1
                                self.health.last_ok = time.time()
                                try:
                                    item = self.handle_event(json.loads(msg.data))
                                except (ValueError, KeyError, TypeError):
                                    continue
                                if item:
                                    await self._deliver(item, emit)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE,
                            ):
                                raise ConnectionError(f"websocket closed: {msg.extra or msg.type}")
                    finally:
                        stop_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.connected = False
                self._note_error(exc)
                attempt += 1
                delay = min(120.0, 2 ** min(attempt, 7)) * random.uniform(0.8, 1.2)
                log.warning("[%s] stream error (%s); reconnecting in %.0fs", self.name, exc, delay)
                await _sleep_or_stop(stop, delay)
        self.health.connected = False

    async def check(self) -> list[NewsItem]:
        await self.resolve_accounts()
        if not self.dids:
            raise RuntimeError(
                "no Bluesky handles resolved" if self.accounts else "keyword-only stream (no check)"
            )
        out: list[NewsItem] = []
        for did, handle in list(self.dids.items())[:5]:
            resp = await self.ctx.http.get(AUTHOR_FEED_URL, params={"actor": did, "limit": 3})
            for entry in resp.json().get("feed", []):
                post = entry.get("post", {})
                rec = post.get("record", {})
                rkey = post.get("uri", "").rsplit("/", 1)[-1]
                out.append(
                    self.make_item(
                        title=f"@{handle}: {rec.get('text', '')}",
                        url=f"https://bsky.app/profile/{handle}/post/{rkey}",
                        published=parse_datetime(rec.get("createdAt")),
                    )
                )
        return out
