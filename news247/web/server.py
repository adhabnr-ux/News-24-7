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

from .. import __version__
from ..config import WebConfig

if TYPE_CHECKING:
    from ..engine import Engine

log = logging.getLogger(__name__)


# no dashboard token: health checks, endpoints that authenticate themselves (relays, Meta's webhook)
# and the public pages Meta requires (business website, privacy policy, terms)
PUBLIC_PATHS = {"/health", "/relay/ws", "/webhooks/whatsapp", "/about", "/privacy", "/terms"}


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
        app.router.add_get("/api/calendar", self.api_calendar)
        app.router.add_get("/api/watch", self.api_watch)
        app.router.add_get("/api/radar", self.api_radar)
        app.router.add_get("/api/alert/{id}", self.api_alert)
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
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        # these authenticate themselves: relays (challenge-response), Meta (verify token/signature)
        # /app/* is the Foretape shell (no data in it); its data comes from token-protected APIs
        is_app = request.path == "/app" or request.path.startswith("/app/")
        if self.cfg.token and request.path not in PUBLIC_PATHS and not is_app:
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
            }
        )

    async def api_brief(self, request: web.Request) -> web.Response:
        return _json(self.engine.brief())

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
        alert["series"] = self.engine.price_series(alert)
        return _json(alert)

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
            sub = await wp.subscribe(body.get("subscription") or {}, str(body.get("label") or ""))
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
