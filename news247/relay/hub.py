"""The monitor's side of the iMessage relay.

Alerts for the ``relay`` channel are handed to whichever Mac relay is connected. The hub waits
for the relay to *accept* the message (a few seconds; a sleeping Mac fails this fast, so backup
channels kick in right away) and then for the *result* of the send. If no relay is online the
message is queued: when the Mac wakes up and reconnects it gets what it missed, one by one with
a "late" note, or as a single digest when several piled up. Messages older than ``max_late``
expire, and ones a backup channel already delivered are not repeated.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from .. import __version__
from ..util import fmt_age
from .protocol import (
    CLOSE_BAD_SECRET,
    CLOSE_PROTOCOL,
    CLOSE_REPLACED,
    MIN_SECRET_LEN,
    PROTOCOL_VERSION,
    Channel,
    ProtocolError,
    check_proof,
    new_nonce,
    new_secret,
    proof,
    session_key,
)

if TYPE_CHECKING:
    from ..storage import Storage

log = logging.getLogger(__name__)

SEVERITY_EMOJI = {"LOW": "⚪", "MEDIUM": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}
SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
OUTBOX_KEEP = 500


class RelayUnavailable(RuntimeError):
    """No relay is connected (the message may have been queued)."""


class RelayFailed(RuntimeError):
    """A relay tried and failed (e.g. Messages is signed out)."""


class _AcceptTimeout(Exception):
    pass


def _norm(handle: str) -> str:
    h = str(handle).strip().lower()
    if "@" in h:
        return h
    digits = "".join(c for c in h if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


@dataclass
class OutMsg:
    id: str
    to: list[str]
    text: str
    title: str = ""
    severity: str = "HIGH"
    created: float = field(default_factory=time.time)
    status: str = "new"  # new|queued|sending|sent|delivered|failed|unknown|superseded|expired
    late_ok: bool = True  # may be delivered late when no relay is online
    attempts: int = 0
    error: str = ""
    relay: str = ""
    sent_at: float | None = None
    delivered_at: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OutMsg:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class RelayConnection:
    """One authenticated Mac relay."""

    def __init__(self, ws: web.WebSocketResponse, chan: Channel, info: dict[str, Any], remote: str) -> None:
        self.ws = ws
        self.chan = chan
        self.info = info
        self.relay_id = str(info.get("id") or uuid.uuid4().hex)
        self.name = str(info.get("name") or remote or "relay")[:80]
        self.remote = remote
        self.connected_at = time.time()
        self.last_seen = self.connected_at
        self.sent = 0
        self.failed = 0
        self.pending: dict[str, tuple[asyncio.Future[Any], asyncio.Future[Any]]] = {}
        self.closing = False

    async def post(self, msg: dict[str, Any]) -> None:
        await self.ws.send_str(json.dumps(self.chan.seal(msg), ensure_ascii=False))

    async def request_send(
        self, rec: OutMsg, text: str, to: list[str], accept_timeout: float, result_timeout: float
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        accepted: asyncio.Future[Any] = loop.create_future()
        result: asyncio.Future[Any] = loop.create_future()
        self.pending[rec.id] = (accepted, result)
        try:
            await self.post(
                {
                    "type": "send",
                    "id": rec.id,
                    "to": to,
                    "text": text,
                    "severity": rec.severity,
                    "created": rec.created,
                    # the hub stops waiting after accept_timeout; a relay that only reads this
                    # later (it was asleep) must not send it: a backup may already have
                    "issued": time.time(),
                }
            )
            try:
                await asyncio.wait_for(asyncio.shield(accepted), accept_timeout)
            except asyncio.TimeoutError:
                raise _AcceptTimeout() from None
            return await asyncio.wait_for(result, result_timeout)
        finally:
            self.pending.pop(rec.id, None)

    def on_message(self, msg: dict[str, Any]) -> None:
        self.last_seen = time.time()
        kind = msg["type"]
        futs = self.pending.get(str(msg.get("id", "")))
        if kind == "accepted" and futs and not futs[0].done():
            futs[0].set_result(True)
        elif kind == "result" and futs:
            if not futs[0].done():
                futs[0].set_result(True)
            if not futs[1].done():
                futs[1].set_result(msg)
        elif kind == "status":
            self.info.update(msg.get("info") or {})

    def fail_pending(self, exc: BaseException) -> None:
        for acc, res in self.pending.values():
            for fut in (acc, res):
                if not fut.done():
                    fut.set_exception(exc)

    def close_soon(self, code: int = 1000, reason: str = "") -> None:
        if not self.closing:
            self.closing = True
            asyncio.ensure_future(self.ws.close(code=code, message=reason.encode()[:120]))

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.relay_id,
            "remote": self.remote,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "sent": self.sent,
            "failed": self.failed,
            "macos": self.info.get("macos", ""),
            "version": self.info.get("version", ""),
            "service": self.info.get("service", ""),
            "receipts": bool(self.info.get("receipts")),
            "messages_ok": self.info.get("messages_ok"),
            "warnings": self.info.get("warnings", []),
        }


class RelayHub:
    def __init__(
        self,
        secret: str = "",
        *,
        accept_timeout: float = 5.0,
        result_timeout: float = 45.0,
        max_late_s: float = 3600.0,
        digest_min: int = 3,
        late_delivery: bool = True,
    ) -> None:
        self._secret = secret.strip()
        self.accept_timeout = accept_timeout
        self.result_timeout = result_timeout
        self.max_late_s = max_late_s
        self.digest_min = digest_min
        self.late_delivery = late_delivery
        self.connections: list[RelayConnection] = []
        self.outbox: OrderedDict[str, OutMsg] = OrderedDict()
        self.storage: Storage | None = None
        self.recipients: list[str] = []
        self.on_inbound: Callable[[str, str], Awaitable[str | None]] | None = None
        self.last_seen: float | None = None
        self.last_name = ""
        self.stats = {"sent": 0, "failed": 0, "queued": 0, "late": 0, "digests": 0, "delivered": 0}
        self.stats.update({"expired": 0, "rejected": 0, "inbound": 0})
        self.delivery_s: deque[float] = deque(maxlen=50)
        self._seen_inbound: deque[str] = deque(maxlen=200)
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ setup

    @property
    def secret(self) -> str:
        return self._secret

    def attach(self, storage: Storage) -> None:
        """Persist the outbox and, if no RELAY_SECRET was given, a generated secret."""
        self.storage = storage
        if not self._secret:
            self._secret = storage.get_setting("relay_secret") or ""
            if not self._secret:
                self._secret = new_secret()
                storage.set_setting("relay_secret", self._secret)
                log.info("generated a relay secret (shown on the dashboard's Setup page)")
        seen = storage.get_setting("relay_last_seen")
        if seen:
            self.last_seen = float(seen)
            self.last_name = storage.get_setting("relay_last_name") or ""
        for d in storage.relay_load(since=time.time() - max(self.max_late_s, 86400)):
            rec = OutMsg.from_dict(d)
            self.outbox[rec.id] = rec
        if len(self.secret) < MIN_SECRET_LEN:
            log.warning("RELAY_SECRET is short (%d chars); use 16+ random characters", len(self.secret))

    def set_recipients(self, numbers: list[str]) -> None:
        self.recipients = list(numbers)
        for conn in list(self.connections):
            self._spawn(self._push_config(conn))

    async def _push_config(self, conn: RelayConnection) -> None:
        with contextlib.suppress(Exception):
            await conn.post(
                {"type": "config", "recipients": self.recipients, "accept_timeout": self.accept_timeout}
            )

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ outbox

    def _save(self, rec: OutMsg) -> None:
        self.outbox[rec.id] = rec
        self.outbox.move_to_end(rec.id)
        while len(self.outbox) > OUTBOX_KEEP:
            self.outbox.popitem(last=False)
        if self.storage is not None:
            with contextlib.suppress(Exception):
                self.storage.relay_save(rec.to_dict())

    def new_message(
        self,
        msg_id: str,
        to: list[str],
        text: str,
        *,
        title: str = "",
        severity: str = "HIGH",
        late_ok: bool = True,
    ) -> OutMsg:
        if msg_id in self.outbox:
            msg_id = f"{msg_id}-{uuid.uuid4().hex[:6]}"
        return OutMsg(id=msg_id, to=list(to), text=text, title=title, severity=severity, late_ok=late_ok)

    def supersede(self, msg_id: str, by: str) -> None:
        """A backup channel delivered this alert: don't send it again when the relay returns."""
        rec = self.outbox.get(msg_id)
        if rec is not None and rec.status == "queued":
            rec.status, rec.note = "superseded", f"delivered by {by} instead"
            self._save(rec)

    def queued(self) -> list[OutMsg]:
        return [r for r in self.outbox.values() if r.status == "queued"]

    # ------------------------------------------------------------------ sending

    def live(self) -> list[RelayConnection]:
        """Connected relays, the one that most recently delivered first."""
        conns = [c for c in self.connections if not c.closing]
        return sorted(conns, key=lambda c: (c.failed == 0, c.sent, c.connected_at), reverse=True)

    def offline_reason(self) -> str:
        if self.last_seen:
            who = f" ({self.last_name})" if self.last_name else ""
            return f"iMessage relay offline: last connected {fmt_age(time.time() - self.last_seen)} ago{who}"
        return "no iMessage relay has connected yet: run the one-line installer from the Setup page on a Mac"

    async def deliver(self, rec: OutMsg, text: str | None = None) -> dict[str, Any]:
        """Send through a connected relay. Raises RelayUnavailable (queued if allowed) or RelayFailed."""
        text = rec.text if text is None else text
        to = rec.to or self.recipients
        if not to:
            raise RelayFailed("no phone number set: add IMESSAGE_TO or open the dashboard's Setup page")
        errors: list[str] = []
        unresponsive = 0
        self.outbox[rec.id] = rec  # registered before sending: a receipt can beat the result
        for conn in self.live():
            rec.attempts += 1
            rec.status = "sending"
            try:
                res = await conn.request_send(rec, text, to, self.accept_timeout, self.result_timeout)
            except _AcceptTimeout:
                unresponsive += 1
                errors.append(f"{conn.name} did not answer within {self.accept_timeout:.0f}s (asleep?)")
                conn.close_soon(reason="unresponsive")  # it reconnects (and gets the queue) when it wakes
                continue
            except asyncio.TimeoutError:
                rec.status = "unknown"
                rec.error = f"{conn.name} accepted the message but did not report back"
                self._save(rec)
                raise RelayFailed(rec.error) from None
            except (ConnectionError, RuntimeError) as exc:
                unresponsive += 1
                errors.append(f"{conn.name}: connection lost ({exc})")
                continue
            if res.get("ok"):
                conn.sent += 1
                if rec.status not in ("delivered", "failed"):  # a receipt may have raced ahead
                    rec.status = "sent"
                rec.relay, rec.sent_at, rec.error = (
                    conn.name,
                    time.time(),
                    rec.error if rec.status == "failed" else "",
                )
                if res.get("duplicate"):
                    rec.note = "already sent earlier"
                self.stats["sent"] += 1
                self._save(rec)
                return res
            conn.failed += 1
            errors.append(f"{conn.name}: {res.get('error') or 'send failed'}")
        if not errors or unresponsive == len(errors):  # nobody (awake) to send it
            reason = "; ".join(errors) if errors else self.offline_reason()
            if rec.late_ok and self.late_delivery:
                rec.status, rec.error = "queued", reason
                self.stats["queued"] += 1
                self._save(rec)
                raise RelayUnavailable(f"{reason} — queued until it reconnects")
            rec.status, rec.error = "failed", reason
            self._save(rec)
            raise RelayUnavailable(reason)
        rec.status, rec.error = "failed", "; ".join(errors)
        self.stats["failed"] += 1
        self._save(rec)
        raise RelayFailed(rec.error)

    async def flush(self, conn: RelayConnection | None = None) -> None:
        """Deliver what was queued while no relay was online."""
        now = time.time()
        fresh = []
        for rec in self.queued():
            if now - rec.created > self.max_late_s:
                rec.status, rec.note = "expired", f"relay was offline for over {fmt_age(self.max_late_s)}"
                self.stats["expired"] += 1
                self._save(rec)
            else:
                fresh.append(rec)
        if not fresh:
            return
        log.info("relay back online: delivering %d queued alert(s)", len(fresh))
        if len(fresh) >= self.digest_min:
            await self._send_digest(fresh, now)
            return
        for rec in fresh:
            age = now - rec.created
            text = rec.text if age < 60 else f"⏱ {fmt_age(age)} late (relay was offline)\n{rec.text}"
            if self.recipients:
                rec.to = list(self.recipients)
            try:
                await self.deliver(rec, text)
                self.stats["late"] += 1
            except (RelayUnavailable, RelayFailed) as exc:
                log.warning("late delivery failed: %s", exc)
                return

    async def _send_digest(self, recs: list[OutMsg], now: float) -> None:
        recs = sorted(recs, key=lambda r: r.created)
        lines = [f"📬 {len(recs)} alerts while your iMessage relay was offline:"]
        for r in recs[-10:]:
            title = r.title or r.text.split("\n", 1)[0]
            if title[:1] not in SEVERITY_EMOJI.values():
                title = f"{SEVERITY_EMOJI.get(r.severity, '•')} {title}"
            lines.append(f"{title[:140]} ({fmt_age(now - r.created)} ago)")
        if len(recs) > 10:
            lines.append(f"…and {len(recs) - 10} earlier. Details on the dashboard.")
        top = max(recs, key=lambda r: SEVERITY_RANK.get(r.severity, 0)).severity
        to = self.recipients or recs[-1].to
        digest = self.new_message(
            f"digest-{uuid.uuid4().hex[:10]}",
            to,
            "\n".join(lines),
            title=f"📬 digest of {len(recs)} queued alerts",
            severity=top,
            late_ok=False,
        )
        try:
            await self.deliver(digest)
        except (RelayUnavailable, RelayFailed) as exc:
            log.warning("digest delivery failed: %s", exc)
            return
        self.stats["digests"] += 1
        for r in recs:
            r.status, r.relay, r.sent_at, r.note = (
                "sent",
                digest.relay,
                digest.sent_at,
                f"in digest {digest.id}",
            )
            self._save(r)

    # ------------------------------------------------------------------ websocket endpoint

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        remote = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
        hub_nonce = new_nonce()
        await ws.send_json(
            {
                "type": "challenge",
                "v": PROTOCOL_VERSION,
                "nonce": hub_nonce,
                "server": f"news247/{__version__}",
                "time": time.time(),  # lets the relay correct for clock differences
            }
        )
        try:
            first = await ws.receive(timeout=15)
            hello = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        except (asyncio.TimeoutError, ValueError, TypeError):
            hello = {}
        agent_nonce = hello.get("nonce") if isinstance(hello, dict) else None
        if not (
            self.secret
            and isinstance(agent_nonce, str)
            and 16 <= len(agent_nonce) <= 128
            and hello.get("type") == "hello"
            and check_proof(proof(self.secret, "hello", hub_nonce, agent_nonce), hello.get("proof"))
        ):
            self.stats["rejected"] += 1
            log.warning("relay connection from %s rejected: wrong relay secret", remote or "?")
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "error": "wrong relay secret"})
            await ws.close(code=CLOSE_BAD_SECRET, message=b"wrong relay secret")
            return ws
        chan = Channel(session_key(self.secret, hub_nonce, agent_nonce))
        info = hello.get("info") if isinstance(hello.get("info"), dict) else {}
        conn = RelayConnection(ws, chan, dict(info), remote)
        await ws.send_json(
            {
                "type": "welcome",
                "v": PROTOCOL_VERSION,
                "proof": proof(self.secret, "welcome", agent_nonce, hub_nonce),
            }
        )
        for old in [c for c in self.connections if c.relay_id == conn.relay_id]:
            old.close_soon(CLOSE_REPLACED, "replaced by a new connection")
            self.connections.remove(old)
        self.connections.append(conn)
        self._mark_seen(conn)
        log.info("iMessage relay connected: %s (%s, macOS %s)", conn.name, remote, info.get("macos", "?"))
        await self._push_config(conn)
        flush = self._spawn(self.flush(conn))
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = chan.open(json.loads(m.data))
                except (ValueError, ProtocolError) as exc:
                    log.warning("relay %s sent an invalid message (%s); disconnecting", conn.name, exc)
                    await ws.close(code=CLOSE_PROTOCOL, message=b"invalid message")
                    break
                conn.on_message(msg)
                if msg["type"] == "receipt":
                    self._on_receipt(conn, msg)
                elif msg["type"] == "inbound":
                    self._spawn(self._on_inbound(msg))
        finally:
            conn.closing = True
            if conn in self.connections:
                self.connections.remove(conn)
            conn.fail_pending(ConnectionError("relay disconnected"))
            self._mark_seen(conn)
            if not flush.done():
                flush.cancel()
            log.info("iMessage relay disconnected: %s", conn.name)
        return ws

    def _mark_seen(self, conn: RelayConnection) -> None:
        self.last_seen, self.last_name = time.time(), conn.name
        if self.storage is not None:
            with contextlib.suppress(Exception):
                self.storage.set_setting("relay_last_seen", str(self.last_seen))
                self.storage.set_setting("relay_last_name", self.last_name)

    def _on_receipt(self, conn: RelayConnection, msg: dict[str, Any]) -> None:
        rec = self.outbox.get(str(msg.get("id", "")))
        status = msg.get("status")
        if rec is None or status not in ("delivered", "failed", "sent"):
            return
        if status == "delivered":
            rec.status = "delivered"
            rec.delivered_at = msg.get("delivered_at") or time.time()
            if rec.created and rec.delivered_at:
                self.delivery_s.append(max(0.0, float(rec.delivered_at) - rec.created))
            self.stats["delivered"] += 1
        elif status == "failed":
            rec.status = "failed"
            rec.error = str(msg.get("error") or f"Messages reported error {msg.get('error_code')}")
            conn.failed += 1
            log.warning("relay %s: message %s was not delivered: %s", conn.name, rec.id, rec.error)
        self._save(rec)

    async def _on_inbound(self, msg: dict[str, Any]) -> None:
        sender, text = str(msg.get("from", "")), str(msg.get("text", "")).strip()
        guid = str(msg.get("guid") or f"{sender}|{msg.get('at')}|{text}")
        if guid in self._seen_inbound or not text:
            return
        self._seen_inbound.append(guid)
        if _norm(sender) not in {_norm(r) for r in self.recipients}:
            log.info("ignoring a text from %s (not one of the alert recipients)", sender)
            return
        self.stats["inbound"] += 1
        if self.on_inbound is None:
            return
        try:
            reply = await self.on_inbound(sender, text)
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the connection
            log.warning("phone command %r failed: %s", text, exc)
            reply = f"Sorry, that failed: {exc}"
        if reply:
            rec = self.new_message(
                f"reply-{uuid.uuid4().hex[:10]}",
                [sender],
                reply,
                title=f"↩ reply to “{text[:40]}”",
                late_ok=False,
            )
            with contextlib.suppress(RelayUnavailable, RelayFailed):
                await self.deliver(rec)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        lat = sorted(self.delivery_s)
        return {
            "online": bool(self.live()),
            "relays": [c.describe() for c in self.connections],
            "last_seen": self.last_seen,
            "last_name": self.last_name,
            "offline_reason": "" if self.live() else self.offline_reason(),
            "queued": len(self.queued()),
            "stats": dict(self.stats),
            "delivery_median_s": lat[len(lat) // 2] if lat else None,
            "recent": [
                {
                    k: r.to_dict()[k]
                    for k in (
                        "id",
                        "title",
                        "status",
                        "created",
                        "sent_at",
                        "delivered_at",
                        "error",
                        "note",
                        "relay",
                    )
                }
                for r in list(self.outbox.values())[-15:][::-1]
            ],
        }

    async def close(self) -> None:
        for conn in list(self.connections):
            with contextlib.suppress(Exception):
                await conn.ws.close(code=1001, message=b"server shutting down")
        for task in list(self._tasks):
            task.cancel()
