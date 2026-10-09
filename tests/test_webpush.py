"""Foretape: standard Web Push to the home-screen app (RFC 8291 encryption, RFC 8292 VAPID), the
push channel, durable subscriptions, and the /app + /api/push endpoints."""

from __future__ import annotations

import json
import time
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from news247 import webpush as wp
from news247.config import build_config
from news247.engine import Engine
from news247.models import Alert, Analysis, NewsItem, Severity, SourceTier
from news247.notify.channels import WebPushNotifier
from news247.storage import Storage
from news247.web.server import WebServer

# RFC 8291 Appendix A
RFC_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
    "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


class Phone:
    """A browser's side of a push subscription: its own keys, able to read what it is sent."""

    def __init__(self, endpoint: str) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.endpoint = endpoint

    def browser_json(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": wp.b64u(wp.public_bytes(self.key)), "auth": wp.b64u(self.auth)},
        }

    def subscription(self) -> wp.Subscription:
        d = self.browser_json()
        return wp.Subscription(self.endpoint, d["keys"]["p256dh"], d["keys"]["auth"], "test", time.time())

    def read(self, body: bytes) -> dict[str, Any]:
        return json.loads(wp.decrypt(body, self.key, self.auth))


def verify_vapid(header: str, endpoint_origin: str) -> dict[str, Any]:
    """What a push service does with the Authorization header; returns the JWT claims."""
    assert header.startswith("vapid t=")
    t_part, k_part = header.removeprefix("vapid ").split(", ")
    jwt, key = t_part.removeprefix("t="), k_part.removeprefix("k=")
    head, claims, sig = jwt.split(".")
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.unb64u(key))
    raw = wp.unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if forged
    assert json.loads(wp.unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    c = json.loads(wp.unb64u(claims))
    assert c["aud"] == endpoint_origin and c["exp"] > time.time()
    c["k"] = key
    return c


# --------------------------------------------------------------------------- protocol


def test_encryption_matches_rfc8291_vector():
    body = wp.encrypt(
        RFC_PLAINTEXT,
        wp.unb64u(RFC_UA_PUBLIC),
        wp.unb64u(RFC_AUTH),
        as_private=wp.private_from_bytes(wp.unb64u(RFC_AS_PRIVATE)),
        salt=wp.unb64u(RFC_SALT),
    )
    assert wp.b64u(body) == RFC_BODY
    ua = wp.private_from_bytes(wp.unb64u(RFC_UA_PRIVATE))
    assert wp.b64u(wp.public_bytes(ua)) == RFC_UA_PUBLIC
    assert wp.decrypt(wp.unb64u(RFC_BODY), ua, wp.unb64u(RFC_AUTH)) == RFC_PLAINTEXT


def test_round_trip_and_size_limit():
    phone = Phone("https://push.example/x")
    msg = "🔴 ▼ NVDA ORCL".encode() * 10
    body = wp.encrypt(msg, wp.public_bytes(phone.key), phone.auth)
    assert wp.decrypt(body, phone.key, phone.auth) == msg
    assert len(body) == 16 + 4 + 1 + 65 + len(msg) + 1 + 16  # header + record + delimiter + tag
    with pytest.raises(ValueError, match="too large"):
        wp.encrypt(b"x" * (wp.MAX_PAYLOAD + 1), wp.public_bytes(phone.key), phone.auth)


def test_vapid_header_is_a_valid_es256_jwt():
    key = wp.derive_vapid_key("secret")
    claims = verify_vapid(
        wp.vapid_header("https://web.push.apple.com/QGx/abc", key, "mailto:me@example.com"),
        "https://web.push.apple.com",
    )
    assert claims["sub"] == "mailto:me@example.com" and claims["exp"] <= time.time() + 24 * 3600
    assert claims["k"] == wp.b64u(wp.public_bytes(key))
    # a different key cannot pass as ours
    other = wp.vapid_header("https://web.push.apple.com/x", wp.derive_vapid_key("other"), "mailto:x@y")
    forged = other.split(", ")[0] + ", k=" + claims["k"]
    with pytest.raises(InvalidSignature):
        verify_vapid(forged, "https://web.push.apple.com")


def test_vapid_key_is_stable_per_secret():
    a1, a2, b = wp.derive_vapid_key("tok"), wp.derive_vapid_key("tok"), wp.derive_vapid_key("tok2")
    assert wp.public_bytes(a1) == wp.public_bytes(a2) != wp.public_bytes(b)
    raw = wp.private_to_bytes(a1)
    assert len(raw) == 32 and wp.public_bytes(wp.private_from_bytes(raw)) == wp.public_bytes(a1)


def test_subscription_validation_and_services():
    phone = Phone("https://web.push.apple.com/QGx")
    sub = wp.Subscription.from_browser(phone.browser_json(), "iPhone " * 40)
    assert sub.service.startswith("Apple") and len(sub.label) == 80
    assert wp.Subscription.from_browser(
        {**phone.browser_json(), "endpoint": "https://fcm.googleapis.com/fcm/send/x"}
    ).service.startswith("Google")
    for bad in (
        {},
        {**phone.browser_json(), "endpoint": "http://insecure.example/x"},
        {"endpoint": "https://x.example", "keys": {"p256dh": "AAAA", "auth": wp.b64u(phone.auth)}},
        {"endpoint": "https://x.example", "keys": {"p256dh": phone.browser_json()["keys"]["p256dh"]}},
    ):
        with pytest.raises(ValueError):
            wp.Subscription.from_browser(bad)


def test_build_request_headers():
    phone = Phone("https://updates.push.services.mozilla.com/wpush/v2/abc")
    body, headers = wp.build_request(
        phone.subscription(),
        {"title": "hi"},
        wp.derive_vapid_key("k"),
        "mailto:a@b.c",
        ttl_s=600,
        urgency="normal",
        topic="nvda:earnings/q3 beat!",
    )
    assert phone.read(body) == {"title": "hi"}
    assert headers["Content-Encoding"] == "aes128gcm" and headers["TTL"] == "600"
    assert headers["Urgency"] == "normal" and headers["Topic"] == "nvdaearningsq3beat"
    verify_vapid(headers["Authorization"], "https://updates.push.services.mozilla.com")


# --------------------------------------------------------------------------- the channel


def alert(sev: Severity = Severity.CRITICAL) -> Alert:
    item = NewsItem(
        source="fhfa-x",
        title="PULTE: FANNIE AND FREDDIE MOVING TO ONE PRICING GRID WITH VANTAGESCORE",
        url="https://x.com/pulte/status/1",
        tier=SourceTier.PRIMARY,
        published=time.time() - 3,
    )
    an = Analysis(score=91, severity=sev, tickers=["FICO"], direction="down", summary="Moat at risk")
    return Alert(
        kind="news",
        severity=sev,
        title=item.title,
        body="",
        url=item.url,
        tickers=["FICO"],
        item=item,
        analysis=an,
    )


class FakeState:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.puts = 0

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def put(self, key: str, value: bytes) -> bool:
        self.data[key], self.puts = value, self.puts + 1
        return True


def channel(tmp_path: Any, http: Any, state: Any = None, name: str = "a.db") -> WebPushNotifier:
    ch = WebPushNotifier({"ttl_s": 900}, http, Severity.HIGH)
    ch.attach(Storage(tmp_path / name), secret="tok", state=state, subject="mailto:owner@example.com")
    return ch


async def test_alert_reaches_the_phone_encrypted_and_signed(tmp_path, server, http):
    server.on("/push/phone1", (201, ""))
    phone = Phone(server.url("/push/phone1"))
    ch = channel(tmp_path, http)
    ch.subs[phone.endpoint] = phone.subscription()
    a = alert()
    await ch.send(a)
    req = server.requests[-1]
    shown = phone.read(req["body"])
    assert shown["title"].startswith("🔴") and "ONE PRICING GRID" in shown["title"]
    assert shown["severity"] == "CRITICAL" and shown["tickers"] == ["FICO"] and shown["id"] == a.id
    assert shown["url"] == "https://x.com/pulte/status/1" and "Moat at risk" in shown["body"]
    h = req["headers"]
    assert h["Urgency"] == "high" and h["TTL"] == "900" and h["Content-Encoding"] == "aes128gcm"
    origin = server.url("").rstrip("/")
    claims = verify_vapid(h["Authorization"], origin)
    assert claims["k"] == ch.public_key and claims["sub"] == "mailto:owner@example.com"
    assert ch.subs[phone.endpoint].last_ok and ch.last_sent


async def test_medium_alerts_use_normal_urgency(tmp_path, server, http):
    server.on("/push/p", (201, ""))
    phone = Phone(server.url("/push/p"))
    ch = channel(tmp_path, http)
    ch.subs[phone.endpoint] = phone.subscription()
    await ch.send(alert(Severity.MEDIUM))
    assert server.requests[-1]["headers"]["Urgency"] == "normal"


async def test_no_phone_yet_is_an_explained_failure(tmp_path, http):
    with pytest.raises(RuntimeError, match="Turn on alerts"):
        await channel(tmp_path, http).send(alert())


async def test_gone_phones_are_removed_and_others_still_get_it(tmp_path, server, http):
    server.on("/push/gone", (410, "expired"))
    server.on("/push/ok", (201, ""))
    server.on("/push/broken", (500, "<html>oops</html>"))
    state = FakeState()
    ch = channel(tmp_path, http, state)
    phones = [Phone(server.url(f"/push/{n}")) for n in ("gone", "ok", "broken")]
    for p in phones:
        ch.subs[p.endpoint] = p.subscription()
        ch.storage.push_save(p.subscription().to_dict())
    results = await ch.push(ch.payload(alert()))
    assert sorted(results.values()) == ["HTTP 500: oops", "gone", "ok"]
    assert phones[0].endpoint not in ch.subs and ch.removed == 1
    assert {d["endpoint"] for d in ch.storage.push_all()} == {phones[1].endpoint, phones[2].endpoint}
    assert ch.subs[phones[2].endpoint].failures == 1
    assert len(json.loads(state.data["push-subscriptions"])) == 2
    await ch.send(alert())  # one phone works: not a failure
    del ch.subs[phones[1].endpoint]
    with pytest.raises(RuntimeError, match="push failed: HTTP 500"):
        await ch.send(alert())


async def test_subscriptions_survive_a_fresh_container(tmp_path, http):
    state = FakeState()
    ch = channel(tmp_path, http, state)
    phone = Phone("https://web.push.apple.com/QGx")
    await ch.subscribe(phone.browser_json(), "iPhone")
    await ch.subscribe(phone.browser_json(), "iPhone")  # the app re-subscribes on every open
    assert state.puts == 1  # unchanged: no extra write (free databases may sleep)
    # Render wiped the disk: a new database, same STATE_DB and token
    fresh = channel(tmp_path, http, state, name="fresh.db")
    assert not fresh.subs
    await fresh.restore()
    assert list(fresh.subs) == [phone.endpoint] and fresh.storage.push_all()[0]["label"] == "iPhone"
    assert fresh.public_key == ch.public_key  # same key: the phone's subscription stays valid
    # local copy wins over the state database when present
    again = channel(tmp_path, http, FakeState(), name="fresh.db")
    await again.restore()
    assert list(again.subs) == [phone.endpoint]
    assert await again.unsubscribe(phone.endpoint) and not await again.unsubscribe(phone.endpoint)


async def test_key_without_token_is_remembered(tmp_path, http):
    a = WebPushNotifier({}, http, Severity.HIGH)
    a.attach(Storage(tmp_path / "k.db"))
    b = WebPushNotifier({}, http, Severity.HIGH)
    b.attach(Storage(tmp_path / "k.db"))
    assert a.public_key == b.public_key
    fixed = wp.b64u(wp.private_to_bytes(wp.derive_vapid_key("x")))
    c = WebPushNotifier({"vapid_private": fixed}, http, Severity.HIGH)
    c.attach(Storage(tmp_path / "k.db"), secret="tok")
    assert c.public_key == wp.b64u(wp.public_bytes(wp.derive_vapid_key("x")))
    d = c.describe()
    assert d["devices"] == [] and d["durable"] is False


# --------------------------------------------------------------------------- the app + APIs


def push_engine(tmp_path: Any, **web_cfg: Any) -> Engine:
    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {"console": {"enabled": False}, "webpush": {"enabled": True, "min_severity": "high"}},
            "web": {"token": "tok", **web_cfg},
        }
    )
    return Engine(cfg, storage=Storage(tmp_path / "e.db"), sources=[])


@pytest.fixture
async def app_server(tmp_path):
    eng = push_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            yield eng, srv, s
    finally:
        await srv.close()
        await eng.http.close()


async def test_app_shell_is_public_and_installable(app_server):
    eng, srv, s = app_server
    async with s.get(srv.make_url("/app"), allow_redirects=False) as r:
        assert r.status == 302 and r.headers["Location"] == "/app/"
    async with s.get(srv.make_url("/app/")) as r:
        html = await r.text()
        assert r.status == 200 and "<title>Foretape</title>" in html and "manifest" in html
        assert "apple-touch-icon" in html and '<canvas id="scene"' in html
        assert "/app/app.js" in html and "/app/scene.js" in html and "/app/app.css" in html
    async with s.get(srv.make_url("/app/app.js")) as r:
        js = await r.text()
        assert r.status == 200 and r.content_type == "application/javascript"
        assert "Notification.requestPermission" in js and "/api/push/subscribe" in js
    async with s.get(srv.make_url("/app/scene.js")) as r:
        glsl = await r.text()
        assert r.status == 200 and "gl_FragColor" in glsl and "uDepth" in glsl
    async with s.get(srv.make_url("/app/app.css")) as r:
        assert r.status == 200 and r.content_type == "text/css" and "backdrop-filter" in await r.text()
    async with s.get(srv.make_url("/app/sw.js")) as r:
        assert r.status == 200 and r.headers["Service-Worker-Allowed"] == "/app/"
        sw = await r.text()
        assert "showNotification" in sw and "/app/launch.webp" in sw
    for name, ctype in (
        ("logo.svg", "image/svg+xml"),
        ("icon-192.png", "image/png"),
        ("badge-96.png", "image/png"),
        ("launch.webp", "image/webp"),
        ("depth.png", "image/png"),
        ("fonts/inter-normal.woff2", "font/woff2"),
        ("fonts/instrument-serif-italic.woff2", "font/woff2"),
    ):
        async with s.get(srv.make_url(f"/app/{name}")) as r:
            assert r.status == 200 and r.content_type == ctype, name
            assert len(await r.read()) > 500, name
    async with s.get(srv.make_url("/app/launch.webp")) as r:
        assert "immutable" in r.headers["Cache-Control"]  # the photo never changes for a given release
    async with s.get(srv.make_url("/app/app.js")) as r:
        assert r.headers["Cache-Control"] == "no-cache"  # the code always revalidates
    for bad in (
        "/app/index.py",
        "/app/nope.png",
        "/app/%2e%2e%2fserver.py",
        "/app/fonts/nope.woff2",
        "/app/fonts/%2e%2e%2f..%2fserver.py",
    ):
        async with s.get(srv.make_url(bad)) as r:
            assert r.status == 404, bad  # only the whitelisted shell files are served
    async with s.get(srv.make_url("/app/push-key")) as r:
        assert (await r.text()) == eng.webpush.public_key  # type: ignore[union-attr]
    # the shell holds no data: everything else still needs the token
    for path in ("/api/app", "/api/status", "/applesauce"):
        async with s.get(srv.make_url(path)) as r:
            assert r.status in (401,), path


async def test_manifest_starts_unlocked_only_with_the_right_token(app_server):
    _eng, srv, s = app_server
    async with s.get(srv.make_url("/app/manifest.webmanifest?token=tok")) as r:
        m = json.loads(await r.text())
        assert r.content_type == "application/manifest+json"
    assert m["name"] == "Foretape" and m["display"] == "standalone" and m["scope"] == "/app/"
    assert m["start_url"] == "/app/?token=tok"
    assert any(i["purpose"] == "maskable" for i in m["icons"])
    async with s.get(srv.make_url("/app/manifest.webmanifest?token=guess")) as r:
        assert json.loads(await r.text())["start_url"] == "/app/"
    # the shell itself already links the token-carrying manifest (iOS reads it at install time)
    async with s.get(srv.make_url("/app/?token=tok")) as r:
        assert 'href="/app/manifest.webmanifest?token=tok"' in await r.text()
    async with s.get(srv.make_url("/app/?token=guess")) as r:
        assert 'href="/app/manifest.webmanifest"' in await r.text()


async def test_subscribe_test_push_and_control_from_the_app(app_server, server):
    eng, srv, s = app_server
    auth = {"Authorization": "Bearer tok"}
    async with s.post(srv.make_url("/api/push/subscribe"), json={"subscription": {"endpoint": "x"}}) as r:
        assert r.status == 401  # no token
    async with s.post(
        srv.make_url("/api/push/subscribe"), json={"subscription": {"endpoint": "x"}}, headers=auth
    ) as r:
        assert r.status == 400 and "invalid subscription" in await r.text()
    apple = Phone("https://web.push.apple.com/QGx")
    async with s.post(
        srv.make_url("/api/push/subscribe"),
        json={"subscription": apple.browser_json(), "label": "iPhone"},
        headers=auth,
    ) as r:
        assert (await r.json()) == {"ok": True, "service": "Apple (iPhone/iPad/Mac)", "devices": 1}
    async with s.post(
        srv.make_url("/api/push/unsubscribe"), json={"endpoint": apple.endpoint}, headers=auth
    ) as r:
        assert (await r.json()) == {"removed": True}
    # a phone on a (local) push service: the test button really delivers
    server.on("/push/me", (201, ""))
    phone = Phone(server.url("/push/me"))
    eng.webpush.subs[phone.endpoint] = phone.subscription()  # type: ignore[union-attr]
    async with s.post(srv.make_url("/api/push/test"), headers=auth) as r:
        assert (await r.json()) == {"sent": 1, "results": ["ok"]}
    shown = phone.read(server.requests[-1]["body"])
    assert "Foretape test" in shown["title"] and shown["severity"] == "CRITICAL"
    # the app's data and controls
    async with s.get(srv.make_url("/api/app"), headers=auth) as r:
        st = await r.json()
    assert st["devices"] == 1 and st["phone_mode"] == "normal" and st["alerts"] == []
    async with s.post(srv.make_url("/api/control"), json={"command": "pause 2h"}, headers=auth) as r:
        out = await r.json()
    assert out["phone_mode"].startswith("paused until") and eng.dispatcher.paused
    async with s.post(srv.make_url("/api/control"), json={"command": "resume"}, headers=auth) as r:
        assert (await r.json())["phone_mode"] == "normal"
    async with s.post(srv.make_url("/api/control"), json={"command": "critical"}, headers=auth) as r:
        assert (await r.json())["phone_mode"] == "CRITICAL and above"
    async with s.post(srv.make_url("/api/control"), json={"command": "make me rich"}, headers=auth) as r:
        assert r.status == 400


async def test_engine_alert_goes_to_the_phone(tmp_path, server):
    server.on("/push/me", (201, ""))
    eng = push_engine(tmp_path)
    await eng.http.start()
    try:
        phone = Phone(server.url("/push/me"))
        eng.webpush.subs[phone.endpoint] = phone.subscription()  # type: ignore[union-attr]
        assert await eng.dispatcher.dispatch(alert(Severity.HIGH)) == {"webpush": "ok"}
        assert "PRICING GRID" in phone.read(server.requests[-1]["body"])["title"]
        assert await eng.dispatcher.dispatch(alert(Severity.MEDIUM)) == {}  # below the phone's floor
    finally:
        await eng.http.close()


async def test_push_can_be_switched_off(tmp_path):
    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "notify": {"console": {"enabled": False}, "webpush": {"enabled": False}},
            "web": {"token": "tok"},
        }
    )
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), sources=[])
    assert eng.webpush is None
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s, s.get(srv.make_url("/app/push-key")) as r:
            assert r.status == 404 and "WEBPUSH_ENABLED" in await r.text()
    finally:
        await srv.close()


async def test_setup_page_offers_the_foretape_link(tmp_path):
    eng = push_engine(tmp_path, public_url="https://news247.example.com")
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/foretape")) as r:
                assert r.status == 401
            async with s.get(srv.make_url("/api/foretape?token=tok")) as r:
                st = await r.json()
            assert st["enabled"] and st["url"] == "https://news247.example.com/app/?token=tok"
            assert st["devices"] == [] and st["durable"] is False
            assert st["qr_svg"].startswith("<svg")
            async with s.get(srv.make_url("/setup?token=tok")) as r:
                assert 'id="ft"' in await r.text()
    finally:
        await srv.close()
