"""Commands you can text to your iMessage relay, e.g. "pause 2h", "critical", "status"."""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNITS = {"m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?)\b")
HELP = (
    "News247 commands:\n"
    "PAUSE 2h · pause texts (30m, 2h, 1d…; PAUSE alone = 1h)\n"
    "STOP · pause until you text RESUME\n"
    "RESUME · texts back on\n"
    "CRITICAL · only the biggest alerts\n"
    "MORE · medium alerts too\n"
    "NORMAL · back to the default\n"
    "STATUS · is everything running?"
)


@dataclass
class PhoneCommand:
    action: str  # pause | resume | level | status | help
    seconds: float | None = None  # pause duration; None = until resumed
    level: str | None = None  # CRITICAL | HIGH | MEDIUM | None (= default)


def parse_duration(text: str) -> float | None:
    m = _DURATION_RE.search(text.lower())
    if not m:
        return None
    unit = m.group(2)
    key = "min" if unit.startswith("min") else unit[0] if unit[0] in "hd" else "m"
    return float(m.group(1)) * _UNITS[key]


def parse_command(text: str) -> PhoneCommand | None:
    """Understands short replies; anything else (e.g. "thanks!") is ignored."""
    t = re.sub(r"[^\w\s.]", " ", text.lower()).strip()
    words = t.split()
    if not words or len(words) > 5:
        return None
    first, rest = words[0], " ".join(words[1:])
    if first in ("pause", "mute", "snooze", "silence", "quiet", "hush"):
        if rest in ("", "for"):
            return PhoneCommand("pause", 3600.0)
        dur = parse_duration(rest)
        return PhoneCommand("pause", dur) if dur else None
    if t in ("stop", "stop all", "stop texts", "off"):
        return PhoneCommand("pause", None)
    if t in ("resume", "unpause", "unmute", "start", "on", "go", "continue", "back on"):
        return PhoneCommand("resume")
    if t in ("critical", "critical only", "only critical", "urgent", "urgent only", "less", "fewer"):
        return PhoneCommand("level", level="CRITICAL")
    if t in ("more", "medium", "everything", "all alerts"):
        return PhoneCommand("level", level="MEDIUM")
    if t in ("normal", "default", "high", "important", "reset", "all"):
        return PhoneCommand("level", level=None)
    if t in ("status", "ping", "alive", "health", "are you there", "you there"):
        return PhoneCommand("status")
    if t in ("help", "commands", "menu", "h"):
        return PhoneCommand("help")
    return None
