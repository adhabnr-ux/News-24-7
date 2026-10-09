"""WhatsApp through Meta's official Cloud API: free with Meta's test number, no ban risk.

Meta lets a business send *free-form* messages only within 24 hours of the person's last message
to it (the "customer service window"); outside it only pre-approved *templates* are delivered.
This client makes that invisible:

* inside the window, alerts go out in full;
* outside it, they go out as the approved ``news247_alert`` template (headline squeezed onto
  one line) with a **Show details** button: tapping it reopens the window and News247 replies
  with the full text of every alert you haven't seen in full;
* the template is created through the API on first start (needs the WhatsApp Business Account
  id) and its approval is tracked; until it's approved, Meta's pre-approved ``hello_world`` is
  sent as a ping instead and the alert waits for your reply;
* a webhook receives your replies (commands like "pause 2h", "status") and delivery/read
  receipts, verified with Meta's signature when the app secret is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from ..http import HttpClient, HTTPError
from .session import digits

log = logging.getLogger(__name__)

WINDOW_S = 24 * 3600
WINDOW_MARGIN_S = 30 * 60  # treat the window as closed a little early (clock skew, slow sends)
REENGAGE = 131047  # "more than 24 hours have passed since the recipient last replied"
LOCKED = 131031  # "Business Account locked": Meta restricted the WhatsApp Business Account
LOCKED_HINT = (
    " — Meta locked the WhatsApp Business Account. The Setup page shows Meta's health check with the "
    "exact reason; usual fix: complete Business info (legal name, address, website), publish the app, "
    "and request a review (docs/FREE-SETUP.md#if-meta-says-business-account-locked-131031)"
)
DETAILS_BUTTON = "Show details"
DETAILS_WORDS = {"details", "show details", "more details", "full", "show", "alerts", "what", "what happened"}
TEMPLATE_BODY = "📈 News247 alert: {{1}} — tap below for the full details."
TEMPLATE_EXAMPLE = "🔴 OpenAI launches enterprise agents · ▼ CRM INTU NOW · x, 3s after post"
# If Meta rejects a wording, the next one is submitted automatically (different names, because a
# rejected name can't be resubmitted unchanged). Variables never start or end the body (Meta rule).
TEMPLATE_VARIANTS = [
    {"suffix": "", "body": TEMPLATE_BODY, "button": True},
    {
        "suffix": "_v2",
        "body": "News247 market update: {{1}}. Reply to this chat any time for more.",
        "button": False,
    },
    {
        "suffix": "_v3",
        "body": "Your requested News247 alert: {{1}}. Reply STOP to pause alerts.",
        "button": False,
    },
]


class CloudError(RuntimeError):
    def __init__(self, code: int | None, message: str) -> None:
        if code == LOCKED and LOCKED_HINT not in message:
            message += LOCKED_HINT
        super().__init__(
            f"WhatsApp Cloud API error {code}: {message}" if code else f"WhatsApp Cloud API: {message}"
        )
        self.code = code
        self.detail = message


def one_line(text: str, limit: int = 900) -> str:
    """Template parameters can't contain newlines, tabs or 4+ spaces in a row."""
    parts = [p.strip().replace("\t", " ") for p in text.replace("*", "").splitlines() if p.strip()]
    line = " · ".join(parts)
    while "    " in line:
        line = line.replace("    ", "   ")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


class CloudAPI:
    def __init__(
        self,
        http: HttpClient,
        *,
        token: str,
        phone_number_id: str,
        waba_id: str = "",
        app_secret: str = "",
        verify_token: str = "",
        template: str = "news247_alert",
        template_lang: str = "en_US",
        fallback_template: str = "hello_world",
        api_version: str = "v23.0",
        api_base: str = "https://graph.facebook.com",
    ) -> None:
        self.http = http
        self.token = token
        self.phone_number_id = str(phone_number_id)
        self.waba_id = str(waba_id or "")
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.template = template  # the name in use (base name, or a fallback variant)
        self.template_base = template
        self.template_button = True
        self.template_lang = template_lang
        self.fallback_template = fallback_template
        self.base = f"{api_base.rstrip('/')}/{api_version}"
        self.recipients: list[str] = []
        self.on_inbound: Callable[[str, str], Awaitable[str | None]] | None = None
        self.phone: dict[str, Any] = {}
        self.health: dict[str, Any] = {}  # Meta's health_status: can the number send, and if not, why
        self.account: dict[str, Any] = {}
        self.template_status = ""  # APPROVED | PENDING | REJECTED | PAUSED | DISABLED | missing | ""
        self.template_note = ""
        self.last_error = ""
        self.last_inbound: dict[str, float] = {}
        self.last_webhook_at: float | None = None
        self.messages: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.pending: dict[str, deque[tuple[float, str, str]]] = {}
        self.delivery_s: deque[float] = deque(maxlen=50)
        self.stats = {
            "text": 0,
            "template": 0,
            "ping": 0,
            "delivered": 0,
            "read": 0,
            "failed": 0,
            "inbound": 0,
        }
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ Graph API

    async def _graph(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            resp = await self.http.request(method, f"{self.base}/{path.lstrip('/')}", headers=headers, **kw)
        except HTTPError as exc:
            try:
                err = json.loads(exc.body).get("error", {})
                detail = (err.get("error_data") or {}).get("details") or err.get("message") or exc.body[:200]
                raise CloudError(err.get("code") or exc.status, detail) from None
            except (ValueError, AttributeError):
                raise CloudError(exc.status, (exc.body or "")[:200]) from None
        return resp.json() if resp.body else {}

    async def refresh_phone(self) -> None:
        self.phone = await self._graph(
            "GET",
            self.phone_number_id,
            params={"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"},
        )

    async def refresh_health(self) -> dict[str, Any]:
        """Meta's own diagnosis (GET <phone>?fields=health_status): per node (app, business, WABA,
        number) whether it can send, and if not the error, its description and a possible fix."""
        data = await self._graph("GET", self.phone_number_id, params={"fields": "health_status"})
        hs = data.get("health_status") or {}
        problems = []
        for ent in hs.get("entities") or []:
            for err in ent.get("errors") or []:
                problems.append(
                    {
                        "entity": ent.get("entity_type", ""),
                        "status": ent.get("can_send_message", ""),
                        "code": err.get("error_code"),
                        "description": err.get("error_description", ""),
                        "solution": err.get("possible_solution", ""),
                    }
                )
            for info in ent.get("additional_info") or []:
                problems.append(
                    {
                        "entity": ent.get("entity_type", ""),
                        "status": ent.get("can_send_message", ""),
                        "code": None,
                        "description": str(info),
                        "solution": "",
                    }
                )
        self.health = {
            "can_send_message": hs.get("can_send_message", ""),
            "problems": problems,
            "checked": time.time(),
        }
        if self.waba_id:
            with contextlib.suppress(CloudError):
                self.account = await self._graph(
                    "GET", self.waba_id, params={"fields": "name,account_review_status"}
                )
        return self.health

    async def ensure_template(self) -> str:
        """Find an approved alert template, or submit one; return the review status.

        Tries the wordings in TEMPLATE_VARIANTS in order: an approved one is used at once, a
        pending one is waited for, and when Meta has rejected every existing one the next
        wording is submitted."""
        if not self.waba_id:
            self.template_status, self.template_note = (
                "",
                "set WHATSAPP_CLOUD_WABA_ID so News247 can create it",
            )
            return self.template_status
        data = await self._graph(
            "GET",
            f"{self.waba_id}/message_templates",
            params={"fields": "name,status,language,category,rejected_reason", "limit": "200"},
        )
        mine = {
            t.get("name"): t
            for t in data.get("data", [])
            if t.get("language") == self.template_lang
            and str(t.get("name", "")).startswith(self.template_base)
        }
        variants = [(self.template_base + v["suffix"], v) for v in TEMPLATE_VARIANTS]

        def use(name: str, variant: dict[str, Any], status: str, note: str = "") -> str:
            self.template, self.template_button = name, bool(variant["button"])
            self.template_status, self.template_note = status, note
            return status

        for name, v in variants:
            if str(mine.get(name, {}).get("status", "")).upper() == "APPROVED":
                return use(name, v, "APPROVED")
        for name, v in variants:
            if str(mine.get(name, {}).get("status", "")).upper() in ("PENDING", "IN_APPEAL"):
                return use(name, v, "PENDING", "in Meta's review (usually minutes)")
        refused = [
            f"{n}: {str(t.get('status', '')).lower()} {t.get('rejected_reason') or ''}".strip()
            for n, t in mine.items()
        ]
        for name, v in variants:
            if name in mine:
                continue
            components: list[dict[str, Any]] = [
                {"type": "BODY", "text": v["body"], "example": {"body_text": [[TEMPLATE_EXAMPLE]]}}
            ]
            if v["button"]:
                components.append(
                    {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": DETAILS_BUTTON}]}
                )
            created = await self._graph(
                "POST",
                f"{self.waba_id}/message_templates",
                json={
                    "name": name,
                    "language": self.template_lang,
                    "category": "UTILITY",
                    "components": components,
                },
            )
            status = str(created.get("status") or "PENDING").upper()
            note = "submitted; Meta usually reviews utility templates within minutes"
            if refused:
                note += " (earlier wording refused: " + "; ".join(refused) + ")"
            log.info("WhatsApp template %s submitted (%s)", name, status)
            return use(
                name, v, status if status != "APPROVED" else "APPROVED", note if status != "APPROVED" else ""
            )
        name, v = variants[0]
        return use(name, v, "REJECTED", "Meta refused every wording: " + "; ".join(refused))

    async def maintain(self, stop: asyncio.Event) -> None:
        """Check the number and template at start, and keep checking until the template is approved."""
        while not stop.is_set():
            try:
                await self.refresh_phone()
                try:  # diagnostics only: never blocks sending, but say why it's missing
                    await self.refresh_health()
                except CloudError as exc:
                    self.health = {
                        "can_send_message": "",
                        "problems": [],
                        "error": str(exc)[:300],
                        "checked": time.time(),
                    }
                await self.ensure_template()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - shown on the Setup page, retried
                self.last_error = str(exc)[:600]
                log.warning("WhatsApp Cloud API check failed: %s", self.last_error)
            if self.health.get("can_send_message") == "BLOCKED":
                self.last_error = (
                    self.last_error or "Meta reports this number can't send messages (see the health check)"
                )
            wait = (
                120
                if (self.template_status in ("PENDING", "") and self.waba_id) or self.last_error
                else 6 * 3600
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=wait)

    # ------------------------------------------------------------------ sending

    def window_open(self, number: str) -> bool:
        last = self.last_inbound.get(digits(number))
        return bool(last) and time.time() - last < WINDOW_S - WINDOW_MARGIN_S  # type: ignore[operator]

    def window_closes(self, number: str) -> float | None:
        last = self.last_inbound.get(digits(number))
        return last + WINDOW_S if last else None

    def _record(self, resp: dict[str, Any], to: str, title: str, kind: str) -> str:
        wamid = str((resp.get("messages") or [{}])[0].get("id", f"local-{time.time()}"))
        self.messages[wamid] = {
            "id": wamid,
            "to": to,
            "title": title,
            "kind": kind,
            "status": "sent",
            "sent_at": time.time(),
            "delivered_at": None,
            "read_at": None,
            "error": "",
        }
        while len(self.messages) > 300:
            self.messages.popitem(last=False)
        self.stats[kind] += 1
        return wamid

    async def send_text(self, to: str, text: str, title: str = "", full_text: str = "") -> str:
        d = digits(to)
        resp = await self._graph(
            "POST",
            f"{self.phone_number_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": d,
                "type": "text",
                "text": {"preview_url": False, "body": text[:4096]},
            },
        )
        wamid = self._record(resp, d, title or text.split("\n", 1)[0], "text")
        if full_text:  # an alert: if Meta later reports 131047, it is re-sent as a template/ping
            self.messages[wamid]["alert"] = full_text
        return wamid

    async def send_template(
        self, to: str, name: str, param: str | None, title: str, button: bool, kind: str = "template"
    ) -> str:
        d = digits(to)
        tpl: dict[str, Any] = {
            "name": name,
            "language": {"code": "en_US" if name == self.fallback_template else self.template_lang},
        }
        components: list[dict[str, Any]] = []
        if param is not None:
            components.append({"type": "body", "parameters": [{"type": "text", "text": param}]})
        if button:
            components.append(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "0",
                    "parameters": [{"type": "payload", "payload": "details"}],
                }
            )
        if components:
            tpl["components"] = components
        resp = await self._graph(
            "POST",
            f"{self.phone_number_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": d,
                "type": "template",
                "template": tpl,
            },
        )
        return self._record(resp, d, title, kind)

    def _remember(self, to: str, full: str, title: str) -> None:
        self.pending.setdefault(digits(to), deque(maxlen=20)).append((time.time(), full, title))

    async def send_alert(self, to: str, full: str, title: str) -> str:
        """Deliver one alert in the best form Meta currently allows."""
        try:
            wamid = await self._send_alert(to, full, title)
            if self.last_error.startswith("WhatsApp Cloud API error"):
                self.last_error = ""  # a send worked again
            return wamid
        except CloudError as exc:
            if exc.code == LOCKED:  # fetch Meta's reason now so the Setup page can show it
                task = asyncio.ensure_future(self.refresh_health())
                self._tasks.add(task)
                task.add_done_callback(lambda t: (self._tasks.discard(t), t.cancelled() or t.exception()))
            self.last_error = str(exc)[:600]
            raise

    async def _send_alert(self, to: str, full: str, title: str) -> str:
        d = digits(to)
        # Free-form only when we *know* the window is open (we saw your reply through the webhook).
        # Otherwise Meta often accepts the message and only later reports 131047, so the alert would
        # silently never arrive; an approved template (or Meta's hello_world ping) always arrives.
        if self.window_open(d):
            try:
                return await self.send_text(d, full, title, full_text=full)
            except CloudError as exc:
                if exc.code != REENGAGE:
                    raise
                self.last_inbound.pop(d, None)  # the window is closed
        if self.template_status == "APPROVED":
            wamid = await self.send_template(
                d, self.template, one_line(full), title, button=self.template_button
            )
            self._remember(d, full, title)
            return wamid
        if self.fallback_template:  # template still in review: ping, and keep the alert for your reply
            wamid = await self.send_template(
                d, self.fallback_template, None, f"ping for: {title}", button=False, kind="ping"
            )
            self._remember(d, full, title)
            return wamid
        raise CloudError(
            REENGAGE,
            "outside the 24-hour window and the alert template isn't approved yet: reply anything to the News247 number",
        )

    async def _resend(self, to: str, full: str, title: str) -> None:
        try:
            await self._send_alert(to, full, title)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"re-sending after 131047 failed: {exc}"[:600]
            log.warning("WhatsApp: %s", self.last_error)

    async def deliver_pending(self, to: str) -> bool:
        d = digits(to)
        items = list(self.pending.pop(d, []))
        if not items:
            return False
        now = time.time()
        if len(items) == 1:
            text = items[0][1]
        else:
            parts = [f"📬 {len(items)} alerts you haven't seen in full:"]
            for ts, full, _ in items:
                mins = max(1, int((now - ts) / 60))
                parts.append(f"⏱ {mins}m ago\n{full}")
            text = "\n\n".join(parts)
        await self.send_text(d, text[:4096], f"📬 details of {len(items)} alert(s)")
        return True

    # ------------------------------------------------------------------ webhook

    def verify_subscription(self, query: dict[str, str]) -> str | None:
        """Meta's one-time GET handshake: echo hub.challenge if the verify token matches."""
        ok = (
            query.get("hub.mode") == "subscribe"
            and bool(self.verify_token)
            and hmac.compare_digest(
                str(query.get("hub.verify_token", "")).encode(), self.verify_token.encode()
            )
        )
        return query.get("hub.challenge", "") if ok else None

    def signature_ok(self, raw: bytes, header: str | None) -> bool:
        if not self.app_secret:
            return True  # not configured: accepted (the Setup page warns)
        want = "sha256=" + hmac.new(self.app_secret.encode(), raw, hashlib.sha256).hexdigest()
        return bool(header) and hmac.compare_digest(want.encode(), str(header).encode())

    async def handle_webhook(self, payload: dict[str, Any]) -> None:
        self.last_webhook_at = time.time()
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                meta = value.get("metadata") or {}
                if meta.get("phone_number_id") and str(meta["phone_number_id"]) != self.phone_number_id:
                    continue
                for status in value.get("statuses") or []:
                    self._on_status(status)
                for msg in value.get("messages") or []:
                    await self._on_message(msg)

    def _on_status(self, s: dict[str, Any]) -> None:
        rec = self.messages.get(str(s.get("id", "")))
        if rec is None:
            return
        state = s.get("status")
        ts = float(s.get("timestamp") or time.time())
        if state == "delivered" and rec["status"] == "sent":
            rec["status"], rec["delivered_at"] = "delivered", ts
            self.delivery_s.append(max(0.0, ts - rec["sent_at"]))
            self.stats["delivered"] += 1
        elif state == "read":
            if rec["delivered_at"] is None:
                rec["delivered_at"] = ts
                self.delivery_s.append(max(0.0, ts - rec["sent_at"]))
                self.stats["delivered"] += 1
            rec["status"], rec["read_at"] = "read", ts
            self.stats["read"] += 1
        elif state == "failed":
            errs = s.get("errors") or [{}]
            rec["status"] = "failed"
            rec["error"] = (
                f"{errs[0].get('code', '')} {errs[0].get('title') or errs[0].get('message', '')}".strip()
            )
            self.stats["failed"] += 1
            log.warning("WhatsApp message %s failed: %s", rec["id"], rec["error"])
            if errs[0].get("code") == REENGAGE:
                self.last_inbound.pop(rec["to"], None)  # the window was closed after all
                if rec.get("alert") and not rec.get("resent"):
                    rec["resent"] = True
                    task = asyncio.ensure_future(self._resend(rec["to"], rec["alert"], rec["title"]))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def _on_message(self, m: dict[str, Any]) -> None:
        sender = digits(str(m.get("from", "")))
        if sender not in {digits(r) for r in self.recipients}:
            log.info("WhatsApp: ignoring a message from +%s (not an alert recipient)", sender)
            return
        self.last_inbound[sender] = float(m.get("timestamp") or time.time())  # the window is open again
        self.stats["inbound"] += 1
        kind = m.get("type")
        if kind == "button":
            text, payload = (
                str((m.get("button") or {}).get("text", "")),
                str((m.get("button") or {}).get("payload", "")),
            )
        elif kind == "interactive":
            reply = (m.get("interactive") or {}).get("button_reply") or {}
            text, payload = str(reply.get("title", "")), str(reply.get("id", ""))
        else:
            text, payload = str((m.get("text") or {}).get("body", "")), ""
        try:
            if payload.startswith("details") or text.strip().lower().strip("!.?") in DETAILS_WORDS:
                if not await self.deliver_pending(sender):
                    await self.send_text(sender, "✅ No alerts waiting. You're up to date.", "↩ up to date")
                return
            answer = await self.on_inbound(sender, text) if self.on_inbound and text else None
            if answer:
                await self.send_text(sender, answer, f"↩ reply to “{text[:40]}”")
            await self.deliver_pending(sender)  # any reply opens the window: catch up
        except Exception as exc:  # noqa: BLE001 - a failed reply must not break the webhook
            log.warning("WhatsApp reply failed: %s", exc)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        lat = sorted(self.delivery_s)
        windows = {f"+{d}": self.window_closes(d) for d in (digits(r) for r in self.recipients)}
        return {
            "phone": self.phone,
            "health": self.health,
            "account": self.account,
            "template": self.template,
            "template_status": self.template_status,
            "template_note": self.template_note,
            "last_error": self.last_error,
            "webhook_seen": self.last_webhook_at,
            "signature_check": bool(self.app_secret),
            "verify_token_set": bool(self.verify_token),
            "window_closes": windows,
            "pending": sum(len(q) for q in self.pending.values()),
            "stats": dict(self.stats),
            "delivery_median_s": lat[len(lat) // 2] if lat else None,
            "recent": list(self.messages.values())[-10:][::-1],
        }
