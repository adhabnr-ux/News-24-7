"""Turning Alerts into human-readable text."""

from __future__ import annotations

import time

from ..models import Alert, PriceMove
from ..util import fmt_age, fmt_pct

ARROW = {"up": "▲", "down": "▼", "mixed": "◆", "unknown": "•", "none": "•"}


def window_label(seconds: int) -> str:
    if seconds >= 86400:
        return "today"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def move_headline(move: PriceMove) -> str:
    arrow = ARROW[move.direction]
    w = window_label(move.window_s)
    if move.is_group:
        name = move.symbol.split(":", 1)[1].upper()
        movers = sum(1 for c in move.members.values() if (c > 0) == (move.change_pct > 0))
        return f"{arrow} {name} stocks {fmt_pct(move.change_pct)} in {w} ({movers}/{len(move.members)} moving together)"
    if move.window_s >= 86400:
        return f"{arrow} {move.symbol} {fmt_pct(move.change_pct)} vs. previous close"
    return f"{arrow} {move.symbol} {fmt_pct(move.change_pct)} in {w}"


def short_title(alert: Alert) -> str:
    return f"{alert.severity.emoji} {alert.title}"


def plain_body(alert: Alert, include_url: bool = True) -> str:
    """Multi-line plain-text body shared by most channels."""
    lines: list[str] = []
    if alert.kind == "news" and alert.item and alert.analysis:
        it, an = alert.item, alert.analysis
        when = f"published {fmt_age(time.time() - it.published)} ago" if it.published else "just detected"
        lag = f", caught {fmt_age(it.latency)} after publish" if it.latency is not None else ""
        lines.append(f"{it.source} ({it.tier.name.lower()}) · {when}{lag}")
        if an.summary:
            lines.append(f"Why it matters: {an.summary}")
        if an.tickers:
            lines.append(f"Watch: {' '.join(an.tickers[:10])}  {ARROW.get(an.direction, '•')} {an.direction}")
        if an.themes:
            lines.append("Themes: " + ", ".join(t.replace("_", " ") for t in an.themes))
        lines.append(f"Impact score {an.score:.0f}/100" + (" (AI-reviewed)" if an.llm_used else ""))
    if alert.body:
        lines.append(alert.body)
    for rel in alert.related[:3]:
        if rel.get("kind") == "price":
            lines.append(f"Market now: {rel['text']}")
        else:
            lines.append(
                f"Possible catalyst ({rel.get('age', '?')} earlier): {rel['title']} [{rel['source']}]"
            )
    if include_url and alert.url:
        lines.append(alert.url)
    return "\n".join(lines)


def markdown_body(alert: Alert) -> str:
    """Markdown-ish body for Discord/Slack (links rendered by the client)."""
    return plain_body(alert, include_url=False) + (f"\n<{alert.url}>" if alert.url else "")


def sms_text(alert: Alert, limit: int = 700) -> str:
    """Compact text for iMessage/SMS: headline first, then just what you need to act."""
    lines = [short_title(alert)]
    if alert.kind == "news" and alert.item and alert.analysis:
        it, an = alert.item, alert.analysis
        bits = []
        if an.tickers:
            bits.append(f"{ARROW.get(an.direction, '•')} {' '.join(an.tickers[:6])}")
        lag = f"{fmt_age(it.latency)} after post" if it.latency is not None else "just now"
        bits.append(f"{it.source} · {lag}")
        lines.append(" · ".join(bits))
        if an.summary:
            lines.append(an.summary)
    elif alert.body:
        lines.append(alert.body.split("\n")[0])
    for rel in alert.related[:1]:
        if rel.get("kind") == "price":
            lines.append(f"Now: {rel['text']}")
        else:
            lines.append(f"Likely cause ({rel.get('age', '?')} ago): {rel['title']}")
    if alert.body and alert.kind == "news" and "Confirmed" in alert.body:
        lines.append(alert.body)
    if alert.url:
        lines.append(alert.url)
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    url = f"\n{alert.url}" if alert.url else ""
    return text[: max(0, limit - len(url) - 1)].rstrip() + "…" + url


def whatsapp_text(alert: Alert, limit: int = 1500) -> str:
    """The SMS layout with a bold headline (WhatsApp renders *text* as bold)."""
    text = sms_text(alert, limit=limit)
    head, sep, rest = text.partition("\n")
    head = head.replace("*", "").strip()  # squawk headlines start with '*', which would break the bold
    return f"*{head}*{sep}{rest}"
