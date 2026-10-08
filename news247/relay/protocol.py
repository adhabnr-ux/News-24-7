"""Wire protocol between the News247 monitor (the hub) and a Mac relay (the agent).

Everything travels over one WebSocket that the *Mac* opens to the monitor, so the Mac needs no
open port, tunnel or fixed address. Both sides prove they know the shared relay secret without
ever sending it:

    hub   -> {"type": "challenge", "nonce": Ns}
    agent -> {"type": "hello", "nonce": Na, "proof": HMAC(secret, "hello|Ns|Na"), "info": {...}}
    hub   -> {"type": "welcome", "proof": HMAC(secret, "welcome|Na|Ns"), ...}

A relay with the wrong secret is refused, and a relay pointed at the wrong server refuses to
send anything (the server can't produce the welcome proof). After the handshake every message in
both directions carries ``seq`` (strictly increasing) and ``mac`` = HMAC(session key, message),
where the session key is derived from the secret and both nonces, so messages can't be forged,
altered or replayed, even by something sitting between the two (TLS normally prevents that anyway).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

PROTOCOL_VERSION = 1
WS_PATH = "/relay/ws"
MIN_SECRET_LEN = 16

# WebSocket close codes (4000-4999 are application-defined)
CLOSE_BAD_SECRET = 4001
CLOSE_PROTOCOL = 4002
CLOSE_REPLACED = 4003


class ProtocolError(Exception):
    """A message failed authentication or was malformed."""


def new_nonce() -> str:
    return secrets.token_hex(16)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def proof(secret: str, label: str, first: str, second: str) -> str:
    return hmac.new(secret.encode(), f"{label}|{first}|{second}".encode(), hashlib.sha256).hexdigest()


def check_proof(expected: str, supplied: Any) -> bool:
    return isinstance(supplied, str) and hmac.compare_digest(expected.encode(), supplied.encode())


def session_key(secret: str, hub_nonce: str, agent_nonce: str) -> bytes:
    return hmac.new(
        secret.encode(), f"news247-relay-session|{hub_nonce}|{agent_nonce}".encode(), hashlib.sha256
    ).digest()


def canonical(msg: dict[str, Any]) -> bytes:
    body = {k: v for k, v in msg.items() if k != "mac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class Channel:
    """Seals outgoing and opens incoming messages for one authenticated session."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self.seq_out = 0
        self.seq_in = 0

    def seal(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.seq_out += 1
        out = {**msg, "seq": self.seq_out}
        out["mac"] = hmac.new(self.key, canonical(out), hashlib.sha256).hexdigest()
        return out

    def open(self, msg: Any) -> dict[str, Any]:
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise ProtocolError("malformed message")
        mac = msg.get("mac")
        want = hmac.new(self.key, canonical(msg), hashlib.sha256).hexdigest()
        if not isinstance(mac, str) or not hmac.compare_digest(want.encode(), mac.encode()):
            raise ProtocolError(f"bad signature on '{msg['type']}' message")
        seq = msg.get("seq")
        if not isinstance(seq, int) or seq <= self.seq_in:
            raise ProtocolError(f"replayed or out-of-order message (seq {seq!r} after {self.seq_in})")
        self.seq_in = seq
        return msg


def ws_url(server: str) -> str:
    """'https://news247.onrender.com/' -> 'wss://news247.onrender.com/relay/ws'."""
    base = server.strip().rstrip("/")
    if "://" not in base:
        base = "https://" + base
    scheme, rest = base.split("://", 1)
    scheme = {"https": "wss", "http": "ws"}.get(scheme.lower(), scheme.lower())
    if rest.endswith(WS_PATH):
        rest = rest[: -len(WS_PATH)]
    return f"{scheme}://{rest}{WS_PATH}"
