"""Alert delivery: routes alerts to channels with severity filters, quiet hours and rate limits."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from ..config import NotifyConfig, as_bool
from ..http import HttpClient
from ..models import Alert, Severity
from .channels import CHANNEL_CLASSES, Notifier, PhoneChannel, normalize_phone

log = logging.getLogger(__name__)

# channels that are local/log-like and therefore exempt from quiet hours and rate limits
LOCAL_CHANNELS = {"console", "webhook"}


def _parse_hhmm(value: str) -> dtime:
    h, m = str(value).split(":")
    return dtime(int(h), int(m))


def in_quiet_hours(quiet: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not quiet or not quiet.get("start") or not quiet.get("end"):
        return False
    if now is None:
        tz = quiet.get("timezone")
        now = datetime.now(ZoneInfo(tz)) if tz else datetime.now()
    start, end = _parse_hhmm(quiet["start"]), _parse_hhmm(quiet["end"])
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # window crosses midnight


class Dispatcher:
    def __init__(self, cfg: NotifyConfig, http: HttpClient) -> None:
        self.cfg = cfg
        self.channels: list[Notifier] = []
        for ch in cfg.channels:
            if not ch.enabled:
                continue
            cls = CHANNEL_CLASSES[ch.name]
            opts = dict(ch.options)
            backup = as_bool(opts.pop("backup", False), default=False)
            try:
                notifier = cls(opts, http, ch.min_severity)
            except ValueError as exc:
                log.error("notification channel disabled: %s", exc)
                continue
            notifier.backup = backup
            self.channels.append(notifier)
        self._recent: deque[float] = deque()
        self.suppressed = 0
        # set by texting the relay ("pause 2h", "critical") or from code; applies to every
        # channel that reaches your phone, never to the console/dashboard
        self.paused_until = 0.0  # unix time; inf = until resumed
        self.phone_min: Severity | None = None

    # ------------------------------------------------------------------ phone controls

    def pause(self, seconds: float | None) -> float:
        self.paused_until = float("inf") if seconds is None else time.time() + seconds
        return self.paused_until

    def resume(self) -> None:
        self.paused_until = 0.0

    @property
    def paused(self) -> bool:
        return time.time() < self.paused_until

    def set_phone_min(self, severity: Severity | None) -> None:
        self.phone_min = severity

    def _rate_ok(self, alert: Alert) -> bool:
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= self.cfg.rate_limit_per_minute and alert.severity < Severity.CRITICAL:
            return False
        self._recent.append(now)
        return True

    def targets(self, alert: Alert) -> list[Notifier]:
        quiet = in_quiet_hours(self.cfg.quiet_hours)
        quiet_min = (
            Severity.parse((self.cfg.quiet_hours or {}).get("min_severity", "critical")) if quiet else None
        )
        paused = self.paused
        out = []
        for ch in self.channels:
            local = ch.name in LOCAL_CHANNELS
            floor = ch.min_severity if local or self.phone_min is None else self.phone_min
            if alert.severity < floor:
                continue
            if not local and (paused or (quiet_min is not None and alert.severity < quiet_min)):
                continue
            out.append(ch)
        return out

    @property
    def has_primary_remote(self) -> bool:
        return any(c.name not in LOCAL_CHANNELS and not c.backup for c in self.channels)

    async def dispatch(self, alert: Alert) -> dict[str, str]:
        targets = self.targets(alert)
        backups = [c for c in targets if c.backup]
        targets = [c for c in targets if not c.backup]
        remote = [c for c in targets if c.name not in LOCAL_CHANNELS]
        if (remote or backups) and not self._rate_ok(alert):
            self.suppressed += 1
            log.warning(
                "rate limit reached (%d/min): not pushing '%s'",
                self.cfg.rate_limit_per_minute,
                alert.title[:80],
            )
            targets = [c for c in targets if c.name in LOCAL_CHANNELS]
            remote, backups = [], []
        results = await asyncio.gather(*(self._send(ch, alert) for ch in targets))
        out = dict(zip((c.name for c in targets), results))
        failed = [c for c in remote if out.get(c.name) != "ok"]
        # backups stand in when every primary phone channel failed (e.g. the iMessage relay's Mac
        # is asleep), or when they are the only phone channels configured
        if backups and ((remote and len(failed) == len(remote)) or not self.has_primary_remote):
            names = ", ".join(c.name for c in failed) or "no primary channel"
            log.info(
                "primary phone channel(s) failed (%s): using backup %s",
                names,
                ", ".join(c.name for c in backups),
            )
            res = await asyncio.gather(*(self._send(ch, alert) for ch in backups))
            out.update(zip((c.name for c in backups), res))
            ok = [c.name for c, r in zip(backups, res) if r == "ok"]
            if ok:
                for ch in failed:
                    hook = getattr(ch, "superseded", None)
                    if hook is not None:
                        hook(alert, ok[0])
        return out

    async def _send(self, ch: Notifier, alert: Alert) -> str:
        try:
            await asyncio.wait_for(ch.send(alert), timeout=getattr(ch, "timeout", 20))
            ch.sent += 1
            return "ok"
        except Exception as exc:  # noqa: BLE001 - one broken channel must not block the others
            ch.failed += 1
            ch.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("notify via %s failed: %s", ch.name, ch.last_error)
            return f"error: {ch.last_error}"

    def describe(self) -> list[dict[str, Any]]:
        return [c.describe() for c in self.channels]

    @property
    def phone_channels(self) -> list[Notifier]:
        return [c for c in self.channels if isinstance(c, PhoneChannel)]

    def set_phone(self, numbers: list[str]) -> list[str]:
        """Point every phone channel (iMessage, Sendblue, SMS, ...) at these numbers."""
        for ch in self.phone_channels:
            ch.set_recipients(numbers)  # type: ignore[attr-defined]
        return self.phone_channels[0].recipients if self.phone_channels else []  # type: ignore[attr-defined]


__all__ = ["CHANNEL_CLASSES", "Dispatcher", "Notifier", "PhoneChannel", "in_quiet_hours", "normalize_phone"]
