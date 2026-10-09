"""aiohttp dashboard: live alerts via Server-Sent Events plus a small JSON API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from importlib import resources
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

from ..config import WebConfig

if TYPE_CHECKING:
    from ..engine import Engine

log = logging.getLogger(__name__)


def _json(data: Any) -> web.Response:
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=str))


class WebServer:
    def __init__(self, engine: Engine, cfg: WebConfig) -> None:
        self.engine = engine
        self.cfg = cfg
        self.app = self.build_app()
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/", self.index)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/status", self.api_status)
        app.router.add_get("/api/alerts", self.api_alerts)
        app.router.add_get("/api/items", self.api_items)
        app.router.add_get("/api/market", self.api_market)
        app.router.add_get("/api/latency", self.api_latency)
        app.router.add_get("/events", self.events)
        app.router.add_get("/setup", self.setup_page)
        app.router.add_get("/api/setup", self.api_setup)
        app.router.add_post("/api/setup", self.api_setup_save)
        app.router.add_post("/api/setup/test", self.api_setup_test)
        app.router.add_get("/api/relay", self.api_relay)
        app.router.add_get("/relay/ws", self.relay_ws)
        app.router.add_get("/relay/install.sh", self.relay_install)
        app.router.add_get("/relay/package.tar.gz", self.relay_package)
        app.router.add_get("/api/whatsapp", self.api_whatsapp)
        app.router.add_post("/api/whatsapp/pair", self.api_whatsapp_pair)
        app.router.add_post("/api/whatsapp/unlink", self.api_whatsapp_unlink)
        app.router.add_get("/api/whatsapp-cloud", self.api_whatsapp_cloud)
        app.router.add_get("/webhooks/whatsapp", self.whatsapp_webhook_verify)
        app.router.add_post("/webhooks/whatsapp", self.whatsapp_webhook)
        return app

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        # these authenticate themselves: relays (challenge-response), Meta (verify token/signature)
        if self.cfg.token and request.path not in ("/health", "/relay/ws", "/webhooks/whatsapp"):
            supplied = request.query.get("token") or request.headers.get("Authorization", "").removeprefix(
                "Bearer "
            )
            if not hmac.compare_digest(supplied.encode(), self.cfg.token.encode()):
                raise web.HTTPUnauthorized(text="token required")
        return await handler(request)

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.host, self.cfg.port)
        await site.start()
        log.info(
            "dashboard: http://%s:%d/",
            self.cfg.host if self.cfg.host != "0.0.0.0" else "localhost",
            self.cfg.port,
        )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ handlers

    async def index(self, request: web.Request) -> web.Response:
        page = resources.files("news247.web").joinpath("static/index.html").read_text(encoding="utf-8")
        return web.Response(text=page, content_type="text/html")

    async def setup_page(self, request: web.Request) -> web.Response:
        page = resources.files("news247.web").joinpath("static/setup.html").read_text(encoding="utf-8")
        return web.Response(text=page, content_type="text/html")

    def _check_write(self, request: web.Request) -> None:
        """Changing settings needs either a dashboard token (checked by middleware) or a
        request from this machine itself. A public URL without a token is read-only."""
        if self.cfg.token:
            return
        local = request.remote in ("127.0.0.1", "::1") and "X-Forwarded-For" not in request.headers
        if not local:
            raise web.HTTPForbidden(
                text="Setup changes from the internet need DASHBOARD_TOKEN set on the server; "
                "then open this page with ?token=YOUR_TOKEN"
            )

    async def api_setup(self, request: web.Request) -> web.Response:
        st = self.engine.phone_status()
        st["writable"] = True
        try:
            self._check_write(request)
        except web.HTTPForbidden:
            st["writable"] = False
        return _json(st)

    async def api_setup_save(self, request: web.Request) -> web.Response:
        self._check_write(request)
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="expected JSON") from None
        phone = str(body.get("phone", "")).strip()
        digits = sum(c.isdigit() for c in phone)
        if not ("@" in phone or 10 <= digits <= 15):
            raise web.HTTPBadRequest(text="enter a phone number like +1 555 123 4567")
        if not self.engine.dispatcher.phone_channels:
            raise web.HTTPConflict(
                text="No texting service is switched on yet (e.g. SENDBLUE_ENABLED=true plus its keys)."
            )
        applied = self.engine.set_phone(phone)
        sb = next((c for c in self.engine.dispatcher.phone_channels if c.name == "sendblue"), None)
        if sb is not None:
            await sb.ensure_from_number()  # type: ignore[attr-defined]
        return _json({"phone": applied, **self.engine.phone_status()})

    async def api_setup_test(self, request: web.Request) -> web.Response:
        self._check_write(request)
        return _json(await self.engine.send_test())

    # ------------------------------------------------------------------ iMessage relay

    def _hub(self) -> Any:
        hub = self.engine.relay_hub
        if hub is None:
            raise web.HTTPNotFound(
                text="The iMessage relay is off. Set RELAY_ENABLED=true on the server and restart it."
            )
        return hub

    def public_base(self, request: web.Request) -> str:
        """The address a Mac on the internet uses to reach this server."""
        if self.cfg.public_url:
            return self.cfg.public_url.rstrip("/")
        for env, fmt in (
            ("RENDER_EXTERNAL_URL", "{}"),
            ("RAILWAY_PUBLIC_DOMAIN", "https://{}"),
            ("FLY_APP_NAME", "https://{}.fly.dev"),
        ):
            if os.environ.get(env):
                return fmt.format(os.environ[env]).rstrip("/")
        proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
        host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
        return f"{proto}://{host}"

    def _token_qs(self) -> str:
        return f"?token={quote(self.cfg.token)}" if self.cfg.token else ""

    async def api_relay(self, request: web.Request) -> web.Response:
        hub = self._hub()
        out = hub.status()
        try:
            self._check_write(request)
            base = self.public_base(request)
            out["install_command"] = f"curl -fsSL '{base}/relay/install.sh{self._token_qs()}' | bash"
            out["server"] = base
            out["secret"] = hub.secret
        except web.HTTPForbidden:
            out["install_command"] = ""
        return _json(out)

    async def relay_ws(self, request: web.Request) -> web.StreamResponse:
        return await self._hub().handle(request)

    async def relay_install(self, request: web.Request) -> web.Response:
        from ..relay.package import install_script

        hub = self._hub()
        self._check_write(request)  # the script contains the relay secret
        base = self.public_base(request)
        script = install_script(base, hub.secret, f"{base}/relay/package.tar.gz{self._token_qs()}")
        return web.Response(
            text=script, content_type="text/x-shellscript", headers={"Cache-Control": "no-store"}
        )

    async def relay_package(self, request: web.Request) -> web.Response:
        from ..relay.package import source_tarball

        self._hub()
        data = await asyncio.to_thread(source_tarball)
        return web.Response(
            body=data,
            content_type="application/gzip",
            headers={"Content-Disposition": 'attachment; filename="news247.tar.gz"'},
        )

    # ------------------------------------------------------------------ WhatsApp sender

    def _wa(self) -> Any:
        wa = self.engine.whatsapp
        if wa is None:
            raise web.HTTPNotFound(
                text="WhatsApp is off. Set WHATSAPP_ENABLED=true on the server and restart it."
            )
        return wa

    async def api_whatsapp(self, request: web.Request) -> web.Response:
        from ..whatsapp.neonize_backend import qr_svg

        wa = self._wa()
        try:
            self._check_write(request)
            writable = True
        except web.HTTPForbidden:
            writable = False
        st = wa.status(qr_svg if writable else None)
        if not writable:  # whoever scans the QR links *their* account as the sender
            st["qr"] = st["pair_code"] = None
        st["writable"] = writable
        return _json(st)

    async def api_whatsapp_pair(self, request: web.Request) -> web.Response:
        wa = self._wa()
        self._check_write(request)
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="expected JSON") from None
        try:
            code = await wa.request_pair_code(str(body.get("phone", "")))
        except Exception as exc:  # noqa: BLE001 - show the reason on the page
            raise web.HTTPConflict(text=str(exc)) from None
        return _json({"code": code})

    async def api_whatsapp_unlink(self, request: web.Request) -> web.Response:
        wa = self._wa()
        self._check_write(request)
        await wa.unlink()
        return _json({"ok": True})

    # ------------------------------------------------------------------ WhatsApp Cloud API (Meta)

    def _cloud(self) -> Any:
        cloud = self.engine.whatsapp_cloud
        if cloud is None:
            raise web.HTTPNotFound(
                text="WhatsApp Cloud API is off (WHATSAPP_CLOUD_ENABLED=true and its keys)"
            )
        return cloud

    async def api_whatsapp_cloud(self, request: web.Request) -> web.Response:
        cloud = self._cloud()
        st = cloud.status()
        st["webhook_url"] = f"{self.public_base(request)}/webhooks/whatsapp"
        try:
            self._check_write(request)
            st["verify_token"] = cloud.verify_token  # needed to fill in Meta's webhook form
        except web.HTTPForbidden:
            st["verify_token"] = ""
        return _json(st)

    async def whatsapp_webhook_verify(self, request: web.Request) -> web.Response:
        challenge = self._cloud().verify_subscription(dict(request.query))
        if challenge is None:
            raise web.HTTPForbidden(text="verify token mismatch")
        return web.Response(text=challenge)

    async def whatsapp_webhook(self, request: web.Request) -> web.Response:
        cloud = self._cloud()
        raw = await request.read()
        if not cloud.signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
            log.warning("WhatsApp webhook with a bad signature from %s", request.remote)
            raise web.HTTPForbidden(text="bad signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise web.HTTPBadRequest(text="expected JSON") from None
        # answer Meta immediately (it retries slow webhooks); replies are sent in the background
        task = asyncio.ensure_future(cloud.handle_webhook(payload))
        cloud._tasks.add(task)
        task.add_done_callback(cloud._tasks.discard)
        return web.Response(text="ok")

    async def health(self, request: web.Request) -> web.Response:
        st = self.engine.status()
        ok_sources = sum(1 for s in st["sources"] if s["status"] in ("ok", "starting"))
        return _json(
            {
                "ok": True,
                "uptime_s": round(st["uptime_s"]),
                "sources_ok": ok_sources,
                "sources": len(st["sources"]),
            }
        )

    async def api_status(self, request: web.Request) -> web.Response:
        return _json(self.engine.status())

    async def api_alerts(self, request: web.Request) -> web.Response:
        limit = min(500, int(request.query.get("limit", 100)))
        return _json(self.engine.storage.recent_alerts(limit))

    async def api_items(self, request: web.Request) -> web.Response:
        limit = min(1000, int(request.query.get("limit", 200)))
        min_score = float(request.query.get("min_score", 0))
        return _json(self.engine.storage.recent_items(limit, min_score))

    async def api_market(self, request: web.Request) -> web.Response:
        return _json(self.engine.detector.snapshot())

    async def api_latency(self, request: web.Request) -> web.Response:
        return _json(self.engine.storage.latency_stats())

    async def events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)
        q = self.engine.subscribe()
        try:
            await resp.write(b"event: hello\ndata: {}\n\n")
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(f": ping {time.time():.0f}\n\n".encode())
                    continue
                payload = json.dumps(msg["data"], default=str)
                await resp.write(f"event: {msg['event']}\ndata: {payload}\n\n".encode())
        except ConnectionResetError:
            pass  # browser tab closed
        finally:
            self.engine.unsubscribe(q)
        return resp
