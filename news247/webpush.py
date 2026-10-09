"""Standard Web Push (the protocol behind browser and home-screen-app notifications).

Sending a push needs no account anywhere: the phone's browser hands us a *subscription* (an
endpoint at Apple's, Google's or Mozilla's push service plus two keys), and we POST an encrypted
message to that endpoint, signed with our own VAPID key:

* RFC 8291 message encryption ("aes128gcm" content coding, RFC 8188): only the phone can read it;
* RFC 8292 VAPID: an ES256-signed JWT proves the message comes from the server the phone
  subscribed to.

Everything is implemented here on top of ``cryptography``; the encryption is checked against
the RFC 8291 test vector in the tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096
MAX_PAYLOAD = RECORD_SIZE - 16 - 1 - 86  # tag, delimiter, header: what fits in one record


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def public_bytes(key: ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey) -> bytes:
    pub = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def private_from_bytes(raw: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def private_to_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_numbers().private_value.to_bytes(32, "big")


def derive_vapid_key(secret: str) -> ec.EllipticCurvePrivateKey:
    """A stable VAPID key from a secret (e.g. the dashboard token), so phones stay subscribed
    across restarts on hosts without a persistent disk."""
    n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551  # P-256 group order
    seed = _hmac(b"foretape-vapid-v1", secret.encode())
    value = int.from_bytes(_hmac(seed, b"\x01") + _hmac(seed, b"\x02"), "big") % (n - 1) + 1
    return ec.derive_private_key(value, ec.SECP256R1())


# --------------------------------------------------------------------------- RFC 8291 encryption


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Encrypt a push message body for one subscription (aes128gcm, a single record)."""
    if len(plaintext) > MAX_PAYLOAD:
        raise ValueError(f"push payload too large ({len(plaintext)} > {MAX_PAYLOAD} bytes)")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = public_bytes(as_private)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    # RFC 8291 section 3.4: combine the ECDH secret with the subscription's auth secret
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    # RFC 8188: content encryption key and nonce
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # 0x02 = last record
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """The receiving side (what the phone does). Used by tests and the self-check."""
    salt, idlen = body[:16], body[20]
    as_public, ciphertext = body[21 : 21 + idlen], body[21 + idlen :]
    ua_public = public_bytes(ua_private)
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ecdh_secret = ua_private.exchange(ec.ECDH(), as_key)
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
    return padded.rstrip(b"\x00")[:-1]  # drop padding and the record delimiter


# --------------------------------------------------------------------------- RFC 8292 VAPID


def vapid_header(endpoint: str, key: ec.EllipticCurvePrivateKey, subject: str, ttl_s: int = 12 * 3600) -> str:
    """The Authorization header for one push service (audience = the endpoint's origin)."""
    u = urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl_s, "sub": subject}
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (
        b64u(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64u(json.dumps(claims, separators=(",", ":")).encode())
    )
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={b64u(public_bytes(key))}"


@dataclass
class Subscription:
    endpoint: str
    p256dh: str
    auth: str
    label: str = ""
    created: float = 0.0
    last_ok: float | None = None
    failures: int = 0

    @classmethod
    def from_browser(cls, data: dict[str, Any], label: str = "") -> Subscription:
        """From PushSubscription.toJSON(): {endpoint, keys: {p256dh, auth}}."""
        keys = data.get("keys") or {}
        endpoint, p256dh, auth = (
            str(data.get("endpoint", "")),
            str(keys.get("p256dh", "")),
            str(keys.get("auth", "")),
        )
        if not endpoint.startswith("https://") or not p256dh or not auth:
            raise ValueError("not a valid push subscription")
        if len(unb64u(p256dh)) != 65 or len(unb64u(auth)) != 16:
            raise ValueError("push subscription keys have the wrong size")
        return cls(endpoint, p256dh, auth, label[:80], time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "p256dh": self.p256dh,
            "auth": self.auth,
            "label": self.label,
            "created": self.created,
            "last_ok": self.last_ok,
            "failures": self.failures,
        }

    @property
    def service(self) -> str:
        host = urlsplit(self.endpoint).hostname or ""
        if "apple" in host:
            return "Apple (iPhone/iPad/Mac)"
        if "google" in host or "fcm" in host:
            return "Google (Android/Chrome)"
        if "mozilla" in host:
            return "Mozilla (Firefox)"
        if "windows" in host or "microsoft" in host:
            return "Microsoft (Edge)"
        return host


def build_request(
    sub: Subscription,
    payload: dict[str, Any],
    key: ec.EllipticCurvePrivateKey,
    subject: str,
    *,
    ttl_s: int = 1800,
    urgency: str = "high",
    topic: str = "",
) -> tuple[bytes, dict[str, str]]:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    body = encrypt(data, unb64u(sub.p256dh), unb64u(sub.auth))
    headers = {
        "Authorization": vapid_header(sub.endpoint, key, subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl_s),
        "Urgency": urgency,
    }
    if topic:  # a newer push with the same topic replaces an undelivered older one
        headers["Topic"] = "".join(c for c in topic if c.isalnum() or c in "-_")[:32]
    return body, headers
