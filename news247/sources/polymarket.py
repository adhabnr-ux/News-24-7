"""Polymarket odds jumps: a leading (noisy) signal for geopolitical and policy news.

In 2026, well-timed bets moved Polymarket before the Feb 28 Iran strikes and the Apr 7 US-Iran
ceasefire were public. This source watches the most-traded open markets (Gamma API, free, no
key) and emits an item when a market's "Yes" price moves at least ``min_move_pts`` percentage
points within ``window_s`` seconds on real volume. Treat it as a heads-up, not a confirmation.

Options: ``min_move_pts`` (default 10), ``window_s`` (default 300), ``min_volume_24h`` (USD,
default 25000), ``limit`` (markets to watch, default 150), ``cooldown_s`` (default 1800: one
alert per market per half hour unless it moves another ``min_move_pts``). Use ``include`` /
``exclude`` (regex) to keep it to market-relevant questions.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from ..models import NewsItem, SourceTier
from .base import PollingSource

GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def _yes_price(m: dict[str, Any]) -> float | None:
    raw = m.get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        outcomes = m.get("outcomes")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes or []
        idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
        return float(prices[idx])
    except (TypeError, ValueError, IndexError, KeyError):
        last = m.get("lastTradePrice")
        return float(last) if isinstance(last, (int, float)) else None


def _money(v: float) -> str:
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


class PolymarketSource(PollingSource):
    type_name = "polymarket"
    default_interval = 30.0
    default_tier = SourceTier.SOCIAL

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.url: str = cfg.get("url", GAMMA_URL)
        self.min_move = float(cfg.get("min_move_pts", 10)) / 100.0
        self.window_s = float(cfg.get("window_s", 300))
        self.min_volume = float(cfg.get("min_volume_24h", 25_000))
        self.limit = int(cfg.get("limit", 150))
        self.cooldown_s = float(cfg.get("cooldown_s", 1800))
        self.history: dict[str, deque[tuple[float, float]]] = {}
        self.alerted: dict[str, tuple[float, float]] = {}  # market id -> (time, price) of last alert

    async def fetch(self) -> list[NewsItem]:
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": str(self.limit),
        }
        resp = await self.ctx.http.get(self.url, params=params, headers=self.headers)
        return self.parse(resp.json())

    def parse(self, data: Any, now: float | None = None) -> list[NewsItem]:
        now = time.time() if now is None else now
        markets = data if isinstance(data, list) else (data or {}).get("data", [])
        if not isinstance(markets, list):
            raise ValueError("unexpected Polymarket response (expected a list of markets)")
        out: list[NewsItem] = []
        for m in markets:
            mid = str(m.get("id") or m.get("conditionId") or "")
            price = _yes_price(m)
            if not mid or price is None:
                continue
            hist = self.history.setdefault(mid, deque())
            hist.append((now, price))
            while hist and now - hist[0][0] > self.window_s:
                hist.popleft()
            if len(hist) < 2:
                continue
            # biggest move inside the window, measured from the extreme opposite the latest price
            lo = min(p for _, p in hist)
            hi = max(p for _, p in hist)
            delta = price - lo if price - lo >= hi - price else price - hi
            volume = float(m.get("volume24hr") or 0)
            if abs(delta) < self.min_move or volume < self.min_volume:
                continue
            last = self.alerted.get(mid)
            if last and now - last[0] < self.cooldown_s and abs(price - last[1]) < self.min_move:
                continue
            self.alerted[mid] = (now, price)
            start_t = next(t for t, p in hist if p == (lo if delta > 0 else hi))
            minutes = max(1, round((now - start_t) / 60))
            verb = "jumps" if delta > 0 else "plunges"
            question = str(m.get("question") or m.get("title") or "?").strip()
            events = m.get("events") or []
            slug = (events[0].get("slug") if events and isinstance(events[0], dict) else None) or m.get(
                "slug"
            )
            title = (
                f"Polymarket: '{question}' {verb} {abs(delta) * 100:.0f} pts to {price * 100:.0f}% "
                f"in {minutes} min ({_money(volume)} traded in 24h)"
            )
            item = self.make_item(
                title=title,
                url=f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com",
                published=now,
                uid=f"polymarket-{mid}-{int(now)}",
            )
            item.extra.update({"market_id": mid, "price": price, "move": delta, "volume24h": volume})
            out.append(item)
        # forget markets that dropped out of the top list
        seen = {str(m.get("id") or m.get("conditionId") or "") for m in markets}
        for mid in [k for k in self.history if k not in seen]:
            del self.history[mid]
        return out
