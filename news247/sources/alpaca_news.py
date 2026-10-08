"""Alpaca real-time news stream (Benzinga newsdesk) — pushed over a websocket.

Free Alpaca account (paper trading is fine, no money needed): create API keys at
https://app.alpaca.markets and set ``key`` / ``secret``. Benzinga's desk writes up wire
stories and newspaper scoops within seconds to a couple of minutes, with tickers attached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import aiohttp

from ..http import ws_receive_timeout
from ..models import NewsItem, SourceTier
from ..util import parse_datetime, strip_html
from .base import Emit, Source, _sleep_or_stop

log = logging.getLogger(__name__)

STREAM_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
REST_URL = "https://data.alpaca.markets/v1beta1/news"


class AlpacaNewsSource(Source):
    type_name = "alpaca_news"
    default_tier = SourceTier.MEDIA

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.key = cfg.get("key", "")
        self.secret = cfg.get("secret", "")
        if not (self.key and self.secret):
            raise ValueError(
                f"source '{self.name}': alpaca_news needs key and secret (free at alpaca.markets)"
            )
        self.url = cfg.get("url", STREAM_URL)
        self.symbols: list[str] = list(cfg.get("symbols", ["*"]))

    def to_item(self, n: dict[str, Any]) -> NewsItem:
        item = self.make_item(
            title=n.get("headline", ""),
            summary=strip_html(n.get("summary") or n.get("content") or ""),
            url=n.get("url") or "",
            published=parse_datetime(n.get("created_at")),
            author=n.get("author", "") or n.get("source", ""),
            uid=f"alpaca:{n.get('id')}",
            tickers=[str(s).upper() for s in n.get("symbols") or []],
        )
        item.extra["wire"] = n.get("source", "")
        return item

    def handle_message(self, raw: str) -> list[NewsItem]:
        msgs = json.loads(raw)
        if isinstance(msgs, dict):
            msgs = [msgs]
        items = []
        for m in msgs:
            kind = m.get("T")
            if kind == "error":
                raise ConnectionError(f"Alpaca error {m.get('code')}: {m.get('msg')}")
            if kind == "n" and m.get("headline"):
                items.append(self.to_item(m))
        return items

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            try:
                async with self.ctx.http.session.ws_connect(
                    self.url, heartbeat=30, **ws_receive_timeout(120)
                ) as ws:
                    await ws.send_str(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                    await ws.send_str(json.dumps({"action": "subscribe", "news": self.symbols}))
                    self.health.connected = True
                    self._note_ok()
                    attempt = 0
                    log.info("[%s] Alpaca news stream connected", self.name)
                    async for msg in ws:
                        if stop.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.health.last_ok = time.time()
                            self.health.polls += 1
                            for item in self.handle_message(msg.data):
                                await self._deliver(item, emit)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    if not stop.is_set():
                        raise ConnectionError("Alpaca stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.connected = False
                self._note_error(exc)
                attempt += 1
                delay = min(300.0, 2 ** min(attempt, 8)) * random.uniform(0.8, 1.2)
                if "402" in str(exc) or "auth" in str(exc).lower():
                    delay = max(delay, 300.0)  # bad keys/plan: don't hammer
                log.warning("[%s] Alpaca news error (%s); reconnecting in %.0fs", self.name, exc, delay)
                await _sleep_or_stop(stop, delay)
        self.health.connected = False

    async def check(self) -> list[NewsItem]:
        resp = await self.ctx.http.get(
            REST_URL,
            params={"limit": 5},
            headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret},
        )
        return [self.to_item(n) for n in resp.json().get("news", [])]
