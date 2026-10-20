"""Notification channels. Each one is a few lines: build a payload, POST it."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json as _json
import logging
import os
import platform
import shutil
import smtplib
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..http import HttpClient, HTTPError
from ..models import Alert, Severity
from ..relay.hub import RelayHub
from ..relay.messages_app import IMESSAGE_SCRIPTS, MessagesApp  # noqa: F401 - IMESSAGE_SCRIPTS re-exported
from ..util import strip_html
from .format import markdown_body, plain_body, short_title, sms_text, whatsapp_text

log = logging.getLogger(__name__)


class Notifier:
    name = "base"
    timeout = 20.0  # seconds the dispatcher waits for send()
    backup = False  # only used when every primary phone channel failed (option ``backup: true``)

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


def normalize_phone(value: str, default_country: str = "1") -> str:
    """'(555) 123-4567' -> '+15551234567'. E-mail handles (Apple IDs) pass through unchanged."""
    v = str(value).strip()
    if "@" in v:
        return v
    digits = "".join(c for c in v if c.isdigit())
    if v.startswith("+"):
        return "+" + digits
    if v.startswith("00"):
        return "+" + digits[2:]
    if len(digits) == 10 and default_country == "1":
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else v


def _numbers(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").split(",")
    return [normalize_phone(v) for v in raw if str(v).strip()]


class PhoneChannel:
    """Mixin for channels that text a phone number. The number can come from config (``to``)
    or be set at runtime from the dashboard's Setup page (``set_recipients``)."""

    recipients: list[str]

    def _init_recipients(self) -> None:
        self.recipients = _numbers(self.options.get("to"))  # type: ignore[attr-defined]

    def set_recipients(self, numbers: list[str]) -> None:
        self.recipients = _numbers(numbers)

    def targets(self) -> list[str]:
        if not self.recipients:
            raise RuntimeError("no phone number set: add IMESSAGE_TO or open the dashboard's Setup page")
        return self.recipients


# --------------------------------------------------------------------------- texts to your phone


class IMessageNotifier(PhoneChannel, Notifier):
    """iMessage via the Messages app when News247 itself runs on a Mac (free; the Mac must stay
    on and signed in). Monitor in the cloud instead? Use the ``relay`` channel.

    Options: ``to`` (phone number(s) like "+15551234567" or Apple ID e-mails),
    ``service``: "imessage" (default), "sms" (needs iPhone Text Message Forwarding to this Mac),
    or "auto" (iMessage, falling back to SMS).
    """

    name = "imessage"

    def validate(self) -> None:
        self._init_recipients()  # "+1555...,me@icloud.com" works too
        self.service = str(self.options.get("service", "imessage")).lower()
        if self.service not in ("imessage", "sms", "auto"):
            raise ValueError("notify.imessage: service must be imessage, sms or auto")
        self.app = MessagesApp()

    async def send(self, alert: Alert) -> None:
        if not self.app.supported():
            raise RuntimeError(
                "imessage needs macOS with Messages signed in (in the cloud use the relay channel)"
            )
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        for handle in self.targets():
            if self.service == "auto":
                try:
                    await self.app.send(handle, text, "imessage")
                except RuntimeError:
                    await self.app.send(handle, text, "sms")
            else:
                await self.app.send(handle, text, self.service)


class RelayNotifier(PhoneChannel, Notifier):
    """iMessage through your own Mac relay (``news247 relay``): the monitor runs anywhere, any
    Mac signed in to Messages does the sending over an outbound, authenticated WebSocket.

    Options: ``to``; ``secret`` (empty = generated and shown on the Setup page);
    ``accept_timeout`` (s, default 5: a relay that doesn't answer this fast is treated as asleep
    and backup channels take over); ``result_timeout`` (s, default 45); ``late_delivery``
    (default true: what couldn't be sent is delivered when the relay reconnects);
    ``max_late_minutes`` (default 60); ``digest_min`` (default 3: that many queued alerts are
    combined into one text).
    """

    name = "relay"

    def validate(self) -> None:
        self._init_recipients()
        o = self.options
        self.hub = RelayHub(
            str(o.get("secret") or ""),
            accept_timeout=float(o.get("accept_timeout", 5)),
            result_timeout=float(o.get("result_timeout", 45)),
            max_late_s=float(o.get("max_late_minutes", 60)) * 60,
            digest_min=int(o.get("digest_min", 3)),
            late_delivery=str(o.get("late_delivery", True)).strip().lower()
            not in ("false", "0", "no", "off"),
        )
        self.hub.set_recipients(self.recipients)
        self.timeout = self.hub.accept_timeout + self.hub.result_timeout + 5
        self._msg_ids: dict[str, str] = {}

    def set_recipients(self, numbers: list[str]) -> None:
        super().set_recipients(numbers)
        self.hub.set_recipients(self.recipients)

    async def send(self, alert: Alert) -> None:
        to = self.targets()
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        is_test = bool(alert.item and alert.item.source == "news247-test")
        rec = self.hub.new_message(
            alert.id, to, text, title=short_title(alert), severity=alert.severity.name, late_ok=not is_test
        )
        self._msg_ids[alert.id] = rec.id
        await self.hub.deliver(rec)

    def superseded(self, alert: Alert, by: str) -> None:
        msg_id = self._msg_ids.pop(alert.id, None)
        if msg_id:
            self.hub.supersede(msg_id, by)

    def describe(self) -> dict[str, Any]:
        st = self.hub.status()
        return {
            **super().describe(),
            "relay_online": st["online"],
            "relays": [r["name"] for r in st["relays"]],
            "queued": st["queued"],
        }


class TwilioSMSNotifier(PhoneChannel, Notifier):
    """Plain SMS via Twilio (green bubble). Options: ``account_sid``, ``auth_token``,
    ``from`` (your Twilio number or messaging_service_sid), ``to`` (number or list)."""

    name = "twilio"

    def validate(self) -> None:
        self.require("account_sid", "auth_token")
        if not (self.options.get("from") or self.options.get("messaging_service_sid")):
            raise ValueError("notify.twilio: missing from (or messaging_service_sid)")
        self._init_recipients()

    async def send(self, alert: Alert) -> None:
        import base64

        sid = self.options["account_sid"]
        auth = base64.b64encode(f"{sid}:{self.options['auth_token']}".encode()).decode()
        base = self.options.get("api_base", "https://api.twilio.com")
        for number in self.targets():
            data = {"To": number, "Body": sms_text(alert, limit=int(self.options.get("max_chars", 320)))}
            if self.options.get("messaging_service_sid"):
                data["MessagingServiceSid"] = self.options["messaging_service_sid"]
            else:
                data["From"] = str(self.options["from"])
            await self.http.post(
                f"{base}/2010-04-01/Accounts/{sid}/Messages.json",
                data=data,
                headers={"Authorization": f"Basic {auth}"},
            )


class BlueBubblesNotifier(PhoneChannel, Notifier):
    """iMessage through a BlueBubbles server (a free app on an always-on Mac). Lets the monitor
    itself run anywhere (cloud, Linux box) while the Mac only relays messages.
    Options: ``server`` (e.g. https://my-mac.example.com), ``password``, ``to``, ``method``
    ("apple-script" default, or "private-api")."""

    name = "bluebubbles"

    def validate(self) -> None:
        self.require("server", "password")
        self._init_recipients()

    async def send(self, alert: Alert) -> None:
        import uuid

        server = str(self.options["server"]).rstrip("/")
        params = {"password": self.options["password"]}  # kept out of the URL string we log
        method = self.options.get("method", "apple-script")
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        for handle in self.targets():
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


class SendblueNotifier(PhoneChannel, Notifier):
    """Hosted iMessage API (no Mac needed).

    Options: ``api_key_id``, ``api_secret``, ``to``, ``from_number`` (your Sendblue line; looked
    up automatically via /api/lines when omitted). On the free plan your number must first be a
    verified contact: text your Sendblue number once from your iPhone.
    Sendblue's docs use both api.sendblue.co and api.sendblue.com; both are tried.
    """

    name = "sendblue"
    HOSTS = ("https://api.sendblue.co", "https://api.sendblue.com")

    def validate(self) -> None:
        self.require("api_key_id", "api_secret")
        self._init_recipients()
        self.from_number: str = (
            normalize_phone(self.options["from_number"]) if self.options.get("from_number") else ""
        )
        base = self.options.get("api_base")
        self.hosts = [str(base).rstrip("/")] if base else list(self.HOSTS)

    @property
    def headers(self) -> dict[str, str]:
        return {"sb-api-key-id": self.options["api_key_id"], "sb-api-secret-key": self.options["api_secret"]}

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        """Try each API host; remember the first one that answers."""

        last: Exception | None = None
        for host in list(self.hosts):
            try:
                resp = await self.http.request(method, f"{host}{path}", headers=self.headers, **kw)
            except HTTPError as exc:
                if exc.status in (401, 403):
                    raise RuntimeError(
                        f"Sendblue rejected the API keys ({exc.status}): {exc.body[:200]}"
                    ) from None
                if exc.status not in (404, 405, 502, 503):
                    raise RuntimeError(f"Sendblue error {exc.status}: {exc.body[:300]}") from None
                last = exc
                continue
            except OSError as exc:  # DNS/connection failure: try the other host
                last = exc
                continue
            self.hosts = [host] + [h for h in self.hosts if h != host]
            try:
                return resp.json()
            except ValueError:
                return {}
        raise RuntimeError(f"Sendblue unreachable: {last}")

    async def lines(self) -> list[str]:
        data = await self._call("GET", "/api/lines")
        rows = data.get("lines") or data.get("data") or data if isinstance(data, (dict, list)) else []
        out = []
        for row in rows if isinstance(rows, list) else []:
            num = (
                row
                if isinstance(row, str)
                else (row.get("number") or row.get("phone_number") or row.get("line"))
            )
            if num:
                out.append(normalize_phone(num))
        return out

    async def ensure_from_number(self) -> str:
        if not self.from_number:
            with contextlib.suppress(RuntimeError):
                found = await self.lines()
                if found:
                    self.from_number = found[0]
        return self.from_number

    async def send(self, alert: Alert) -> None:
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        sender = await self.ensure_from_number()
        for number in self.targets():
            body: dict[str, Any] = {"number": number, "content": text}
            if sender:
                body["from_number"] = sender
            data = await self._call("POST", "/api/send-message", json=body)
            status = str((data or {}).get("status", "")).upper() if isinstance(data, dict) else ""
            if status in ("ERROR", "DECLINED", "FAILED"):
                hint = ""
                msg = str(data.get("error_message") or data.get("error") or data)
                if "verif" in msg.lower() or "contact" in msg.lower():
                    hint = " — text your Sendblue number once from your iPhone to verify it"
                raise RuntimeError(f"Sendblue {status.lower()}: {msg}{hint}")


class BlooioNotifier(PhoneChannel, Notifier):
    """Hosted iMessage API (no Mac needed). Options: ``api_key``, ``to``."""

    name = "blooio"

    def validate(self) -> None:
        self.require("api_key")
        self._init_recipients()

    async def send(self, alert: Alert) -> None:
        import uuid
        from urllib.parse import quote

        base = str(self.options.get("api_base", "https://backend.blooio.com")).rstrip("/")
        text = sms_text(alert, limit=int(self.options.get("max_chars", 900)))
        for number in self.targets():
            await self.http.post(
                f"{base}/v2/api/chats/{quote(number, safe='')}/messages",
                json={"text": text},
                headers={
                    "Authorization": f"Bearer {self.options['api_key']}",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
            )


class TextbeltNotifier(PhoneChannel, Notifier):
    """Plain SMS via Textbelt: prepaid key, no carrier registration paperwork.
    Options: ``key``, ``to``. Note: Textbelt may hold messages containing links until your
    account is verified, so ``include_url`` defaults to false."""

    name = "textbelt"

    def validate(self) -> None:
        self.require("key")
        self._init_recipients()

    async def send(self, alert: Alert) -> None:
        base = str(self.options.get("api_base", "https://textbelt.com")).rstrip("/")
        shown = alert if self.options.get("include_url", False) else _without_url(alert)
        text = sms_text(shown, limit=int(self.options.get("max_chars", 320)))
        for number in self.targets():
            resp = await self.http.post(
                f"{base}/text", data={"phone": number, "message": text, "key": self.options["key"]}
            )
            data = resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Textbelt: {data.get('error') or data}")


class WhatsAppNotifier(PhoneChannel, Notifier):
    """WhatsApp from a self-hosted linked device inside News247 (see docs/WHATSAPP.md).

    Pair once on the dashboard's Setup page (QR code, or an 8-character code entered in
    WhatsApp > Linked devices > Link with phone number). Options: ``to`` (your number);
    ``min_interval_s`` (spacing between messages, default 2); ``max_per_hour`` (default 60);
    ``session_dir`` (default <data_dir>/whatsapp: keep it on a persistent disk).
    """

    name = "whatsapp"
    timeout = 90.0  # a send may wait for a reconnect and for the spacing between messages
    snapshot: Any = None  # PostgresSnapshot when state_db is set

    def validate(self) -> None:
        from ..whatsapp.session import SessionConfig, WhatsAppSession

        self._init_recipients()
        o = self.options
        self.session = WhatsAppSession(
            None,
            SessionConfig(
                min_interval_s=float(o.get("min_interval_s", 2)),
                max_per_hour=int(o.get("max_per_hour", 60)),
            ),
        )
        self.session.recipients = self.recipients

    def attach(self, data_dir: Path) -> None:
        """Use the real WhatsApp engine, storing the pairing under ``data_dir``."""
        from ..whatsapp.neonize_backend import NeonizeBackend, available

        ok, why = available()
        if not ok:
            self.session.last_error = f"WhatsApp engine unavailable: {why}"
            log.error("whatsapp: %s (install with: pip install 'news247[whatsapp]')", why)
            return
        db = Path(self.options.get("session_dir") or Path(data_dir) / "whatsapp") / "session.db"
        self.session.set_backend_factory(lambda: NeonizeBackend(db))
        state_db = str(self.options.get("state_db") or "")
        if state_db:  # free hosts wipe the disk on restart: keep the pairing in Postgres
            from ..whatsapp.snapshot import PostgresSnapshot

            self.snapshot = PostgresSnapshot(
                state_db, db, interval_s=float(self.options.get("state_interval_s", 1800))
            )

    def set_recipients(self, numbers: list[str]) -> None:
        super().set_recipients(numbers)
        self.session.recipients = self.recipients

    async def send(self, alert: Alert) -> None:
        text = whatsapp_text(alert, limit=int(self.options.get("max_chars", 1500)))
        await self.session.send(self.targets(), text, title=short_title(alert))

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "state": self.session.state, "linked": self.session.me}


class WhatsAppCloudNotifier(PhoneChannel, Notifier):
    """WhatsApp through Meta's official Cloud API: free with Meta's test number, no ban risk.
    See news247/whatsapp/cloud.py for how the 24-hour window is handled.

    Options: ``token`` (permanent System User token), ``phone_number_id``, ``waba_id`` (lets
    News247 create and track its message template), ``app_secret`` (verifies webhook calls),
    ``verify_token`` (for the webhook handshake), ``to``; ``template`` (default news247_alert),
    ``template_lang`` (en_US), ``fallback_template`` (hello_world; "" to disable), ``api_version``.
    """

    name = "whatsapp_cloud"

    def validate(self) -> None:
        from ..whatsapp.cloud import CloudAPI

        self.require("token", "phone_number_id")
        self._init_recipients()
        o = self.options
        self.cloud = CloudAPI(
            self.http,
            token=str(o["token"]),
            phone_number_id=str(o["phone_number_id"]),
            waba_id=str(o.get("waba_id") or ""),
            app_secret=str(o.get("app_secret") or ""),
            verify_token=str(o.get("verify_token") or ""),
            template=str(o.get("template") or "news247_alert"),
            template_lang=str(o.get("template_lang") or "en_US"),
            fallback_template=str(o.get("fallback_template", "hello_world") or ""),
            api_version=str(o.get("api_version") or "v23.0"),
            api_base=str(o.get("api_base") or "https://graph.facebook.com"),
        )
        self.cloud.recipients = self.recipients

    def set_recipients(self, numbers: list[str]) -> None:
        super().set_recipients(numbers)
        self.cloud.recipients = self.recipients

    async def send(self, alert: Alert) -> None:
        full = whatsapp_text(alert, limit=int(self.options.get("max_chars", 3500)))
        for number in self.targets():
            await self.cloud.send_alert(number, full, short_title(alert))

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "template_status": self.cloud.template_status}


HELD_TEXT = {  # why an alert was kept back from the lock screen: the push log's wording
    "digest": "kept for your digest",
    "burst": "held back in a rush of alerts, folded into one summary",
}


class WebPushNotifier(Notifier):
    """Push notifications to the Foretape app on your phone (standard Web Push: free, no account,
    no phone number). Phones subscribe from the app (/app); alerts are encrypted for each phone
    and sent through its own push service (Apple, Google, Mozilla, Microsoft).

    Options: ``subject`` (contact for push services, mailto: or https:), ``ttl_s`` (how long a
    push service keeps an undelivered alert, default 1800), ``vapid_private`` (base64url key;
    default: derived from the dashboard token so it survives restarts)."""

    name = "webpush"
    timeout = 30.0

    def validate(self) -> None:
        from ..webpush import Subscription

        self.subs: dict[str, Subscription] = {}
        self.key: Any = None
        self.subject = str(self.options.get("subject") or "")
        self.storage: Any = None
        self.state: Any = None  # StateStore, when STATE_DB is set
        self.last_sent: float | None = None
        self.removed = 0
        self.allow: Any = None  # (subscription, alert) -> bool: each phone's desk preferences
        self.hold: Any = None  # (subscription, alert) -> bool: True when that phone's digest keeps it
        self.forward: Any = None  # (device, payload) -> None: that phone's own Discord/Telegram/ntfy copy
        self.why: Any = None  # (subscription, alert) -> str | None: why that phone's settings held it back
        self.filtered = 0
        # each phone's last pushes: what was sent, held for its digest or kept back (and why), and
        # what the push service answered; saved with the settings so it survives a restart
        self.log: dict[str, deque[dict[str, Any]]] = {}
        self.log_dirty = False
        self.stats: dict[str, dict[str, dict[str, int]]] = {}  # device -> day -> status -> count
        self.gone: dict[str, str] = {}  # endpoint -> device, for subscriptions dropped as expired

    # ------------------------------------------------------------------ setup

    def attach(self, storage: Any, *, secret: str = "", state: Any = None, subject: str = "") -> None:
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: I001

        from ..webpush import Subscription, derive_vapid_key, private_from_bytes, private_to_bytes, unb64u

        self.storage, self.state = storage, state
        self.subject = self.subject or subject or "mailto:news247@example.com"
        raw = str(self.options.get("vapid_private") or "")
        if raw:
            self.key = private_from_bytes(unb64u(raw))
        elif secret:
            self.key = derive_vapid_key(secret)
        else:  # local use without a token: a random key, remembered in the database
            saved = storage.get_setting("vapid_private")
            self.key = private_from_bytes(unb64u(saved)) if saved else ec.generate_private_key(ec.SECP256R1())
            if not saved:
                from ..webpush import b64u

                storage.set_setting("vapid_private", b64u(private_to_bytes(self.key)))
        for d in storage.push_all():
            try:
                sub = Subscription(**d)
                self.subs[sub.endpoint] = sub
            except TypeError:
                continue
        self.load_log()

    # ------------------------------------------------------------------ the push log

    LOG_SIZE = 40

    def note(
        self, device: str, payload: dict[str, Any], status: str, detail: str = "", ts: float | None = None
    ) -> None:
        """Remember what happened to one push for one phone (sent, failed, gone, held, filtered)."""
        if not device:
            return
        q = self.log.setdefault(device, deque(maxlen=self.LOG_SIZE))
        entry = {
            "ts": ts or time.time(),
            "id": str(payload.get("id") or "")[:60],
            "title": str(payload.get("title") or "")[:160],
            "status": status,
            "detail": detail[:200],
        }
        if q and q[0]["id"] == entry["id"] and q[0]["status"] == status:
            return  # one entry per push, however many browsers the phone has
        q.appendleft(entry)
        day = datetime.fromtimestamp(entry["ts"], ZoneInfo("America/New_York")).date().isoformat()
        days = self.stats.setdefault(device, {})
        days.setdefault(day, {})[status] = days.setdefault(day, {}).get(status, 0) + 1
        for old in sorted(days)[:-14]:  # two weeks of daily counts
            days.pop(old)
        self.log_dirty = True

    def note_results(self, payload: dict[str, Any], results: dict[str, str]) -> None:
        """Log a push's answers per phone: sent when any of that phone's subscriptions took it."""
        by_dev: dict[str, list[str]] = {}
        for endpoint, r in results.items():
            sub = self.subs.get(endpoint)
            dev = getattr(sub, "device", "") if sub is not None else self.gone.get(endpoint, "")
            if not dev and r == "gone":
                continue
            by_dev.setdefault(dev, []).append(r)
        for dev, rs in by_dev.items():
            if "ok" in rs:
                self.note(dev, payload, "sent")
            elif all(r == "gone" for r in rs):
                self.note(dev, payload, "gone", "the phone's subscription has expired: turn alerts on again")
            else:
                self.note(dev, payload, "failed", next(r for r in rs if r != "ok"))

    def load_log(self) -> None:
        if self.storage is None:
            return
        raw = self.storage.get_setting("push_log")
        if not raw:
            return
        try:
            data = _json.loads(raw)
        except ValueError:
            return
        if "log" in data and isinstance(data["log"], dict):  # {"log": ..., "stats": ...}
            self.stats = data.get("stats") or {}
            data = data["log"]
        self.log = {d: deque(rows[: self.LOG_SIZE], maxlen=self.LOG_SIZE) for d, rows in data.items()}

    def save_log(self) -> bool:
        if self.storage is None or not self.log_dirty:
            return False
        self.storage.set_setting(
            "push_log", _json.dumps({"log": {d: list(q) for d, q in self.log.items()}, "stats": self.stats})
        )
        self.log_dirty = False
        return True

    async def restore(self) -> None:
        """On a fresh container, take the subscriptions back from the state database."""
        import json as _json

        from ..webpush import Subscription

        if self.state is None or self.subs:
            return
        blob = await self.state.get("push-subscriptions")
        for d in _json.loads(blob or b"[]"):
            sub = Subscription(**d)
            self.subs[sub.endpoint] = sub
            self.storage.push_save(sub.to_dict())
        if self.subs:
            log.info("webpush: restored %d phone subscription(s) from the state database", len(self.subs))

    async def _persist(self) -> None:
        import json as _json

        if self.state is not None:
            await self.state.put(
                "push-subscriptions", _json.dumps([s.to_dict() for s in self.subs.values()]).encode()
            )

    @property
    def public_key(self) -> str:
        from ..webpush import b64u, public_bytes

        return b64u(public_bytes(self.key))

    async def subscribe(self, data: dict[str, Any], label: str = "", device: str = "") -> Any:
        from ..webpush import Subscription

        sub = Subscription.from_browser(data, label, device)
        old = self.subs.get(sub.endpoint)
        if old is not None:
            sub.created, sub.last_ok = old.created, old.last_ok
            sub.device = sub.device or old.device
        self.subs[sub.endpoint] = sub
        self.storage.push_save(sub.to_dict())
        if old is None or (old.p256dh, old.auth, old.device) != (sub.p256dh, sub.auth, sub.device):
            await self._persist()
        return sub

    async def unsubscribe(self, endpoint: str) -> bool:
        sub = self.subs.pop(endpoint, None)
        if sub is None:
            return False
        if sub.device:  # so the push log can still say whose subscription expired
            self.gone[endpoint] = sub.device
            while len(self.gone) > 200:
                self.gone.pop(next(iter(self.gone)))
        self.storage.push_delete(endpoint)
        await self._persist()
        return True

    # ------------------------------------------------------------------ sending

    def payload(self, alert: Alert) -> dict[str, Any]:
        from ..edge import EdgeDesk
        from .format import push_text

        body = push_text(alert)
        line = EdgeDesk.push_line(alert)
        if line and line not in body and line.split(" · ")[-1] not in body:
            body = f"{body}\n{line}" if body else line
        from ..catalysts import cat_line
        from ..dossier import base_line

        base = cat_line(alert.edge.get("cat_base")) or base_line(alert.edge.get("base"))
        rb = alert.edge.get("regime_base")
        if rb and alert.edge.get("cat_base"):
            base += f" · on {rb['regime']} days: {round(rb['hit'] * 100)}% ({rb['n']})"
        from ..odds import odds_line
        from ..plans import plan_line

        pl = plan_line(alert.edge.get("plan"), alert.edge.get("odds"))
        if pl:
            base = f"{pl}\n{base}" if base else pl
        ol = odds_line(alert.edge.get("odds"))
        if ol:
            base = f"{ol}\n{base}" if base else ol
        from ..flowrec import flow_line

        fw = flow_line(alert.edge.get("flow_base"))
        if fw:
            base = f"{base}\n{fw}" if base else fw
        from ..firms import firm_line

        fl = firm_line(alert.edge.get("firm_base"))
        if fl:
            base = f"{base}\n{fl}" if base else fl
        from ..persona import persona_line

        ps = alert.edge.get("persona")
        # only a trait that changes the call: "runs with its news" says what the record above says
        pn = persona_line(ps) if ps and ps.get("trait") not in ("mixed", "runs") else ""
        if pn:
            base = f"{base}\n{pn}" if base else pn
        from ..insiders import insider_line

        il = insider_line(alert.edge.get("insider_base"))
        if il:
            base = f"{base}\n{il}" if base else il
        from ..newsclock import clock_line
        from ..opening import open_line

        for extra in (clock_line(alert.edge.get("clock")), open_line(alert.edge.get("open_base"))):
            if extra:
                extra = extra if extra.startswith("⏱") else f"🔔 Into the open · {extra}"
                base = f"{base}\n{extra}" if base else extra
        from ..coverage import late_line

        lt = alert.edge.get("late") or {}
        ll = late_line(lt.get("min"), lt.get("source") or "", {"late:late": lt.get("rec")}) if lt else ""
        if ll:
            base = f"{ll}\n{base}" if base else ll
        call = alert.edge.get("call") or {}
        if call.get("line"):  # the call leads: what the rest adds up to
            base = f"{call['line']}\n{base}" if base else call["line"]
        if base:
            body = f"{body}\n{base}" if body else base
        play = alert.edge.get("play") or {}
        return {
            "id": alert.id,
            "title": short_title(alert)[:160],
            "body": body[:600],
            "direction": play.get("direction") or (alert.analysis.direction if alert.analysis else ""),
            "conviction": play.get("conviction", ""),
            "severity": alert.severity.name,
            "kind": alert.kind,
            "tickers": alert.tickers[:8],
            "smallcap": bool(alert.edge.get("smallcap")),
            "url": alert.url,
            "ts": alert.created,
            "tag": (alert.item.uid[:24] if alert.item else alert.id),
            "mute": alert.tickers[0]
            if alert.tickers and alert.kind != "system"
            else "",  # the notification's "Mute 4h" button
        }

    async def push(
        self, payload: dict[str, Any], *, urgency: str = "high", only: list[str] | None = None
    ) -> dict[str, str]:
        """Send one payload to every subscribed phone (or ``only`` these endpoints)."""
        from ..webpush import build_request

        results: dict[str, str] = {}
        dead: list[str] = []
        for sub in list(self.subs.values()):
            if only is not None and sub.endpoint not in only:
                continue
            body, headers = build_request(
                sub,
                payload,
                self.key,
                self.subject,
                ttl_s=int(self.options.get("ttl_s", 1800)),
                urgency=urgency,
                topic=str(payload.get("tag", "")),
            )
            try:
                await self.http.post(sub.endpoint, data=body, headers=headers, timeout_s=15)
                sub.last_ok, sub.failures = time.time(), 0
                results[sub.endpoint] = "ok"
            except HTTPError as exc:
                sub.failures += 1
                if exc.status in (404, 410):  # the phone unsubscribed or the app was deleted
                    dead.append(sub.endpoint)
                    results[sub.endpoint] = "gone"
                else:
                    results[sub.endpoint] = f"HTTP {exc.status}: {strip_html(exc.body, limit=160)}"
            except Exception as exc:  # noqa: BLE001 - one phone failing must not stop the others
                sub.failures += 1
                results[sub.endpoint] = f"{type(exc).__name__}: {exc}"[:200]
        for endpoint in dead:
            self.removed += 1
            await self.unsubscribe(endpoint)
        return results

    async def send(self, alert: Alert) -> None:
        if not self.subs:
            raise RuntimeError(
                "no phone has turned on alerts yet: open /app on your phone and tap “Turn on alerts”"
            )
        urgency = "high" if alert.severity >= Severity.HIGH else "normal"
        only = None
        payload = self.payload(alert)
        if self.allow is not None:  # each phone's own preferences (desk.py): categories, quiet hours
            only = []
            for s in self.subs.values():
                if not self.allow(s, alert):
                    why = self.why(s, alert) if self.why is not None else None
                    self.note(s.device, payload, "filtered", why or "your settings")
                elif self.hold is not None and (held := self.hold(s, alert)):  # kept for a summary
                    self.note(s.device, payload, "held", HELD_TEXT.get(held, HELD_TEXT["digest"]))
                else:
                    only.append(s.endpoint)
            self.filtered += len(self.subs) - len(only)
            if not only:
                self.last_sent = time.time()
                return
        results = await self.push(payload, urgency=urgency, only=only)
        self.note_results(payload, results)
        if self.forward is not None:
            for device in {
                s.device for s in self.subs.values() if (only is None or s.endpoint in only) and s.device
            }:
                self.forward(device, payload)
        if results and all(r != "ok" for r in results.values()):
            raise RuntimeError("push failed: " + "; ".join(sorted(set(results.values())))[:300])
        self.last_sent = time.time()

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "devices": [
                {
                    "service": s.service,
                    "label": s.label,
                    "since": s.created,
                    "last_ok": s.last_ok,
                    "failures": s.failures,
                }
                for s in self.subs.values()
            ],
            "durable": self.state is not None,
        }


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
        RelayNotifier,
        TwilioSMSNotifier,
        BlueBubblesNotifier,
        SendblueNotifier,
        BlooioNotifier,
        TextbeltNotifier,
        WhatsAppNotifier,
        WhatsAppCloudNotifier,
        WebPushNotifier,
    )
}
