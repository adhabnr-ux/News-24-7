"""Public Telegram channels via their web preview (https://t.me/s/<channel>) — no account needed.

Many headline "squawk" services post to Telegram within seconds of a story breaking. The
preview page lists the latest ~20 posts; new post ids are new headlines.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import NewsItem, SourceTier
from ..util import parse_datetime, strip_html
from .base import PollingSource

_POST_RE = re.compile(r'data-post="([^"/]+)/(\d+)"')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def parse_channel_page(html: str) -> list[dict[str, Any]]:
    """Return [{channel, id, text, time}] for every post on a t.me/s/ page (oldest first)."""
    matches = list(_POST_RE.finditer(html))
    posts = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        chunk = html[m.end() : end]
        tm = _TEXT_RE.search(chunk)
        if not tm:
            continue  # photo/sticker-only post
        lines = [strip_html(seg, None) for seg in _BR_RE.split(tm.group(1))]
        text = "\n".join(line for line in lines if line)[:1000]
        ts = _TIME_RE.search(chunk)
        posts.append(
            {"channel": m.group(1), "id": int(m.group(2)), "text": text, "time": ts.group(1) if ts else None}
        )
    return posts


class TelegramChannelSource(PollingSource):
    """Options: ``channels`` (list of public channel usernames, without @)."""

    type_name = "telegram"
    default_interval = 10.0
    default_tier = SourceTier.MEDIA

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        cfg = {"browser_headers": True, **cfg}
        super().__init__(cfg, ctx)
        self.channels: list[str] = [str(c).lstrip("@") for c in cfg.get("channels", [])]
        if not self.channels:
            raise ValueError(f"source '{self.name}': telegram needs channels")

    async def fetch(self) -> list[NewsItem]:
        out: list[NewsItem] = []
        errors: list[Exception] = []
        for ch in self.channels:
            try:
                resp = await self.ctx.http.get(f"https://t.me/s/{ch}", headers=self.headers)
                out.extend(self.parse(resp.text()))
            except Exception as exc:  # noqa: BLE001 - one bad channel must not hide the others
                errors.append(exc)
        if errors and len(errors) == len(self.channels):
            raise errors[0]
        return out

    def parse(self, html: str) -> list[NewsItem]:
        items = []
        for p in parse_channel_page(html):
            first_line = p["text"].split("\n")[0].strip()
            rest = p["text"][len(first_line) :].strip()
            items.append(
                self.make_item(
                    title=f"@{p['channel']}: {first_line}",
                    summary=rest,
                    url=f"https://t.me/{p['channel']}/{p['id']}",
                    published=parse_datetime(p["time"]),
                    author=p["channel"],
                    uid=f"tg:{p['channel'].lower()}:{p['id']}",
                )
            )
            items[-1].extra.setdefault("relay", True)
        return items
