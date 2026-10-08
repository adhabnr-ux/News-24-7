"""Notification channels. Each one is a few lines: build a payload, POST it."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import platform
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from typing import Any

from ..http import HttpClient
from ..models import Alert, Severity
from .format import markdown_body, plain_body, short_title

log = logging.getLogger(__name__)


class Notifier:
    name = "base"

    def __init__(self, options: dict[str, Any], http: HttpClient, min_severity: Severity) -> None:
        self.options = options
        self.http = http
        self.min_severity = min_severity
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.validate()

    def validate(self) -> None:  # raise ValueError on missing options
        pass

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not self.options.get(k)]
        if missing:
            raise ValueError(f"notify.{self.name}: missing {', '.join(missing)}")

    async def send(self, alert: Alert) -> None:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "min_severity": self.min_severity.name,
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------------------- local


class ConsoleNotifier(Notifier):
    name = "console"
    COLORS = {
        Severity.LOW: "\033[90m",
        Severity.MEDIUM: "\033[33m",
        Severity.HIGH: "\033[38;5;208m",
        Severity.CRITICAL: "\033[1;31m",
    }

    async def send(self, alert: Alert) -> None:
        color = (
            self.COLORS[alert.severity] if sys.stdout.isatty() and not self.options.get("no_color") else ""
        )
        reset = "\033[0m" if color else ""
        bell = "\a" if self.options.get("bell") and alert.severity >= Severity.CRITICAL else ""
        body = "\n".join("    " + line for line in plain_body(alert).splitlines())
        print(f"{bell}{color}{alert.severity.name:<8} {short_title(alert)}{reset}\n{body}\n", flush=True)


class DesktopNotifier(Notifier):
    """Native pop-up: notify-send (Linux), osascript (macOS), PowerShell toast (Windows)."""

    name = "desktop"

    async def send(self, alert: Alert) -> None:
        title = short_title(alert)[:120]
        body = plain_body(alert, include_url=False)[:400]
        system = platform.system()
        if system == "Darwin":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            sound = ' sound name "Glass"' if alert.severity >= Severity.HIGH else ""
            cmd = ["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"{sound}']
        elif system == "Windows":
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$x = $t.GetElementsByTagName('text'); $x.Item(0).AppendChild($t.CreateTextNode($env:N247_T)) > $null;"
                "$x.Item(1).AppendChild($t.CreateTextNode($env:N247_B)) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('News247').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            env = {**os.environ, "N247_T": title, "N247_B": body}
            await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-Command", ps],
                env=env,
                capture_output=True,
                timeout=15,
            )
            return
        else:
            if not shutil.which("notify-send"):
                raise RuntimeError("notify-send not found (install libnotify-bin)")
            urgency = "critical" if alert.severity >= Severity.CRITICAL else "normal"
            cmd = ["notify-send", "-a", "News247", "-u", urgency, title, body]
        await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=15, check=True)


# --------------------------------------------------------------------------- push


class NtfyNotifier(Notifier):
    """ntfy.sh — free phone push with no account: install the ntfy app, subscribe to your topic."""

    name = "ntfy"
    PRIORITY = {Severity.LOW: 2, Severity.MEDIUM: 3, Severity.HIGH: 4, Severity.CRITICAL: 5}

    def validate(self) -> None:
        self.require("topic")

    async def send(self, alert: Alert) -> None:
        server = str(self.options.get("server", "https://ntfy.sh")).rstrip("/")
        tags = ["rotating_light" if alert.severity >= Severity.CRITICAL else "chart_with_downwards_trend"]
        payload: dict[str, Any] = {
            "topic": self.options["topic"],
            "title": short_title(alert)[:250],
            "message": plain_body(alert, include_url=False)[:3500],
            "priority": self.PRIORITY[alert.severity],
            "tags": tags,
        }
        if alert.url:
            payload["click"] = alert.url
            payload["actions"] = [{"action": "view", "label": "Open", "url": alert.url}]
        headers = {}
        if self.options.get("token"):
            headers["Authorization"] = f"Bearer {self.options['token']}"
        await self.http.post(server, json=payload, headers=headers)


class TelegramNotifier(Notifier):
    name = "telegram"

    def validate(self) -> None:
        self.require("bot_token", "chat_id")

    async def send(self, alert: Alert) -> None:
        text = (
            f"<b>{html.escape(short_title(alert))}</b>\n{html.escape(plain_body(alert, include_url=False))}"
        )
        if alert.url:
            text += f'\n<a href="{html.escape(alert.url, quote=True)}">Open source</a>'
        await self.http.post(
            f"{self.options.get('api_base', 'https://api.telegram.org')}/bot{self.options['bot_token']}/sendMessage",
            json={
                "chat_id": self.options["chat_id"],
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": not self.options.get("link_preview", False),
                "disable_notification": alert.severity < Severity.HIGH,
            },
        )


class PushoverNotifier(Notifier):
    name = "pushover"
    PRIORITY = {Severity.LOW: -1, Severity.MEDIUM: 0, Severity.HIGH: 0, Severity.CRITICAL: 1}

    def validate(self) -> None:
        self.require("token", "user")

    async def send(self, alert: Alert) -> None:
        data = {
            "token": self.options["token"],
            "user": self.options["user"],
            "title": short_title(alert)[:250],
            "message": plain_body(alert, include_url=False)[:1000],
            "priority": str(self.PRIORITY[alert.severity]),
        }
        if alert.url:
            data["url"] = alert.url
        if alert.severity >= Severity.CRITICAL:
            data["sound"] = str(self.options.get("critical_sound", "siren"))
        await self.http.post(
            f"{self.options.get('api_base', 'https://api.pushover.net')}/1/messages.json", data=data
        )


# --------------------------------------------------------------------------- chat / hooks


class DiscordNotifier(Notifier):
    name = "discord"
    COLORS = {
        Severity.LOW: 0x95A5A6,
        Severity.MEDIUM: 0xF1C40F,
        Severity.HIGH: 0xE67E22,
        Severity.CRITICAL: 0xE74C3C,
    }

    def validate(self) -> None:
        self.require("webhook_url")

    async def send(self, alert: Alert) -> None:
        embed: dict[str, Any] = {
            "title": short_title(alert)[:256],
            "description": plain_body(alert, include_url=False)[:4000],
            "color": self.COLORS[alert.severity],
        }
        if alert.url:
            embed["url"] = alert.url
        payload: dict[str, Any] = {"embeds": [embed], "username": "News247"}
        if alert.severity >= Severity.CRITICAL and self.options.get("mention"):
            payload["content"] = self.options["mention"]  # e.g. "@here" or "<@USER_ID>"
        await self.http.post(self.options["webhook_url"], json=payload)


class SlackNotifier(Notifier):
    name = "slack"

    def validate(self) -> None:
        self.require("webhook_url")

    async def send(self, alert: Alert) -> None:
        title = short_title(alert)
        link = f"<{alert.url}|{title}>" if alert.url else title
        await self.http.post(
            self.options["webhook_url"],
            json={
                "text": title,
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*{link}*"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": markdown_body(alert)[:2900]}},
                ],
            },
        )


class WebhookNotifier(Notifier):
    """POSTs the full alert as JSON to any URL (Zapier, n8n, Home Assistant, your own bot…)."""

    name = "webhook"

    def validate(self) -> None:
        self.require("url")

    async def send(self, alert: Alert) -> None:
        await self.http.post(
            self.options["url"], json=alert.to_dict(), headers=dict(self.options.get("headers", {}))
        )


class EmailNotifier(Notifier):
    name = "email"

    def validate(self) -> None:
        self.require("smtp_host", "from", "to")

    def _send_sync(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = short_title(alert)[:200]
        msg["From"] = self.options["from"]
        to = self.options["to"]
        msg["To"] = ", ".join(to) if isinstance(to, list) else to
        msg.set_content(plain_body(alert))
        port = int(self.options.get("smtp_port", 587))
        if port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.options["smtp_host"], port, timeout=20)
        else:
            server = smtplib.SMTP(self.options["smtp_host"], port, timeout=20)
        with server:
            if port != 465 and self.options.get("starttls", True):
                server.starttls()
            if self.options.get("username"):
                server.login(self.options["username"], self.options.get("password", ""))
            server.send_message(msg)

    async def send(self, alert: Alert) -> None:
        await asyncio.to_thread(self._send_sync, alert)


CHANNEL_CLASSES: dict[str, type[Notifier]] = {
    cls.name: cls
    for cls in (
        ConsoleNotifier,
        DesktopNotifier,
        NtfyNotifier,
        TelegramNotifier,
        PushoverNotifier,
        DiscordNotifier,
        SlackNotifier,
        WebhookNotifier,
        EmailNotifier,
    )
}
