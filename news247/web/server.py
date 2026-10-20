"""aiohttp dashboard: live alerts via Server-Sent Events plus a small JSON API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from importlib import resources
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

from .. import __version__
from ..config import WebConfig

if TYPE_CHECKING:
    from ..engine import Engine

log = logging.getLogger(__name__)


# no dashboard token: health checks, endpoints that authenticate themselves (relays, Meta's webhook)
# and the public pages Meta requires (business website, privacy policy, terms)
PUBLIC_PATHS = {"/health", "/relay/ws", "/webhooks/whatsapp", "/about", "/privacy", "/terms"}


def _key_page(path: str, wrong: bool) -> web.Response:
    """A browser opening a protected page without the key gets a form to enter it, not a dead end."""
    note = "That key didn't work. " if wrong else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>News247: access key</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#05070a;color:#e8edf2;
font:16px/1.5 -apple-system,system-ui,sans-serif;padding:16px;box-sizing:border-box}}
form{{max-width:360px;width:100%}}h1{{font-size:22px;margin:0 0 8px}}p{{color:#9aa6b2;margin:0 0 16px}}
input,button{{width:100%;box-sizing:border-box;font:inherit;padding:12px 14px;border-radius:12px}}
input{{background:#0e131a;color:inherit;border:1px solid #233040;margin-bottom:10px}}
button{{background:#e8edf2;color:#05070a;border:0;font-weight:600}}a{{color:#9ecbff}}</style></head>
<body><form method="get" action="{quote(path)}"><h1>Access key</h1>
<p>{note}Enter the DASHBOARD_TOKEN from your server's settings (Render: your service, then
Environment). On your phone, use <a href="/app/">Foretape</a>: it remembers the key.</p>
<input name="token" type="password" autocomplete="current-password" autocapitalize="off"
spellcheck="false" placeholder="access key" required autofocus>
<button type="submit">Open</button></form></body></html>"""
    return web.Response(status=401, text=page, content_type="text/html")


SLIM_TYPES = {  # what is worth gzipping and revalidating (not images, fonts, archives or streams)
    "application/json",
    "application/javascript",
    "text/css",
    "text/html",
    "text/plain",
    "text/csv",
    "text/calendar",
    "image/svg+xml",
    "application/manifest+json",
}


def _json(data: Any) -> web.Response:
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=str))


class WebServer:
    def __init__(self, engine: Engine, cfg: WebConfig) -> None:
        self.engine = engine
        self.cfg = cfg
        self.app = self.build_app()
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._slim, self._auth])
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
        app.router.add_get("/app", self.app_redirect)
        app.router.add_get("/app/", self.app_shell)
        app.router.add_get("/app/sw.js", self.app_sw)
        app.router.add_get("/app/manifest.webmanifest", self.app_manifest)
        app.router.add_get("/app/push-key", self.app_push_key)
        app.router.add_get("/app/fonts/{font}", self.app_font)
        app.router.add_get("/app/{name}", self.app_asset)
        app.router.add_get("/api/app", self.api_app)
        app.router.add_get("/api/foretape", self.api_foretape)
        app.router.add_get("/api/brief", self.api_brief)
        app.router.add_get("/api/scorecard", self.api_scorecard)
        app.router.add_get("/api/calendar", self.api_calendar)
        app.router.add_get("/api/watch", self.api_watch)
        app.router.add_get("/api/radar", self.api_radar)
        app.router.add_get("/api/options", self.api_options)
        app.router.add_get("/api/trends", self.api_trends)
        app.router.add_get("/api/camillo", self.api_camillo)
        app.router.add_get("/api/alert/{id}", self.api_alert)
        app.router.add_get("/api/ticker/{sym}", self.api_ticker)
        app.router.add_get("/api/search", self.api_search)
        app.router.add_get("/api/desk", self.api_desk)
        app.router.add_post("/api/desk", self.api_desk_edit)
        app.router.add_get("/api/smart", self.api_smart)
        app.router.add_get("/api/sectors", self.api_sectors)
        app.router.add_get("/api/record", self.api_record)
        app.router.add_get("/api/implied/{sym}", self.api_implied)
        app.router.add_get("/api/peers/{sym}", self.api_peers)
        app.router.add_get("/api/report", self.api_report)
        app.router.add_get("/api/gaps", self.api_gaps)
        app.router.add_get("/api/sector", self.api_sector)
        app.router.add_get("/api/halts", self.api_halts)
        app.router.add_get("/api/worth", self.api_worth)
        app.router.add_get("/api/earnings/{sym}", self.api_earnings)
        app.router.add_get("/api/calendar.ics", self.api_ics)
        app.router.add_post("/api/why/{sym}", self.api_why)
        app.router.add_get("/api/day", self.api_day)
        app.router.add_get("/api/breadth", self.api_breadth)
        app.router.add_get("/api/earnings-week", self.api_earnings_week)
        app.router.add_get("/api/inplay", self.api_inplay)
        app.router.add_get("/api/portfolio", self.api_portfolio)
        app.router.add_get("/api/memory", self.api_memory)
        app.router.add_get("/api/tomorrow", self.api_tomorrow)
        app.router.add_get("/api/follow-options", self.api_follow_options)
        app.router.add_post("/api/rule-preview", self.api_rule_preview)
        app.router.add_get("/api/regime", self.api_regime)
        app.router.add_get("/api/close", self.api_close)
        app.router.add_get("/api/similar/{id}", self.api_similar)
        app.router.add_get("/api/volume", self.api_volume)
        app.router.add_get("/api/buzz", self.api_buzz)
        app.router.add_get("/api/export", self.api_export)
        app.router.add_get("/api/sources", self.api_sources)
        app.router.add_get("/api/week", self.api_week)
        app.router.add_get("/api/why-push/{id}", self.api_why_push)
        app.router.add_get("/api/push-log", self.api_push_log)
        app.router.add_post("/api/would-push", self.api_would_push)
        app.router.add_get("/api/lab", self.api_lab)
        app.router.add_get("/api/room", self.api_room)
        app.router.add_get("/api/plan/{id}", self.api_plan)
        app.router.add_get("/api/coach", self.api_coach)
        app.router.add_get("/api/targets", self.api_target_board)
        app.router.add_get("/api/edges", self.api_edges)
        app.router.add_get("/api/working", self.api_working)
        app.router.add_get("/api/setups", self.api_setups)
        app.router.add_get("/api/event-risk", self.api_event_risk)
        app.router.add_get("/api/actions", self.api_actions)
        app.router.add_get("/api/odds", self.api_odds)
        app.router.add_get("/api/firms", self.api_firms)
        app.router.add_get("/api/precheck", self.api_precheck)
        app.router.add_get("/api/misses", self.api_misses)
        app.router.add_get("/api/tune", self.api_tune)
        app.router.add_get("/api/stakes", self.api_stakes)
        app.router.add_get("/api/risk", self.api_risk)
        app.router.add_get("/api/flow", self.api_flow)
        app.router.add_get("/api/persona", self.api_persona)
        app.router.add_get("/api/personas", self.api_personas)
        app.router.add_get("/api/exits", self.api_exits)
        app.router.add_get("/api/clock", self.api_clock)
        app.router.add_get("/api/open", self.api_open)
        app.router.add_get("/api/insiders", self.api_insiders)
        app.router.add_get("/api/drift", self.api_drift)
        app.router.add_get("/api/coverage", self.api_coverage)
        app.router.add_get("/api/call", self.api_call)
        app.router.add_get("/api/climate", self.api_climate)
        app.router.add_get("/api/planvs", self.api_planvs)
        app.router.add_get("/api/overnight", self.api_overnight)
        app.router.add_get("/api/myedge", self.api_myedge)
        app.router.add_get("/api/recall", self.api_recall)
        app.router.add_get("/api/line", self.api_line)
        app.router.add_post("/api/line", self.api_line_set)
        app.router.add_get("/api/macro", self.api_macro)
        app.router.add_get("/api/alpha", self.api_alpha)
        app.router.add_get("/api/equity", self.api_equity)
        app.router.add_get("/api/settings-cost", self.api_settings_cost)
        app.router.add_post("/api/link", self.api_link)
        app.router.add_get("/api/devices", self.api_devices)
        app.router.add_post("/api/devices/remove", self.api_devices_remove)
        app.router.add_get("/api/targets/{sym}", self.api_targets)
        app.router.add_get("/api/list/{code}", self.api_list)
        app.router.add_get("/api/phone-health", self.api_phone_health)
        app.router.add_post("/api/push/subscribe", self.api_push_subscribe)
        app.router.add_post("/api/push/unsubscribe", self.api_push_unsubscribe)
        app.router.add_post("/api/push/test", self.api_push_test)
        app.router.add_post("/api/control", self.api_control)
        app.router.add_get("/about", self.public_page)
        app.router.add_get("/privacy", self.public_page)
        app.router.add_get("/terms", self.public_page)
        app.router.add_get("/webhooks/whatsapp", self.whatsapp_webhook_verify)
        app.router.add_post("/webhooks/whatsapp", self.whatsapp_webhook)
        return app

    @web.middleware
    async def _slim(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """Spend little bandwidth (free hosts meter it): gzip text responses when the client takes
        it, and answer a repeat GET of unchanged content with a bodiless 304 (ETag). The event
        stream, websockets and downloads are not touched."""
        resp = await handler(request)
        if (
            request.method != "GET"
            or not isinstance(resp, web.Response)
            or resp.status != 200
            or not isinstance(resp.body, bytes | bytearray)
            or resp.content_type not in SLIM_TYPES | {"image/png", "image/webp"}
        ):
            return resp
        body = bytes(resp.body)
        etag = 'W/"' + hashlib.sha1(body).hexdigest()[:20] + '"'
        resp.headers["ETag"] = etag
        resp.headers.setdefault("Cache-Control", "no-cache")  # revalidate each time: a 304 costs ~200 bytes
        resp.headers["Vary"] = "Accept-Encoding"
        if request.headers.get("If-None-Match", "").strip() == etag:
            return web.Response(
                status=304,
                headers={k: v for k, v in resp.headers.items() if k in ("ETag", "Cache-Control", "Vary")},
            )
        if (
            resp.content_type in SLIM_TYPES
            and len(body) > 600
            and "gzip" in request.headers.get("Accept-Encoding", "")
        ):
            resp.enable_compression(web.ContentCoding.gzip)
        return resp

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        # these authenticate themselves: relays (challenge-response), Meta (verify token/signature)
        # /app/* is the Foretape shell (no data in it); its data comes from token-protected APIs
        is_app = request.path == "/app" or request.path.startswith("/app/")
        if self.cfg.token and request.path not in PUBLIC_PATHS and not is_app:
            supplied = request.query.get("token") or request.headers.get("Authorization", "").removeprefix(
                "Bearer "
            )
            if not hmac.compare_digest(supplied.encode(), self.cfg.token.encode()):
                wants_page = request.method == "GET" and "text/html" in request.headers.get("Accept", "")
                if wants_page and request.path == "/":
                    raise web.HTTPFound("/app/")  # Foretape asks for the key and remembers it
                if wants_page:
                    return _key_page(request.path, bool(supplied))
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
        st = self.engine.whatsapp_status() or wa.status(qr_svg)
        if not writable:  # whoever scans the QR links *their* account as the sender
            st["qr"] = st["pair_code"] = None
            st["qr_svg"] = ""
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

    # ------------------------------------------------------------------ Foretape (phone app)

    APP_ASSETS = {
        "logo.svg": "image/svg+xml",
        "maskable.svg": "image/svg+xml",
        "icon-192.png": "image/png",
        "icon-512.png": "image/png",
        "icon-maskable-512.png": "image/png",
        "apple-touch-icon.png": "image/png",
        "badge-96.png": "image/png",
        "favicon-32.png": "image/png",
        "app.css": "text/css",
        "app.js": "application/javascript",
        "scene.js": "application/javascript",
        "cmd.js": "application/javascript",
        "launch.webp": "image/webp",
        "depth.png": "image/png",
        "fonts/instrument-serif-normal.woff2": "font/woff2",
        "fonts/instrument-serif-italic.woff2": "font/woff2",
        "fonts/inter-normal.woff2": "font/woff2",
        "fonts/jetbrains-mono-normal.woff2": "font/woff2",
    }
    LONG_CACHE = ("fonts/", "launch.webp", "depth.png")  # versioned by the service worker's cache name

    @staticmethod
    def _app_file(name: str) -> Any:
        return resources.files("news247.web").joinpath(f"static/app/{name}")

    async def app_redirect(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/app/" + (f"?{request.query_string}" if request.query_string else ""))

    async def app_shell(self, request: web.Request) -> web.Response:
        html = self._app_file("index.html").read_text(encoding="utf-8")
        token = request.query.get("token")
        if self._token_ok(token):
            # iOS gives a home-screen app its own storage, separate from Safari's: the key has to
            # travel in the manifest's start_url, which iOS reads when you tap "Add to Home Screen"
            html = html.replace(
                'href="/app/manifest.webmanifest"',
                f'href="/app/manifest.webmanifest?token={quote(token)}"',
                1,
            )
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    async def app_sw(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._app_file("sw.js").read_text(encoding="utf-8"),
            content_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/app/"},
        )

    def _serve_app_file(self, name: str) -> web.Response:
        ctype = self.APP_ASSETS.get(name)  # a whitelist: nothing outside the app folder is reachable
        if ctype is None:
            raise web.HTTPNotFound()
        long = name.startswith(self.LONG_CACHE)
        return web.Response(
            body=self._app_file(name).read_bytes(),
            content_type=ctype,
            headers={"Cache-Control": "public, max-age=2592000, immutable" if long else "no-cache"},
        )

    async def app_asset(self, request: web.Request) -> web.Response:
        return self._serve_app_file(request.match_info["name"])

    async def app_font(self, request: web.Request) -> web.Response:
        return self._serve_app_file("fonts/" + request.match_info["font"])

    def _token_ok(self, supplied: str | None) -> bool:
        return (
            bool(supplied)
            and bool(self.cfg.token)
            and hmac.compare_digest(supplied.encode(), self.cfg.token.encode())
        )

    async def app_manifest(self, request: web.Request) -> web.Response:
        """When opened with a valid ?token, the installed app starts already unlocked."""
        token = request.query.get("token")
        start = "/app/" + (f"?token={quote(token)}" if self._token_ok(token) else "")
        manifest = {
            "name": "Foretape",
            "short_name": "Foretape",
            "description": "Market-moving news before the tape moves.",
            "id": "/app/",
            "start_url": start,
            "scope": "/app/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#060F24",
            "theme_color": "#0E2A62",
            "categories": ["finance", "news", "business"],
            "icons": [
                {"src": "/app/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/app/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {
                    "src": "/app/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
                {"src": "/app/logo.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            ],
        }
        return web.Response(
            text=json.dumps(manifest),
            content_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    def _push(self) -> Any:
        wp = self.engine.webpush
        if wp is None:
            raise web.HTTPNotFound(text="push notifications are off (WEBPUSH_ENABLED=false)")
        return wp

    async def app_push_key(self, request: web.Request) -> web.Response:
        return web.Response(text=self._push().public_key, headers={"Cache-Control": "no-cache"})

    async def api_app(self, request: web.Request) -> web.Response:
        st = self.engine.status()
        wp = self.engine.webpush
        alerts = self.engine.storage.recent_alerts(60)
        for a in alerts:
            a["since"] = self.engine.edge_since(a)
        leads = sorted(self.engine.storage.leads())
        return _json(
            {
                "alerts": alerts,
                "phone_mode": self.engine.phone_mode(),
                "sources": len(st["sources"]),
                "sources_ok": sum(1 for s in st["sources"] if s["status"] in ("ok", "starting")),
                "uptime_s": st["uptime_s"],
                "alerts_24h": st["db"]["alerts_24h"],
                "devices": len(wp.subs) if wp is not None else 0,  # type: ignore[attr-defined]
                "durable": bool(wp is not None and wp.state is not None) or not os.environ.get("RENDER"),  # type: ignore[attr-defined]
                "market": self.engine.calendar.market_status(),
                "edge": {
                    "stories": len(leads),
                    "median_lead_s": leads[len(leads) // 2] if leads else None,
                },
                "next": self.engine.calendar.upcoming(days=35)[:6],
                "brief_time": str(wp.options.get("brief_time", "08:15") or "") if wp is not None else "",
                "smallcap": self._smallcap_brief(st.get("smallcap") or {}),
                "options": self._options_brief(st.get("options") or {}),
            }
        )

    async def api_brief(self, request: web.Request) -> web.Response:
        from ..personal import your_stocks

        b = self.engine.brief()
        dev = self._device(request)
        b["mine"] = your_stocks(self.engine, dev) if dev else []
        return _json(b)

    async def api_scorecard(self, request: web.Request) -> web.Response:
        """Who knew first: every big move of a day with its verdict (``?date=YYYY-MM-DD``,
        default today), plus the saved daily summaries (``?days=30``)."""
        day = request.query.get("date") or None
        try:
            card = self.engine.scorecard_for(day)
        except ValueError:
            return web.json_response({"error": "date must be YYYY-MM-DD"}, status=400)
        days = min(365, max(0, int(request.query.get("days", 30) or 0)))
        card["history"] = self.engine.scorecard_history(days) if days else []
        return _json(card)

    async def api_calendar(self, request: web.Request) -> web.Response:
        days = min(120, max(1, int(request.query.get("days", 45))))
        return _json(
            {
                "events": self.engine.calendar.upcoming(days=days),
                "market": self.engine.calendar.market_status(),
            }
        )

    async def api_watch(self, request: web.Request) -> web.Response:
        snap = self.engine.detector.snapshot()
        rows = [
            {
                "symbol": sym,
                "price": v["price"],
                "chg_day": v["chg_day"],
                "chg_5m": v["chg_5m"],
                "ts": v["ts"],
            }
            for sym, v in snap.items()
        ]
        rows.sort(key=lambda r: abs(r["chg_day"] or 0.0), reverse=True)
        return _json({"symbols": rows, "market": self.engine.calendar.market_status()})

    @staticmethod
    def _options_brief(o: dict[str, Any]) -> dict[str, Any]:
        """For the Desk: is the options tape reading chains, from which feed, and is it healthy?"""
        if not o.get("enabled"):
            return {"enabled": False}
        primary = next(iter((o.get("sources") or {}).keys()), "")
        return {
            "enabled": True,
            "feed": primary,
            "companies": o.get("companies", 0),
            "read_today": o.get("read_today", 0),
            "hits": o.get("hits", 0),
            "fallbacks": o.get("fallbacks", 0),
            "status": o.get("status", ""),
            "last_error": o.get("last_error", ""),
        }

    @staticmethod
    def _smallcap_brief(sc: dict[str, Any]) -> dict[str, Any]:
        """For the Desk: is the small-cap lane armed (companies sized, radar scanning)?"""
        radar = sc.get("radar") or {}
        return {
            "listings": sc.get("listings", 0),
            "age_s": sc.get("universe_age_s"),
            "radar": bool(radar.get("enabled")),
            "radar_status": radar.get("status", ""),
            "scans": radar.get("scans", 0),
            "hits": radar.get("hits", 0),
        }

    async def api_radar(self, request: web.Request) -> web.Response:
        """The small-cap radar: names breaking out right now, how big they are, how much is
        trading, and the headline behind the move if one exists yet."""
        try:
            limit = max(1, min(100, int(request.query.get("limit", "40"))))
        except ValueError:
            limit = 40
        return _json(self.engine.radar_board(limit))

    async def api_options(self, request: web.Request) -> web.Response:
        """The options tape: the latest spike / unusual-volume hit per company, and the feed's
        health (companies covered, chains read, rotation progress, stale or failed reads)."""
        try:
            limit = max(1, min(100, int(request.query.get("limit", "40"))))
        except ValueError:
            limit = 40
        return _json(self.engine.options_board(limit))

    async def api_trends(self, request: web.Request) -> web.Response:
        """The trend lane: the latest signal per company (``?days=30``), strongest first, with
        every piece of evidence and the filters' reasons; the scorecard (hit rate and excess
        return vs SPY at 30/60/90 days, by severity); and the scanners' health."""
        try:
            days = max(1.0, min(365.0, float(request.query.get("days", "30"))))
        except ValueError:
            days = 30.0
        return _json(self.engine.trends_board(days))

    async def api_camillo(self, request: web.Request) -> web.Response:
        """The Camillo desk: the method (principles, rules, case studies), live ideas with the
        checklist and next report date, what's heating up, the scorecard and the feeds' health."""
        return _json(self.engine.camillo_page())

    async def api_alert(self, request: web.Request) -> web.Response:
        """Everything about one alert: the analysis, the play, precedents, every source that
        carried the story (first one first) and the tape since."""
        alert = self.engine.storage.get_alert(request.match_info["id"])
        if alert is None:
            raise web.HTTPNotFound(text="no such alert")
        alert["since"] = self.engine.edge_since(alert)
        uid = (alert.get("item") or {}).get("uid")
        story = self.engine.storage.story_of(uid) if uid else None
        alert["story_sources"] = self.engine.storage.story_sources(story) if story is not None else []
        alert["story_items"] = self.engine.storage.story_items(story) if story is not None else []
        alert["series"] = self.engine.price_series(alert)
        alert["follow"] = self.engine.follow_for(alert)
        from ..misses import post_mortem

        alert["post_mortem"] = post_mortem(self.engine, alert)  # why it missed, when it did
        from ..liveodds import live_odds

        alert["live_odds"] = live_odds(self.engine, alert)  # an hour in: how alerts like it finished
        return _json(alert)

    # ------------------------------------------------------------------ dossier, desk, boards

    async def api_ticker(self, request: web.Request) -> web.Response:
        """Everything about one stock: price, why it moves, the evidence tally, its alerts and what
        they did since, insider buys, options, the radar, its calendar."""
        from ..dossier import ticker_dossier

        d = ticker_dossier(self.engine, request.match_info["sym"])
        if d is None:
            raise web.HTTPBadRequest(text="not a symbol")
        dev = self._device(request)
        d["watching"] = bool(dev and d["symbol"] in (self.engine.desk.get(dev).get("watchlist") or []))
        if dev:
            from ..dossier import since_you_looked

            d["since_look"] = since_you_looked(self.engine, dev, d["symbol"])
        return _json(d)

    async def api_search(self, request: web.Request) -> web.Response:
        from ..dossier import search

        q = request.query
        try:
            days = max(1.0, min(90.0, float(q.get("days", "30"))))
        except ValueError:
            days = 30.0
        return _json(
            search(
                self.engine,
                q.get("q", ""),
                days=days,
                min_sev=q.get("sev", "").upper(),
                kind=q.get("kind", ""),
            )
        )

    @staticmethod
    def _device(request: web.Request) -> str:
        dev = request.headers.get("X-Device") or request.query.get("device") or ""
        from ..desk import Desk

        return dev if Desk.valid_device(dev) else ""

    async def api_desk(self, request: web.Request) -> web.Response:
        """This phone's desk: watchlist with live prices, price alerts, tracked calls, preferences."""
        dev = self._device(request)
        if not dev:
            raise web.HTTPBadRequest(text="X-Device header (the app's device id) needed")
        return _json(self._desk_view(dev))

    def _desk_view(self, dev: str) -> dict[str, Any]:
        """The desk with each watched stock's last half hour, for the watchlist's sparklines."""
        v = self.engine.desk.view(dev, self.engine.price_of)
        for w in v["watchlist"]:
            path = self.engine.live_quotes.path(w["symbol"])
            pts = path.hist if path is not None else []
            step = max(1, len(pts) // 24)
            w["spark"] = [round(p, 4) for _, p in pts[::step]][-24:]
        from ..catalysts import catalyst_kinds, catalyst_taste

        v["catalyst_kinds"] = catalyst_kinds(self.engine.catalyst_rates)
        v["catalyst_taste"] = catalyst_taste(self.engine, dev)
        return v

    async def api_desk_edit(self, request: web.Request) -> web.Response:
        self._check_write(request)
        dev = self._device(request)
        if not dev:
            raise web.HTTPBadRequest(text="X-Device header (the app's device id) needed")
        try:
            body = await request.json()
            self.engine.desk.apply(dev, str(body.get("action", "")), body, self.engine.price_of)
        except (ValueError, TypeError, AttributeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        syms = [str(body.get("symbol") or "")] + [str(s) for s in body.get("symbols") or []]
        self.engine._track_symbols([s.upper() for s in syms if s])  # quote it from now on
        return _json(self._desk_view(dev))

    async def api_smart(self, request: web.Request) -> web.Response:
        from ..dossier import smart_money

        return _json(smart_money(self.engine))

    async def api_sectors(self, request: web.Request) -> web.Response:
        from ..dossier import sectors

        return _json(sectors(self.engine))

    async def api_implied(self, request: web.Request) -> web.Response:
        """The move the options market prices by each of the next expiries (ATM straddle)."""
        from ..desk import clean_symbol
        from ..personal import implied_move

        sym = clean_symbol(request.match_info["sym"])
        if not sym:
            raise web.HTTPBadRequest(text="not a symbol")
        return _json(await implied_move(self.engine, sym))

    async def api_peers(self, request: web.Request) -> web.Response:
        from ..dossier import peers

        return _json(peers(self.engine, request.match_info["sym"]))

    async def api_report(self, request: web.Request) -> web.Response:
        """This phone's day so far: watchlist, tracked calls, what fired, how the alerts did."""
        from ..personal import report_card

        dev = self._device(request)
        if not dev:
            raise web.HTTPBadRequest(text="X-Device header (the app's device id) needed")
        from ..personal import day_summary

        card = report_card(self.engine, dev) or {}
        if card:
            card["summary"] = day_summary(self.engine, dev)
        return _json(card)

    async def api_gaps(self, request: web.Request) -> web.Response:
        from ..dossier import gaps

        try:
            pct = max(1.0, min(50.0, float(request.query.get("min", "3"))))
        except ValueError:
            pct = 3.0
        return _json(gaps(self.engine, min_pct=pct))

    async def api_sector(self, request: web.Request) -> web.Response:
        from ..dossier import sector_detail

        return _json(sector_detail(self.engine, request.query.get("name", "")[:60]))

    async def api_halts(self, request: web.Request) -> web.Response:
        from ..dossier import halts_board

        return _json(halts_board(self.engine))

    async def api_worth(self, request: web.Request) -> web.Response:
        """Peers of this phone's watchlist that are doing something today."""
        from ..dossier import worth_a_look

        dev = self._device(request)
        return _json({"rows": worth_a_look(self.engine, dev) if dev else []})

    async def api_earnings(self, request: web.Request) -> web.Response:
        from ..desk import clean_symbol
        from ..personal import earnings_reactions

        sym = clean_symbol(request.match_info["sym"])
        if not sym:
            raise web.HTTPBadRequest(text="not a symbol")
        return _json(await earnings_reactions(self.engine, sym))

    async def api_memory(self, request: web.Request) -> web.Response:
        from ..dossier import market_memory

        return _json(market_memory(self.engine))

    async def api_portfolio(self, request: web.Request) -> web.Response:
        from ..dossier import portfolio

        dev = self._device(request)
        if not dev:
            raise web.HTTPBadRequest(text="X-Device header (the app's device id) needed")
        return _json(portfolio(self.engine, dev))

    async def api_tomorrow(self, request: web.Request) -> web.Response:
        from ..dossier import tomorrow

        return _json(tomorrow(self.engine, self._device(request)))

    async def api_follow_options(self, request: web.Request) -> web.Response:
        """What can be followed: the scorer's themes and the universe's sectors."""
        themes = sorted(t.name for t in self.engine.scorer.themes)
        sectors = sorted({li.sector for li in self.engine.universe.by_symbol.values() if li.sector})
        return _json({"themes": themes, "sectors": sectors})

    async def api_rule_preview(self, request: web.Request) -> web.Response:
        from ..dossier import rule_preview

        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="json body needed") from None
        return _json(rule_preview(self.engine, self._device(request), body if isinstance(body, dict) else {}))

    async def api_inplay(self, request: web.Request) -> web.Response:
        from ..dossier import in_play

        return _json(in_play(self.engine))

    async def api_regime(self, request: web.Request) -> web.Response:
        from ..dossier import regime

        return _json(regime(self.engine))

    async def api_close(self, request: web.Request) -> web.Response:
        from ..dossier import close_recap

        return _json(close_recap(self.engine))

    async def api_similar(self, request: web.Request) -> web.Response:
        from ..dossier import similar_alerts

        alert = self.engine.storage.get_alert(request.match_info["id"])
        if alert is None:
            raise web.HTTPNotFound(text="no such alert")
        return _json(similar_alerts(self.engine, alert))

    def _need_device(self, request: web.Request) -> str:
        dev = self._device(request)
        if not dev:
            raise web.HTTPBadRequest(text="X-Device header (the app's device id) needed")
        return dev

    async def api_why_push(self, request: web.Request) -> web.Response:
        """Did this alert reach this phone, and if not, what stopped it (and the tap that fixes it)."""
        from ..pushcheck import why_push

        out = why_push(self.engine, self._need_device(request), request.match_info["id"])
        if out is None:
            raise web.HTTPNotFound(text="no such alert")
        return _json(out)

    async def api_plan(self, request: web.Request) -> web.Response:
        """A paper plan for an alert: entry, stop, target, reward-to-risk and size (plans.py)."""
        from ..plans import trade_plan

        a = self.engine.storage.get_alert(request.match_info["id"])
        if a is None:
            raise web.HTTPNotFound(text="no such alert")
        return _json(trade_plan(self.engine, self._device(request), a))

    async def api_targets(self, request: web.Request) -> web.Response:
        """A stock's analyst price targets, read from the headlines (targets.py)."""
        from ..desk import clean_symbol
        from ..targets import targets_for

        sym = clean_symbol(request.match_info["sym"])
        if not sym:
            raise web.HTTPBadRequest(text="not a symbol")
        return _json(targets_for(self.engine, sym))

    async def api_event_risk(self, request: web.Request) -> web.Response:
        """Each holding's next report or event in 10 days, in dollars on the position (plans.py)."""
        from ..plans import event_risk

        return _json(event_risk(self.engine, self._need_device(request)))

    async def api_actions(self, request: web.Request) -> web.Response:
        """Needs you: everything on this phone that wants a decision now, ranked (actions.py)."""
        from ..actions import action_center

        return _json(action_center(self.engine, self._need_device(request)))

    async def api_odds(self, request: web.Request) -> web.Response:
        """The calibrated odds model: its out-of-sample test, reliability and biggest factors (odds.py)."""
        from ..odds import report

        return _json(report(self.engine.odds))

    async def api_firms(self, request: web.Request) -> web.Response:
        """Which analysts move stocks: each firm's calls, graded here, best and worst (firms.py)."""
        from ..firms import scorecard

        return _json(scorecard(self.engine.firm_rates))

    async def api_precheck(self, request: web.Request) -> web.Response:
        """Before a paper call opens: every reason for pause, good, warning or red (precheck.py)."""
        from ..desk import clean_symbol
        from ..precheck import pre_trade

        sym = clean_symbol(request.query.get("symbol"))
        direction = request.query.get("direction", "long")
        if not sym or direction not in ("long", "short"):
            raise web.HTTPBadRequest(text="need a symbol and direction long|short")
        return _json(
            pre_trade(
                self.engine, self._need_device(request), sym, direction, request.query.get("alert", "")[:40]
            )
        )

    async def api_misses(self, request: web.Request) -> web.Response:
        """Every graded miss of 60 days, by cause: the sector, the market, new news, or the signal (misses.py)."""
        from ..misses import miss_report

        return _json(miss_report(self.engine))

    async def api_tune(self, request: web.Request) -> web.Response:
        """Every edge bar and budget, replayed on this phone's last 30 days of graded alerts (tune.py)."""
        from ..tune import tune_cached

        return _json(tune_cached(self.engine, self._need_device(request)))

    async def api_stakes(self, request: web.Request) -> web.Response:
        """Upcoming binary events and your reports, with how both branches have traded (stakes.py)."""
        from ..stakes import event_record, event_stakes

        return _json(
            {**event_stakes(self.engine, self._device(request) or ""), "record": event_record(self.engine)}
        )

    async def api_risk(self, request: web.Request) -> web.Response:
        """Your risk in one place: open risk, unbounded calls, clusters, sector shocks, events (risk.py)."""
        from ..risk import desk_risk

        return _json(desk_risk(self.engine, self._need_device(request)))

    async def api_flow(self, request: web.Request) -> web.Response:
        """Options flow by shape, graded: which kinds of unusual activity led the stock (flowrec.py)."""
        from ..flowrec import flow_board

        return _json(flow_board(self.engine.flow_rates))

    async def api_persona(self, request: web.Request) -> web.Response:
        """How one stock has taken its own news here: trait, path, by kind, recent (persona.py)."""
        from ..desk import clean_symbol
        from ..persona import persona

        sym = clean_symbol(request.query.get("symbol"))
        if not sym:
            raise web.HTTPBadRequest(text="need ?symbol=")
        return _json(persona(self.engine, sym))

    async def api_personas(self, request: web.Request) -> web.Response:
        """This phone's stocks with a news personality, and the clearest runners and faders (persona.py)."""
        from ..persona import board

        return _json(board(self.engine, request.headers.get("X-Device", "")))

    async def api_exits(self, request: web.Request) -> web.Response:
        """Exit autopsy: what each closed call's stock did after the exit (exits.py)."""
        from ..exits import exit_report

        return _json(exit_report(self.engine, self._need_device(request)))

    async def api_clock(self, request: web.Request) -> web.Response:
        """The news clock: how fast each kind of news plays out, and what's left at 5/15/30 minutes;
        with ?alert=ID, that alert's live read under an hour in (newsclock.py)."""
        from ..newsclock import clock_board, early, early_line

        out = clock_board(self.engine.clock_rates)
        aid = request.query.get("alert")
        if aid:
            a = self.engine.storage.get_alert(aid)
            e = early(self.engine, a) if a else None
            out["early"] = {**e, "line": early_line(e)} if e else None
        return _json(out)

    async def api_open(self, request: web.Request) -> web.Response:
        """Into the open: news from outside the session by gap size and kind, and this morning's
        gappers with the record their gap falls in (opening.py)."""
        from ..opening import open_board, premarket

        return _json({**open_board(self.engine.open_rates),
                      "today": premarket(self.engine, device=request.headers.get("X-Device", ""))})  # fmt: skip

    async def api_insiders(self, request: web.Request) -> web.Response:
        """Insider buys graded by who, how much, how unusual, with others, after a drop, on a plan (insiders.py)."""
        from ..insiders import insider_board

        return _json(insider_board(self.engine.insider_rates))

    async def api_drift(self, request: web.Request) -> web.Response:
        """A month on: which kinds of news kept drifting after five sessions, and which gave it back (drift.py)."""
        from ..drift import drift_board

        return _json(drift_board(self.engine.drift_rates))

    async def api_coverage(self, request: web.Request) -> web.Response:
        """How late to its story each alert came, and how far the story spread: graded (coverage.py)."""
        from ..coverage import coverage_board

        return _json(coverage_board(self.engine.coverage_rates))

    async def api_call(self, request: web.Request) -> web.Response:
        """The call's own record: act now, wait, watch, skip, and how each did (thecall.py)."""
        from ..thecall import VERBS

        rates = self.engine.call_rates
        return _json(
            {"rows": [rates[v] for v in VERBS if v in rates], "replay": self.engine.call_replay or None}
        )

    async def api_climate(self, request: web.Request) -> web.Response:
        """News climate: each kind's last two weeks against its record before, hot or cold beyond chance (climate.py)."""
        from ..climate import climate_board

        return _json(climate_board(self.engine.climate_rates))

    async def api_planvs(self, request: web.Request) -> web.Response:
        """Your exits against the plan's, replayed on your closed calls from their alerts (planvs.py)."""
        from ..planvs import plan_vs

        return _json(plan_vs(self.engine, self._need_device(request)))

    async def api_overnight(self, request: web.Request) -> web.Response:
        """Each open call's standing and how alerts that stood the same at the close did by the next (overnight.py)."""
        from ..overnight import calls_tonight

        return _json(calls_tonight(self.engine, self._need_device(request)))

    async def api_myedge(self, request: web.Request) -> web.Response:
        """Your calls against the alerts they came from, by kind of news: entry and the rest (myedge.py)."""
        from ..myedge import my_edge

        return _json(my_edge(self.engine, self._need_device(request)))

    async def api_recall(self, request: web.Request) -> web.Response:
        """What we caught, what we missed: every big move of the last 20 sessions and today (recall.py)."""
        from ..recall import recall_report

        return _json(recall_report(self.engine))

    async def api_line(self, request: web.Request) -> web.Response:
        """The alert line, tested: headlines just under it graded in the shadow, beside alerts just over it (shadow.py)."""
        from ..shadow import line_report

        return _json(line_report(self.engine))

    async def api_line_set(self, request: web.Request) -> web.Response:
        """Move the alert line on the evidence ({"medium": 30, "why": ...}) or put it back ({"reset": true})."""
        from ..shadow import set_line

        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="need JSON") from None
        try:
            out = set_line(
                self.engine, None if body.get("reset") else body.get("medium"), str(body.get("why") or "")
            )
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return _json(out)

    async def api_macro(self, request: web.Request) -> web.Response:
        """Macro days: each kind of news on Fed and jobs-report days vs other days, and the next one (macrodays.py)."""
        from ..macrodays import macro_board

        return _json(macro_board(self.engine, self.engine.macro_rates))

    async def api_alpha(self, request: web.Request) -> web.Response:
        """Skill or the market: closed calls' returns split into SPY's part and the rest (alpha.py)."""
        from ..alpha import attribution

        return _json(attribution(self.engine, self._need_device(request)))

    async def api_equity(self, request: web.Request) -> web.Response:
        """Equity curves: your calls, each autopilot, every alert at the close, and SPY (equity.py)."""
        from ..equity import equity

        return _json(equity(self.engine, self._need_device(request)))

    async def api_settings_cost(self, request: web.Request) -> web.Response:
        """What this phone's settings kept back over 14 days, graded beside what reached it (keptback.py)."""
        from ..keptback import settings_cost

        try:
            days = min(max(float(request.query.get("days", 14)), 1.0), 60.0)
        except ValueError:
            days = 14.0
        return _json(settings_cost(self.engine, self._need_device(request), days))

    async def api_setups(self, request: web.Request) -> web.Response:
        """Today's setups: the best alerts since the last close, as plans (plans.py)."""
        from ..plans import todays_setups

        return _json(todays_setups(self.engine, self._device(request)))

    async def api_link(self, request: web.Request) -> web.Response:
        """This device joins the desk behind a link code; answers with that desk's device id."""
        self._check_write(request)
        dev = self._need_device(request)
        try:
            body = await request.json()
            primary = self.engine.desk.use_link(str(body.get("code") or ""), dev, bool(body.get("bring")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return _json(
            {"device": primary, "watchlist": len(self.engine.desk.get(primary).get("watchlist") or [])}
        )

    def _devices_of(self, dev: str) -> list[Any]:
        wp = self.engine.webpush
        return [s for s in wp.subs.values() if s.device == dev] if wp is not None else []  # type: ignore[attr-defined]

    async def api_devices(self, request: web.Request) -> web.Response:
        """The devices that share this desk (each push subscription on it)."""
        dev = self._need_device(request)
        mine = request.query.get("endpoint", "")
        return _json({"devices": [
            {"id": hashlib.sha1(s.endpoint.encode()).hexdigest()[:12], "service": s.service, "label": s.label,
             "since": s.created, "last_ok": s.last_ok, "failures": s.failures, "this": s.endpoint == mine}
            for s in self._devices_of(dev)
        ]})  # fmt: skip

    async def api_devices_remove(self, request: web.Request) -> web.Response:
        """Stop pushes to one device on this desk (another device's subscription, by its id)."""
        self._check_write(request)
        dev = self._need_device(request)
        body = await request.json()
        for s in self._devices_of(dev):
            if hashlib.sha1(s.endpoint.encode()).hexdigest()[:12] == str(body.get("id") or ""):
                await self.engine.webpush.unsubscribe(s.endpoint)  # type: ignore[union-attr]
                return _json({"removed": True})
        raise web.HTTPNotFound(text="no such device on this desk")

    async def api_working(self, request: web.Request) -> web.Response:
        """What's working today: today's alerts graded live, by kind of news."""
        from ..catalysts import working_today

        return _json(working_today(self.engine))

    async def api_edges(self, request: web.Request) -> web.Response:
        """The edge finder: kinds of news x size x exit, ranked by how reliably they paid."""
        from ..catalysts import edge_finder

        return _json(edge_finder(self.engine))

    async def api_target_board(self, request: web.Request) -> web.Response:
        """The last few days' analyst target changes across every stock."""
        from ..targets import target_board

        return _json(target_board(self.engine))

    async def api_coach(self, request: web.Request) -> web.Response:
        """What this phone's closed paper calls say about its habits (plans.py)."""
        from ..plans import coach

        return _json(coach(self.engine, self._need_device(request)))

    async def api_room(self, request: web.Request) -> web.Response:
        """Room left: today's alerts that have moved less than their catalyst's typical move."""
        from ..catalysts import room_left

        return _json(room_left(self.engine))

    async def api_lab(self, request: web.Request) -> web.Response:
        """The strategy lab: following every alert like this, on paper (see dossier.strategy_lab)."""
        from ..dossier import strategy_lab

        q = request.query
        try:
            out = strategy_lab(
                self.engine,
                self._device(request),
                kind=q.get("kind", "all"),
                min_sev=q.get("sev", "HIGH").upper(),
                horizon=q.get("horizon", "close"),
                scope=q.get("scope", "all"),
                days=float(q.get("days", "60")),
                stake=max(1.0, min(1e7, float(q.get("stake", "1000")))),
                skip_fades=q.get("fades") == "skip",
                catalyst=q.get("catalyst", "all"),
                band=q.get("band", "all"),
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return _json(out)

    async def api_would_push(self, request: web.Request) -> web.Response:
        """ "Would I get it?": a made-up alert (kind, severity, stock) walked through this phone's gates."""
        from ..pushcheck import would_push

        dev = self._need_device(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        try:
            out = would_push(
                self.engine,
                dev,
                str(body.get("category") or "news"),
                str(body.get("severity") or "HIGH"),
                str(body.get("symbol") or ""),
                catalyst=str(body.get("catalyst") or ""),
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return _json(out)

    async def api_list(self, request: web.Request) -> web.Response:
        """A shared watchlist, read before adding it (the #list=CODE link)."""
        data = self.engine.desk.shared_list(request.match_info["code"])
        if data is None:
            raise web.HTTPNotFound(text="that list code is wrong or expired")
        mine = set((self.engine.desk.profiles.get(self._device(request)) or {}).get("watchlist") or [])
        rows = []
        for s in data["symbols"][:60]:
            li = self.engine.universe.get(s)
            rows.append({"symbol": s, "name": li.name if li is not None else "", "mine": s in mine})
        return _json({**data, "rows": rows, "new": sum(1 for r in rows if not r["mine"])})

    async def api_push_log(self, request: web.Request) -> web.Response:
        from ..pushcheck import push_log

        return _json(push_log(self.engine, self._need_device(request)))

    async def api_phone_health(self, request: web.Request) -> web.Response:
        from ..pushcheck import phone_health

        return _json(phone_health(self.engine, self._need_device(request)))

    async def api_earnings_week(self, request: web.Request) -> web.Response:
        from ..dossier import earnings_week

        return _json(earnings_week(self.engine))

    async def api_breadth(self, request: web.Request) -> web.Response:
        from ..dossier import breadth

        return _json(breadth(self.engine))

    async def api_volume(self, request: web.Request) -> web.Response:
        from ..dossier import volume_leaders

        return _json(volume_leaders(self.engine))

    async def api_buzz(self, request: web.Request) -> web.Response:
        from ..dossier import buzz

        return _json(buzz(self.engine))

    async def api_export(self, request: web.Request) -> web.Response:
        """This phone's desk as a JSON backup, or its calls and journal as CSV (``?format=csv``)."""
        import csv
        import io

        dev = self._device(request)
        if not dev:
            raise web.HTTPBadRequest(text="X-Device header (the app's device id) needed")
        p = self.engine.desk.get(dev)
        if request.query.get("format") == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(
                [
                    "status",
                    "symbol",
                    "direction",
                    "entry",
                    "exit",
                    "pnl_pct",
                    "opened_utc",
                    "closed_utc",
                    "stop",
                    "target",
                    "tags",
                    "note",
                    "lesson",
                ]
            )
            utc = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M") if t else ""  # noqa: E731
            for t in p.get("tracked") or []:
                w.writerow(
                    [
                        "open",
                        t["symbol"],
                        t["direction"],
                        t["entry"],
                        "",
                        "",
                        utc(t.get("ts")),
                        "",
                        t.get("stop") or "",
                        t.get("target") or "",
                        " ".join(t.get("tags") or []),
                        t.get("note") or "",
                        "",
                    ]
                )
            for c in p.get("closed") or []:
                w.writerow(["closed", c["symbol"], c["direction"], c["entry"], c.get("exit") or "", c.get("pnl_pct") if c.get("pnl_pct") is not None else "", utc(c.get("ts")), utc(c.get("closed")),
                            c.get("stop") or "", c.get("target") or "", " ".join(c.get("tags") or []), c.get("note") or "", c.get("lesson") or ""])  # fmt: skip
            return web.Response(
                text=buf.getvalue(),
                content_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="foretape-calls.csv"'},
            )
        data = {k: v for k, v in p.items() if k not in ("digest_q", "transfer")}
        return web.Response(text=json.dumps({"foretape_desk": 1, "exported": time.time(), "desk": data}, indent=1), content_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="foretape-desk.json"'})  # fmt: skip

    async def api_day(self, request: web.Request) -> web.Response:
        from ..dossier import day_timeline

        return _json(day_timeline(self.engine))

    async def api_sources(self, request: web.Request) -> web.Response:
        from ..dossier import source_record

        return _json(source_record(self.engine))

    async def api_week(self, request: web.Request) -> web.Response:
        from ..personal import week_card

        dev = self._device(request)
        if not dev:
            raise web.HTTPBadRequest(text="X-Device header (the app's device id) needed")
        return _json(week_card(self.engine, dev) or {})

    async def api_why(self, request: web.Request) -> web.Response:
        """Look for why a stock is moving, now (EDGAR + news search, rate-limited per stock)."""
        from ..desk import clean_symbol

        sym = clean_symbol(request.match_info["sym"])
        if not sym:
            raise web.HTTPBadRequest(text="not a symbol")
        return _json(await self.engine.find_why(sym))

    async def api_ics(self, request: web.Request) -> web.Response:
        """The catalyst calendar (and this phone's earnings dates) for a calendar app to subscribe to:
        ``webcal://host/api/calendar.ics?token=…&device=…``."""
        from ..personal import calendar_ics

        return web.Response(
            text=calendar_ics(self.engine, self._device(request)),
            content_type="text/calendar",
            charset="utf-8",
            headers={"Content-Disposition": 'inline; filename="foretape.ics"', "Cache-Control": "no-store"},
        )

    async def api_record(self, request: web.Request) -> web.Response:
        from ..dossier import track_record

        try:
            days = max(1.0, min(90.0, float(request.query.get("days", "30"))))
        except ValueError:
            days = 30.0
        from ..dossier import priced_in_record, time_of_day_record

        out = track_record(self.engine, days)
        out["priced_in"] = priced_in_record(self.engine, days)["rows"]
        out["by_time"] = time_of_day_record(self.engine, days)["rows"]
        from ..dossier import sympathy_record

        out["sympathy"] = sympathy_record(self.engine)
        from ..catalysts import catalyst_table

        out["catalysts"] = catalyst_table(self.engine.catalyst_rates, regimes=self.engine.regime_rates)
        out["regime_now"] = self.engine.regime_label()
        from ..odds import report

        out["odds"] = report(self.engine.odds)
        from ..catalysts import retest_record

        out["retest"] = retest_record(self.engine)
        from ..catalysts import divergence_record

        out["diverge"] = divergence_record(self.engine)
        from ..catalysts import confirm_record

        out["confirm"] = confirm_record(self.engine)
        from ..stakes import event_record

        out["events"] = event_record(self.engine)
        return _json(out)

    async def api_foretape(self, request: web.Request) -> web.Response:
        """For the Setup page: the link (and a QR code) that opens Foretape on a phone."""
        wp = self.engine.webpush
        url = self.public_base(request) + "/app/" + self._token_qs()
        qr = ""
        try:
            import segno  # ships with the WhatsApp extra (the Docker image has it)

            qr = segno.make_qr(url, error="m").svg_inline(scale=4, border=2, dark="#000", light="#fff")
        except ImportError:
            pass
        return _json(
            {
                "enabled": wp is not None,
                "url": url,
                "qr_svg": qr,
                "devices": wp.describe()["devices"] if wp is not None else [],  # type: ignore[attr-defined]
                "durable": bool(wp is not None and wp.state is not None),  # type: ignore[attr-defined]
            }
        )

    async def api_push_subscribe(self, request: web.Request) -> web.Response:
        wp = self._push()
        self._check_write(request)
        try:
            body = await request.json()
            sub = await wp.subscribe(
                body.get("subscription") or {}, str(body.get("label") or ""), self._device(request)
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise web.HTTPBadRequest(text=f"invalid subscription: {exc}") from None
        return _json({"ok": True, "service": sub.service, "devices": len(wp.subs)})

    async def api_push_unsubscribe(self, request: web.Request) -> web.Response:
        wp = self._push()
        self._check_write(request)
        body = await request.json()
        return _json({"removed": await wp.unsubscribe(str(body.get("endpoint", "")))})

    async def api_push_test(self, request: web.Request) -> web.Response:
        wp = self._push()
        self._check_write(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        only = [body["endpoint"]] if body.get("endpoint") else None
        payload = {
            "id": "test",
            "title": "🔴 Foretape test: alerts will arrive like this",
            "body": "Market-moving news will look like this: headline, tickers, how fast it was caught.\n"
            "If you can read this on your lock screen, you're set.",
            "severity": "CRITICAL",
            "kind": "system",
            "tag": f"test-{int(time.time())}",
            "ts": time.time(),
        }
        results = await wp.push(payload, urgency="high", only=only)
        return _json(
            {"sent": sum(1 for r in results.values() if r == "ok"), "results": list(results.values())}
        )

    async def api_control(self, request: web.Request) -> web.Response:
        self._check_write(request)
        body = await request.json()
        reply = await self.engine.handle_phone_command("app", str(body.get("command", "")))
        if reply is None:
            raise web.HTTPBadRequest(
                text="unknown command (try: pause 2h, stop, resume, critical, normal, more)"
            )
        return _json({"reply": reply, "phone_mode": self.engine.phone_mode()})

    async def public_page(self, request: web.Request) -> web.Response:
        from .public_pages import about_page, contact_email, privacy_page, terms_page

        contact = contact_email(self.engine.cfg.general.user_agent, os.environ.get("CONTACT_EMAIL", ""))
        page = {"/about": about_page, "/privacy": privacy_page, "/terms": terms_page}[request.path](contact)
        return web.Response(text=page, content_type="text/html")

    async def health(self, request: web.Request) -> web.Response:
        st = self.engine.status()
        ok_sources = sum(1 for s in st["sources"] if s["status"] in ("ok", "starting"))
        return _json(
            {
                "ok": True,
                "uptime_s": round(st["uptime_s"]),
                "sources_ok": ok_sources,
                "sources": len(st["sources"]),
                "version": __version__,
                # which code is live (Render sets RENDER_GIT_COMMIT): compare with the latest commit on GitHub
                "commit": (os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT") or "")[:7],
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
