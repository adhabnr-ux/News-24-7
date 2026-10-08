"""Source registry: maps the ``type`` field in config to a Source class."""

from __future__ import annotations

import logging
from typing import Any

from .apis import FinnhubNewsSource, HackerNewsSource, RedditSource
from .base import PollingSource, Source, SourceContext
from .bluesky import BlueskySource
from .halts import TradingHaltsSource
from .pagewatch import PageWatchSource
from .rss import RSSSource
from .sec_edgar import SECEdgarSource
from .social import MastodonSource, XSource
from .telegram import TelegramChannelSource

log = logging.getLogger(__name__)

SOURCE_TYPES: dict[str, type[Source]] = {
    cls.type_name: cls
    for cls in (
        RSSSource,
        SECEdgarSource,
        TradingHaltsSource,
        PageWatchSource,
        HackerNewsSource,
        RedditSource,
        FinnhubNewsSource,
        XSource,
        MastodonSource,
        BlueskySource,
        TelegramChannelSource,
    )
}


def build_sources(
    configs: list[dict[str, Any]], ctx: SourceContext, include_disabled: bool = False
) -> list[Source]:
    """Instantiate enabled sources. Misconfigured ones are logged and skipped, never fatal."""
    out: list[Source] = []
    for cfg in configs:
        if not cfg.get("enabled", True) and not include_disabled:
            continue
        cls = SOURCE_TYPES.get(str(cfg.get("type")))
        if cls is None:
            log.error(
                "source '%s': unknown type '%s' (known: %s)",
                cfg.get("name"),
                cfg.get("type"),
                ", ".join(SOURCE_TYPES),
            )
            continue
        try:
            out.append(cls(cfg, ctx))
        except (ValueError, KeyError, TypeError) as exc:
            log.error("source '%s' disabled: %s", cfg.get("name"), exc)
    return out


__all__ = ["SOURCE_TYPES", "PollingSource", "Source", "SourceContext", "build_sources"]
