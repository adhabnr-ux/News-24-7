"""WhatsApp via Meta's Cloud API (free test number): templates, the 24-hour window, the details
button, webhooks (verify, signature, replies, receipts), commands, and the keep-alive ping."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from news247.config import build_config
from news247.engine import Engine
from news247.models import Alert, Analysis, NewsItem, Severity, SourceTier
from news247.storage import Storage
from news247.web.server import WebServer
from news247.whatsapp.cloud import REENGAGE, TEMPLATE_BODY, CloudAPI, CloudError, one_line

ME = "+16235550146"
PHONE_ID, WABA, TOKEN = "1098", "5577", "EAAtoken"


class FakeMeta:
    """Just enough of the Graph API to behave like Meta: windows, templates, errors."""

    def __init__(self) -> None:
        self.templates: dict[str, dict[str, Any]] = {}
        self.window_open: set[str] = set()
        self.sent: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.next_id = 0
        self.fail_code: int | None = None
        self.server: TestServer | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v23.0/{node}", self.node)
        app.router.add_get("/v23.0/{waba}/message_templates", self.list_templates)
        app.router.add_post("/v23.0/{waba}/message_templates", self.create_template)
        app.router.add_post("/v23.0/{phone}/messages", self.messages)
        return app

    @staticmethod
    def error(code: int, message: str, status: int = 400) -> web.Response:
        return web.json_response({"error": {"code": code, "message": message}}, status=status)

    def authed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def node(self, request: web.Request) -> web.Response:
        if not self.authed(request):
            return self.error(190, "Invalid OAuth access token", 401)
        return web.json_response(
            {
                "display_phone_number": "15551234000",
                "verified_name": "Test Number",
                "quality_rating": "GREEN",
                "id": PHONE_ID,
            }
        )

    async def list_templates(self, request: web.Request) -> web.Response:
        name = request.query.get("name")
        return web.json_response({"data": [t for t in self.templates.values() if name in (None, t["name"])]})

    async def create_template(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.created.append(body)
        self.templates[body["name"]] = {
            "name": body["name"],
            "language": body["language"],
            "status": "PENDING",
            "id": "t1",
        }
        return web.json_response({"id": "t1", "status": "PENDING", "category": "UTILITY"})

    async def messages(self, request: web.Request) -> web.Response:
        if not self.authed(request):
            return self.error(190, "Invalid OAuth access token", 401)
        body = await request.json()
        if self.fail_code:
            return self.error(self.fail_code, "Recipient phone number not in allowed list")
        if body["type"] == "text" and body["to"] not in self.window_open:
            return self.error(REENGAGE, "Re-engagement message")
        if body["type"] == "template":
            name = body["template"]["name"]
            if name != "hello_world" and self.templates.get(name, {}).get("status") != "APPROVED":
                return self.error(132001, "Template name does not exist in the translation")
        self.next_id += 1
        self.sent.append(body)
        return web.json_response({"messages": [{"id": f"wamid.{self.next_id}"}]})


@pytest.fixture
async def meta():
    fake = FakeMeta()
    fake.server = TestServer(fake.app())
    await fake.server.start_server()
    yield fake
    await fake.server.close()


def make_cloud(meta: FakeMeta, http: Any, **kw: Any) -> CloudAPI:
    c = CloudAPI(
        http,
        token=TOKEN,
        phone_number_id=PHONE_ID,
        waba_id=WABA,
        api_base=str(meta.server.make_url("")),
        **kw,
    )
    c.recipients = [ME]
    return c


def inbound(text: str = "", button: str = "", sender: str = "16235550146") -> dict[str, Any]:
    msg: dict[str, Any] = {"from": sender, "id": f"wamid.in{time.time()}", "timestamp": str(int(time.time()))}
    if button:
        msg.update(type="button", button={"payload": button, "text": "Show details"})
    else:
        msg.update(type="text", text={"body": text})
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"metadata": {"phone_number_id": PHONE_ID}, "messages": [msg]},
                    }
                ]
            }
        ],
    }


def statuses(wamid: str, *states: str) -> dict[str, Any]:
    items = [
        {"id": wamid, "status": s, "timestamp": str(int(time.time())), "recipient_id": "16235550146"}
        for s in states
    ]
    return {
        "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": PHONE_ID}, "statuses": items}}]}]
    }


FULL = "*🔴 OPENAI LAUNCHES ENTERPRISE AGENTS*\n▼ CRM INTU · x · 3s after post\nAI agents vs SaaS\nhttps://x.com/1"


# --------------------------------------------------------------------------- templates


async def test_template_is_created_once_and_tracked(meta, http):
    c = make_cloud(meta, http)
    assert await c.ensure_template() == "PENDING"
    (tpl,) = meta.created
    assert tpl["category"] == "UTILITY" and tpl["language"] == "en_US"
    body, buttons = tpl["components"]
    # Meta rejects templates that start or end with a variable
    assert (
        body["text"] == TEMPLATE_BODY
        and not body["text"].startswith("{{")
        and not body["text"].rstrip(".").endswith("}}")
    )
    assert body["example"]["body_text"][0][0] and buttons["buttons"] == [
        {"type": "QUICK_REPLY", "text": "Show details"}
    ]
    meta.templates["news247_alert"]["status"] = "APPROVED"
    assert await c.ensure_template() == "APPROVED" and len(meta.created) == 1
    await c.refresh_phone()
    assert c.phone["verified_name"] == "Test Number"


async def test_no_waba_id_means_no_template_management(meta, http):
    c = CloudAPI(http, token=TOKEN, phone_number_id=PHONE_ID, api_base=str(meta.server.make_url("")))
    assert await c.ensure_template() == "" and "WABA" in c.template_note


# --------------------------------------------------------------------------- the 24-hour window


async def test_pending_template_pings_then_details_on_reply(meta, http):
    c = make_cloud(meta, http)
    await c.ensure_template()  # PENDING
    await c.send_alert(ME, FULL, "🔴 OPENAI LAUNCHES")
    assert meta.sent[-1]["template"] == {"name": "hello_world", "language": {"code": "en_US"}}  # a ping
    assert c.status()["pending"] == 1
    # you reply "hi": the window opens and the alert arrives in full
    meta.window_open.add("16235550146")
    await c.handle_webhook(inbound("hi"))
    assert meta.sent[-1]["type"] == "text" and meta.sent[-1]["text"]["body"] == FULL
    assert c.status()["pending"] == 0 and c.window_open(ME)
    # inside the window the next alert goes out in full straight away
    await c.send_alert(ME, "second alert", "second")
    assert meta.sent[-1]["text"]["body"] == "second alert"


async def test_approved_template_with_details_button(meta, http):
    c = make_cloud(meta, http)
    await c.ensure_template()
    meta.templates["news247_alert"]["status"] = "APPROVED"
    await c.ensure_template()
    await c.send_alert(ME, FULL, "t1")
    await c.send_alert(ME, "*🟠 Fed cuts rates*\nFed · 2s after post", "t2")
    tpl = meta.sent[-1]["template"]
    assert tpl["name"] == "news247_alert"
    body, button = tpl["components"]
    assert body["parameters"][0]["text"] == "🟠 Fed cuts rates · Fed · 2s after post"
    assert button == {
        "type": "button",
        "sub_type": "quick_reply",
        "index": "0",
        "parameters": [{"type": "payload", "payload": "details"}],
    }
    # tapping "Show details" opens the window; both alerts arrive in full, in one message
    meta.window_open.add("16235550146")
    await c.handle_webhook(inbound(button="details"))
    digest = meta.sent[-1]["text"]["body"]
    assert (
        digest.startswith("📬 2 alerts you haven't seen in full:")
        and "OPENAI LAUNCHES" in digest
        and "Fed cuts rates" in digest
    )
    await c.handle_webhook(inbound("details"))
    assert meta.sent[-1]["text"]["body"].startswith("✅ No alerts waiting")


async def test_window_closing_soon_counts_as_closed(meta, http):
    c = make_cloud(meta, http)
    meta.templates["news247_alert"] = {"name": "news247_alert", "language": "en_US", "status": "APPROVED"}
    await c.ensure_template()
    c.last_inbound["16235550146"] = time.time() - 23.8 * 3600  # 12 minutes left: too risky
    await c.send_alert(ME, FULL, "t")
    assert meta.sent[-1]["type"] == "template"


async def test_unknown_window_never_risks_free_form(meta, http):
    """After a restart (or with an unpublished app, whose webhooks never reach us) News247 can't
    know whether the window is open, so it never bets on free-form text that Meta could silently
    drop: approved template if there is one, else Meta's hello_world ping + the alert kept."""
    c = make_cloud(meta, http)  # fresh process: no reply seen
    meta.window_open.add("16235550146")  # (even if Meta would accept text right now)
    await c.send_alert(ME, FULL, "t")
    assert meta.sent[-1]["type"] == "template" and meta.sent[-1]["template"]["name"] == "hello_world"
    assert c.status()["pending"] == 1
    meta.templates["news247_alert"] = {"name": "news247_alert", "language": "en_US", "status": "APPROVED"}
    await c.ensure_template()
    await c.send_alert(ME, FULL, "t2")
    assert meta.sent[-1]["template"]["name"] == "news247_alert"


async def test_late_131047_from_webhook_is_resent(meta, http):
    """Meta may accept a text and report 131047 minutes later through the webhook: the window is
    marked closed and the same alert is re-sent in a form Meta delivers."""
    c = make_cloud(meta, http)
    c.last_inbound["16235550146"] = time.time() - 3600  # we believe the window is open
    meta.window_open.add("16235550146")  # Meta accepts the request...
    wamid = await c.send_alert(ME, FULL, "🔴 OPENAI LAUNCHES")
    assert meta.sent[-1]["type"] == "text"
    failed = {"id": wamid, "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]}
    await c.handle_webhook({"entry": [{"changes": [{"value": {"statuses": [failed]}}]}]})  # ...then fails it
    for _ in range(50):
        if meta.sent[-1]["type"] == "template":
            break
        await asyncio.sleep(0.01)
    assert meta.sent[-1]["template"]["name"] == "hello_world" and not c.window_open(ME)
    assert c.status()["pending"] == 1  # the full text waits for your reply
    await c.handle_webhook(
        {"entry": [{"changes": [{"value": {"statuses": [failed]}}]}]}
    )  # duplicate: no 2nd resend
    await asyncio.sleep(0.05)
    assert sum(1 for m in meta.sent if m["type"] == "template") == 1


async def test_window_known_open_after_a_reply_then_stale_window_recovers(meta, http):
    c = make_cloud(meta, http)
    meta.window_open.add("16235550146")
    await c.handle_webhook(inbound("hi"))
    await c.send_alert(ME, FULL, "t")
    assert meta.sent[-1]["type"] == "text"
    c.last_inbound["16235550146"] = time.time() - 3600
    meta.window_open.clear()  # Meta says the window closed (synchronous 131047)
    await c.send_alert(ME, FULL, "t2")
    assert meta.sent[-1]["template"]["name"] == "hello_world" and not c.window_open(ME)


async def test_other_errors_are_reported(meta, http):
    c = make_cloud(meta, http)
    meta.fail_code = 131030
    with pytest.raises(CloudError, match="131030.*allowed list"):
        await c.send_alert(ME, FULL, "t")
    bad = CloudAPI(http, token="wrong", phone_number_id=PHONE_ID, api_base=str(meta.server.make_url("")))
    with pytest.raises(CloudError, match="190.*access token"):
        await bad.refresh_phone()


def test_one_line():
    assert one_line("*Head*\n\nline\twith\ttabs\nx      y") == "Head · line with tabs · x   y"
    assert len(one_line("a" * 2000, limit=900)) == 900


# --------------------------------------------------------------------------- receipts + strangers


async def test_receipts_and_strangers(meta, http):
    c = make_cloud(meta, http)
    meta.window_open.add("16235550146")
    wamid = await c.send_alert(ME, FULL, "t")
    await c.handle_webhook(statuses(wamid, "delivered", "read"))
    st = c.status()
    assert (
        st["recent"][0]["status"] == "read"
        and st["stats"]["delivered"] == 1
        and st["delivery_median_s"] is not None
    )
    failed = await c.send_alert(ME, "x", "x")
    await c.handle_webhook(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": failed,
                                        "status": "failed",
                                        "errors": [{"code": 131026, "title": "Message undeliverable"}],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert c.messages[failed]["error"] == "131026 Message undeliverable"
    sent_before = len(meta.sent)
    await c.handle_webhook(inbound("pause", sender="447700900123"))  # not an alert recipient
    assert len(meta.sent) == sent_before and not c.window_open("447700900123")


# --------------------------------------------------------------------------- engine + web


def cloud_engine(tmp_path: Any, meta: FakeMeta, **extra: Any) -> Engine:
    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path), **extra.pop("general", {})},
            "market": {"enabled": False},
            "notify": {
                "console": {"enabled": False},
                "whatsapp_cloud": {
                    "enabled": True,
                    "token": TOKEN,
                    "phone_number_id": PHONE_ID,
                    "waba_id": WABA,
                    "verify_token": "vt-123",
                    "app_secret": "s3cret",
                    "to": ME,
                    "api_base": str(meta.server.make_url("")),
                },
            },
            "web": {"token": "tok", "public_url": "https://news247.example.com"},
        }
    )
    return Engine(cfg, storage=Storage(tmp_path / "t.db"), sources=[])


def signed(body: dict[str, Any], secret: str = "s3cret") -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


async def test_webhook_endpoint_and_commands(tmp_path, meta):
    eng = cloud_engine(tmp_path, meta)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            # Meta's subscription handshake (no dashboard token needed, the verify token decides)
            q = "hub.mode=subscribe&hub.verify_token=vt-123&hub.challenge=987654"
            async with s.get(srv.make_url(f"/webhooks/whatsapp?{q}")) as r:
                assert r.status == 200 and await r.text() == "987654"
            async with s.get(
                srv.make_url("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=1")
            ) as r:
                assert r.status == 403
            # forged calls are refused
            raw, headers = signed(inbound("pause 2h"), secret="wrong")
            async with s.post(srv.make_url("/webhooks/whatsapp"), data=raw, headers=headers) as r:
                assert r.status == 403
            # a real reply: the command runs and the answer goes back inside the open window
            meta.window_open.add("16235550146")
            raw, headers = signed(inbound("pause 2h"))
            async with s.post(srv.make_url("/webhooks/whatsapp"), data=raw, headers=headers) as r:
                assert r.status == 200
            await asyncio.sleep(0.3)
            assert eng.dispatcher.paused and meta.sent[-1]["text"]["body"].startswith("⏸ Texts paused until")
            # the Setup page shows what to paste into Meta's webhook form
            async with s.get(srv.make_url("/api/whatsapp-cloud?token=tok")) as r:
                st = await r.json()
            assert (
                st["webhook_url"] == "https://news247.example.com/webhooks/whatsapp"
                and st["verify_token"] == "vt-123"
            )
            assert st["signature_check"] and st["webhook_seen"]
    finally:
        await srv.close()
        await eng.http.close()


async def test_dispatch_and_send_test_through_cloud(tmp_path, meta):
    eng = cloud_engine(tmp_path, meta)
    await eng.http.start()
    try:
        meta.window_open.add("16235550146")
        eng.whatsapp_cloud.last_inbound["16235550146"] = time.time()  # type: ignore[union-attr]  # you replied
        res = await eng.send_test()
        assert res == {"whatsapp_cloud": "ok"} and "News247 test" in meta.sent[-1]["text"]["body"]
        item = NewsItem(
            source="x",
            title="*OPENAI LAUNCHES AGENTS",
            url="https://x.com/1",
            tier=SourceTier.SOCIAL,
            published=time.time(),
        )
        an = Analysis(score=90, severity=Severity.CRITICAL, tickers=["CRM"], direction="down")
        out = await eng.dispatcher.dispatch(
            Alert(
                kind="news",
                severity=Severity.CRITICAL,
                title=item.title,
                body="",
                url=item.url,
                item=item,
                analysis=an,
            )
        )
        assert out == {"whatsapp_cloud": "ok"} and meta.sent[-1]["text"]["body"].startswith(
            "*🔴 OPENAI LAUNCHES AGENTS*"
        )
    finally:
        await eng.http.close()


async def test_engine_maintains_template_and_keepalive(tmp_path, meta, server):
    server.on("/health", {"ok": True})
    eng = cloud_engine(tmp_path, meta, general={"keepalive_url": server.url(""), "keepalive_s": 0.05})
    stop = asyncio.Event()
    run = asyncio.ensure_future(eng.run(stop))
    try:
        # wait on the outcome, not a fixed sleep: start-up work (the history warm-up thread)
        # can hold the event loop for a moment on a slow or busy machine
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline and not (
            meta.created and sum(1 for r in server.requests if r["path"] == "/health") >= 3
        ):
            await asyncio.sleep(0.05)
        assert eng.whatsapp_cloud.template_status == "PENDING" and meta.created  # type: ignore[union-attr]
        assert eng.whatsapp_cloud.phone["display_phone_number"] == "15551234000"  # type: ignore[union-attr]
        assert sum(1 for r in server.requests if r["path"] == "/health") >= 3  # keeps a free host awake
    finally:
        stop.set()
        await asyncio.wait_for(run, 10)


# --------------------------------------------------------------------------- locked accounts + public pages


async def test_locked_account_explains_itself(meta, http):
    async def locked(request: web.Request) -> web.Response:
        return FakeMeta.error(131031, "Business Account locked")

    health = {
        "health_status": {
            "can_send_message": "BLOCKED",
            "entities": [
                {"entity_type": "PHONE_NUMBER", "id": PHONE_ID, "can_send_message": "AVAILABLE"},
                {
                    "entity_type": "WABA",
                    "id": WABA,
                    "can_send_message": "BLOCKED",
                    "errors": [
                        {
                            "error_code": 141006,
                            "error_description": "There is an error with the payment method.",
                            "possible_solution": "Add a valid payment method or complete Business info.",
                        }
                    ],
                },
            ],
        }
    }

    async def node(request: web.Request) -> web.Response:
        if request.query.get("fields") == "health_status":
            return web.json_response(health)
        return web.json_response({"name": "News247", "account_review_status": "PENDING"})

    app = web.Application()
    app.router.add_post("/v23.0/{phone}/messages", locked)
    app.router.add_get("/v23.0/{node}", node)
    srv = TestServer(app)
    await srv.start_server()
    try:
        c = CloudAPI(
            http, token=TOKEN, phone_number_id=PHONE_ID, waba_id=WABA, api_base=str(srv.make_url(""))
        )
        with pytest.raises(CloudError) as exc:
            await c.send_alert(ME, FULL, "t")
        assert (
            exc.value.code == 131031 and "Business info" in str(exc.value) and "Setup page" in str(exc.value)
        )
        await asyncio.sleep(0.1)  # the health check runs in the background after a lock
        st = c.status()
        assert (
            st["health"]["can_send_message"] == "BLOCKED"
            and st["account"]["account_review_status"] == "PENDING"
        )
        (problem,) = st["health"]["problems"]
        assert (
            problem["entity"] == "WABA" and problem["code"] == 141006 and "payment" in problem["description"]
        )
        assert "131031" in st["last_error"]
    finally:
        await srv.close()


async def test_public_pages_for_meta(tmp_path, meta):
    eng = cloud_engine(tmp_path, meta)
    eng.cfg.general.user_agent = "News247Monitor/1.0 (owner@example.com)"
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            for path, must in (
                ("/about", "personal market-news alert"),
                ("/privacy", 'id="deletion"'),
                ("/terms", "not investment advice"),
            ):
                async with s.get(srv.make_url(path)) as r:  # no dashboard token needed
                    text = await r.text()
                    assert r.status == 200 and must in text and "owner@example.com" in text, path
            async with s.get(srv.make_url("/api/status")) as r:
                assert r.status == 401  # everything else stays private
    finally:
        await srv.close()


async def test_rejected_template_is_replaced_automatically(meta, http):
    c = make_cloud(meta, http)
    assert await c.ensure_template() == "PENDING" and c.template == "news247_alert"
    meta.templates["news247_alert"].update(status="REJECTED", rejected_reason="INVALID_FORMAT")
    assert await c.ensure_template() == "PENDING"
    assert c.template == "news247_alert_v2" and not c.template_button
    v2 = meta.created[-1]
    assert v2["name"] == "news247_alert_v2" and len(v2["components"]) == 1  # no button this time
    assert "INVALID_FORMAT" in c.template_note
    body = v2["components"][0]["text"]
    assert not body.startswith("{{") and not body.rstrip(".").endswith("}}")
    meta.templates["news247_alert_v2"]["status"] = "APPROVED"
    assert await c.ensure_template() == "APPROVED" and len(meta.created) == 2
    await c.send_alert(ME, FULL, "t")
    tpl = meta.sent[-1]["template"]
    assert tpl["name"] == "news247_alert_v2" and len(tpl["components"]) == 1  # body only
    # every wording refused: said plainly
    for name in ("news247_alert_v2",):
        meta.templates[name]["status"] = "REJECTED"
    await c.ensure_template()  # submits v3
    meta.templates["news247_alert_v3"]["status"] = "REJECTED"
    assert await c.ensure_template() == "REJECTED" and "refused every wording" in c.template_note
