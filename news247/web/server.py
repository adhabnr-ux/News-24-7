"""aiohttp dashboard: live alerts via Server-Sent Events plus a small JSON API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from importlib import resources
from typing import TYPE_CHECKING, Any

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
        return app

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self.cfg.token and request.path != "/health":
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
