"""Small shared helpers."""

from __future__ import annotations

import calendar
import html
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "cmpid",
    "mod",
    "ref",
    "src",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "taid",
    "ncid",
    "guccounter",
}


def strip_html(text: str | None, limit: int | None = 600) -> str:
    if not text:
        return ""
    out = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def struct_to_ts(st: time.struct_time | None) -> float | None:
    if not st:
        return None
    try:
        return float(calendar.timegm(st))
    except (TypeError, ValueError, OverflowError):
        return None


def parse_datetime(value: str | None) -> float | None:
    """Parse ISO-8601 or RFC-822 timestamps into unix seconds (UTC assumed if naive)."""
    if not value:
        return None
    value = value.strip()
    try:
        iso = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def normalize_url(url: str) -> str:
    """Canonical form for cross-source de-duplication (drops tracking params, fragments)."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS
    ]
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.0f}d"


def fmt_pct(p: float) -> str:
    return f"{p:+.2f}%"
