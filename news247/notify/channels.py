"""Notification channels. Each one is a few lines: build a payload, POST it."""

from __future__ import annotations

import asyncio
import contextlib
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
from .format import markdown_body, plain_body, short_title, sms_text

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


# --------------------------------------------------------------------------- texts to your phone

# Sends through the Messages app of a Mac signed in to iMessage. Arguments are passed via argv,
# never interpolated into the script, so headlines can't inject AppleScript. AppleScript compiles
# a whole script up front and Apple renamed the dictionary terms over the years, so each syntax
# is a separate script, tried in order:
#   1. participant-of-account  (macOS 11 Big Sur and later, including macOS 26)
#   2. buddy-of-service        (macOS 10.x)
#   3. an existing 1:1 chat id (last resort)
_IMESSAGE_HEAD = """
on run argv
    set targetHandle to item 1 of argv
    set messageText to item 2 of argv
    set wantSMS to (item 3 of argv is "sms")
    tell application "Messages"
"""
_IMESSAGE_TAIL = """
    end tell
end run
"""
IMESSAGE_SCRIPTS = [
    _IMESSAGE_HEAD
    + """
        if wantSMS then
            set targetAccount to 1st account whose service type = SMS
        else
            set targetAccount to 1st account whose service type = iMessage
        end if
        send messageText to participant targetHandle of targetAccount
"""
    + _IMESSAGE_TAIL,
    _IMESSAGE_HEAD
    + """
        if wantSMS then
            set targetService to 1st service whose service type = SMS
        else
            set targetService to 1st service whose service type = iMessage
        end if
        send messageText to buddy targetHandle of targetService
"""
    + _IMESSAGE_TAIL,
    _IMESSAGE_HEAD
    + """
        if wantSMS then
            send messageText to chat id ("SMS;-;" & targetHandle)
        else
            send messageText to chat id ("iMessage;-;" & targetHandle)
        end if
"""
    + _IMESSAGE_TAIL,
]


class IMessageNotifier(Notifier):
    """iMessage via the Messages app on a Mac (free; the Mac must stay on and signed in).

    Options: ``to`` (phone number(s) like "+15551234567" or Apple ID e-mails),
    ``service``: "imessage" (default), "sms" (needs iPhone Text Message Forwarding to this Mac),
    or "auto" (iMessage, falling back to SMS).
    """

    name = "imessage"

    def validate(self) -> None:
        self.require("to")
        to = self.options["to"]
        raw = to if isinstance(to, list) else str(to).split(",")  # "+1555...,me@icloud.com" works too
        self.recipients = [str(t).strip() for t in raw if str(t).strip()]
        if not self.recipients:
            raise ValueError("notify.imessage: missing to")
        self.service = str(self.options.get("service", "imessage")).lower()
        if self.service not in ("imessage", "sms", "auto"):
            raise ValueError("notify.imessage: service must be imessage, sms or auto")

    async def _run_script(self, script: str, handle: str, text: str, service: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            handle,
            text,
            service,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Messages did not respond within 30s (is it running and signed in?)") from None
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace").strip() or f"osascript exited {proc.returncode}")

    async def _osascript(self, handle: str, text: str, service: str) -> None:
        errors = []
        for i in self._script_order():
            try:
                await self._run_script(IMESSAGE_SCRIPTS[i], handle, text, service)
                self._working_script = i  # remember what works on this Mac
                return
            except RuntimeError as exc:
                msg = str(exc)
                if "-1743" in msg or "Not authorized" in msg:
                    raise RuntimeError(
                        msg + " — allow Automation access: System Settings > Privacy & Security > Automation"
                    ) from None
                errors.append(msg)
        raise RuntimeError("Messages could not send: " + " | ".join(errors))

    def _script_order(self) -> list[int]:
        first = getattr(self, "_working_script", 0)
        return [first] + [i for i in range(len(IMESSAGE_SCRIPTS)) if i != first]

    async def send(self, alert: Alert) -> None:
        if platform.system() != "Darwin":
            raise RuntimeError(
                "imessage needs macOS with Messages signed in (use sendblue/bluebubbles/twilio elsewhere)"
            )
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        for handle in self.recipients:
            if self.service == "auto":
                try:
                    await self._osascript(handle, text, "imessage")
                except RuntimeError:
                    await self._osascript(handle, text, "sms")
            else:
                await self._osascript(handle, text, self.service)


class TwilioSMSNotifier(Notifier):
    """Plain SMS via Twilio (green bubble). Options: ``account_sid``, ``auth_token``,
    ``from`` (your Twilio number or messaging_service_sid), ``to`` (number or list)."""

    name = "twilio"

    def validate(self) -> None:
        self.require("account_sid", "auth_token", "to")
        if not (self.options.get("from") or self.options.get("messaging_service_sid")):
            raise ValueError("notify.twilio: missing from (or messaging_service_sid)")

    async def send(self, alert: Alert) -> None:
        import base64

        sid = self.options["account_sid"]
        auth = base64.b64encode(f"{sid}:{self.options['auth_token']}".encode()).decode()
        base = self.options.get("api_base", "https://api.twilio.com")
        to = self.options["to"]
        for number in to if isinstance(to, list) else [to]:
            data = {"To": str(number), "Body": sms_text(alert, limit=int(self.options.get("max_chars", 320)))}
            if self.options.get("messaging_service_sid"):
                data["MessagingServiceSid"] = self.options["messaging_service_sid"]
            else:
                data["From"] = str(self.options["from"])
            await self.http.post(
                f"{base}/2010-04-01/Accounts/{sid}/Messages.json",
                data=data,
                headers={"Authorization": f"Basic {auth}"},
            )


def _numbers(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else str(value).split(",")
    return [str(v).strip() for v in raw if str(v).strip()]


class BlueBubblesNotifier(Notifier):
    """iMessage through a BlueBubbles server (a free app on an always-on Mac). Lets the monitor
    itself run anywhere (cloud, Linux box) while the Mac only relays messages.
    Options: ``server`` (e.g. https://my-mac.example.com), ``password``, ``to``, ``method``
    ("apple-script" default, or "private-api")."""

    name = "bluebubbles"

    def validate(self) -> None:
        self.require("server", "password", "to")
        self.recipients = _numbers(self.options["to"])

    async def send(self, alert: Alert) -> None:
        import uuid

        from ..http import HTTPError

        server = str(self.options["server"]).rstrip("/")
        params = {"password": self.options["password"]}  # kept out of the URL string we log
        method = self.options.get("method", "apple-script")
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        for handle in self.recipients:
            body = {
                "chatGuid": f"iMessage;-;{handle}",
                "tempGuid": str(uuid.uuid4()),
                "message": text,
                "method": method,
            }
            try:
                await self.http.post(f"{server}/api/v1/message/text", params=params, json=body)
            except HTTPError as exc:
                if exc.status not in (400, 404, 500):
                    raise
                # no existing conversation with this number yet: start one
                await self.http.post(
                    f"{server}/api/v1/chat/new",
                    params=params,
                    json={
                        "addresses": [handle],
                        "message": text,
                        "service": "iMessage",
                        "method": method,
                        "tempGuid": str(uuid.uuid4()),
                    },
                )


class SendblueNotifier(Notifier):
    """Hosted iMessage API (no Mac needed). Options: ``api_key_id``, ``api_secret``, ``to``,
    ``from_number`` (your Sendblue line). On the free sandbox, text your Sendblue number once
    from your phone first so it is a verified contact."""

    name = "sendblue"

    def validate(self) -> None:
        self.require("api_key_id", "api_secret", "to")
        self.recipients = _numbers(self.options["to"])

    async def send(self, alert: Alert) -> None:
        base = str(self.options.get("api_base", "https://api.sendblue.com")).rstrip("/")
        headers = {
            "sb-api-key-id": self.options["api_key_id"],
            "sb-api-secret-key": self.options["api_secret"],
        }
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        for number in self.recipients:
            body: dict[str, Any] = {"number": number, "content": text}
            if self.options.get("from_number"):
                body["from_number"] = self.options["from_number"]
            resp = await self.http.post(f"{base}/api/send-message", json=body, headers=headers)
            with contextlib.suppress(ValueError):
                data = resp.json()
                if isinstance(data, dict) and str(data.get("status", "")).upper() == "ERROR":
                    raise RuntimeError(f"Sendblue: {data.get('error_message') or data}")


class BlooioNotifier(Notifier):
    """Hosted iMessage API (no Mac needed). Options: ``api_key``, ``to``."""

    name = "blooio"

    def validate(self) -> None:
        self.require("api_key", "to")
        self.recipients = _numbers(self.options["to"])

    async def send(self, alert: Alert) -> None:
        import uuid
        from urllib.parse import quote

        base = str(self.options.get("api_base", "https://backend.blooio.com")).rstrip("/")
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        for number in self.recipients:
            await self.http.post(
                f"{base}/v2/api/chats/{quote(number, safe='')}/messages",
                json={"text": text},
                headers={
                    "Authorization": f"Bearer {self.options['api_key']}",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
            )


class TextbeltNotifier(Notifier):
    """Plain SMS via Textbelt: prepaid key, no carrier registration paperwork.
    Options: ``key``, ``to``. Note: Textbelt may hold messages containing links until your
    account is verified, so ``include_url`` defaults to false."""

    name = "textbelt"

    def validate(self) -> None:
        self.require("key", "to")
        self.recipients = _numbers(self.options["to"])

    async def send(self, alert: Alert) -> None:
        base = str(self.options.get("api_base", "https://textbelt.com")).rstrip("/")
        shown = alert if self.options.get("include_url", False) else _without_url(alert)
        text = sms_text(shown, limit=int(self.options.get("max_chars", 320)))
        for number in self.recipients:
            resp = await self.http.post(
                f"{base}/text", data={"phone": number, "message": text, "key": self.options["key"]}
            )
            data = resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Textbelt: {data.get('error') or data}")


def _without_url(alert: Alert) -> Alert:
    import dataclasses

    return dataclasses.replace(alert, url="")


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
        IMessageNotifier,
        TwilioSMSNotifier,
        BlueBubblesNotifier,
        SendblueNotifier,
        BlooioNotifier,
        TextbeltNotifier,
    )
}
