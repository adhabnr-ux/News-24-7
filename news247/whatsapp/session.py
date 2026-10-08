"""A self-hosted WhatsApp sender that lives inside the News247 process.

It works like WhatsApp Web: News247 becomes a *linked device* of a WhatsApp account (paired
once with a QR code or an 8-character code), keeps an encrypted connection to WhatsApp's
servers, and sends your alerts from that account, with no phone, Mac or third-party service in
the middle. The protocol work is done by whatsmeow (Go, also behind mautrix-whatsapp) through
the ``neonize`` binding; this module adds what an alert system needs on top:

* a supervisor that reconnects with backoff and re-offers pairing after a logout,
* one-at-a-time, spaced-out sending with an hourly cap (protects the account),
* delivered/read receipts per alert, and the median time to your phone,
* texted commands ("pause 2h", "status"...) from the alert recipients only,
* a status snapshot (state, QR, pairing code, warnings) for the dashboard's Setup page.

The WhatsApp engine sits behind a tiny ``Backend`` interface so all of this is tested without
a network; ``NeonizeBackend`` is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# states shown on the Setup page
NOT_INSTALLED = "not_installed"
STOPPED = "stopped"
CONNECTING = "connecting"
PAIRING = "pairing"  # waiting for you to scan the QR / enter the code
CONNECTED = "connected"
DISCONNECTED = "disconnected"  # temporarily offline; the engine reconnects by itself
LOGGED_OUT = "logged_out"  # unlinked from the phone: pair again
BANNED = "banned"
ERROR = "error"


def digits(number: str) -> str:
    """'+1 (623) 555-0146' -> '16235550146' (US numbers without country code get a 1)."""
    d = re.sub(r"\D", "", str(number))
    return "1" + d if len(d) == 10 else d


class Backend(Protocol):
    """What the session needs from a WhatsApp engine."""

    async def start(self, events: Events) -> Awaitable[None]:
        """Connect; returns an awaitable that finishes when the engine stops (or fails)."""
        ...

    async def stop(self) -> None: ...

    async def pair_code(self, phone: str) -> str: ...

    async def send_text(self, to: str, text: str) -> str:
        """Send to a number (digits only); returns the message id."""
        ...

    async def logout(self) -> None: ...


@dataclass
class Events:
    """Callbacks a backend calls (from the event loop) as things happen."""

    qr: Callable[[list[str]], None]
    connected: Callable[[str, str], None]  # (own number, display name)
    paired: Callable[[str, str], None]  # (own number, business/push name)
    pair_failed: Callable[[str], None]
    disconnected: Callable[[str], None]  # (reason, may be "")
    logged_out: Callable[[str], None]
    banned: Callable[[str], None]
    receipt: Callable[[list[str], str, str], None]  # (message ids, "delivered"|"read"|..., from number)
    message: Callable[[str, str, str, bool], None]  # (from number, text, message id, from me)


@dataclass
class Sent:
    id: str
    to: str
    title: str
    sent_at: float
    status: str = "sent"  # sent -> delivered -> read
    delivered_at: float | None = None
    read_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            k: getattr(self, k) for k in ("id", "to", "title", "sent_at", "status", "delivered_at", "read_at")
        }


@dataclass
class SessionConfig:
    min_interval_s: float = 2.0  # spacing between messages
    max_per_hour: int = 60  # hard cap; WhatsApp flags accounts that blast messages
    connect_wait_s: float = 15.0  # how long a send waits for a (re)connection
    retry_min_s: float = 5.0
    retry_max_s: float = 300.0
    device_name: str = "News247"
    extra: dict[str, Any] = field(default_factory=dict)


class WhatsAppSession:
    def __init__(
        self, backend_factory: Callable[[], Backend] | None, cfg: SessionConfig | None = None
    ) -> None:
        self.backend_factory = backend_factory
        self.cfg = cfg or SessionConfig()
        self.backend: Backend | None = None
        self.state = STOPPED if backend_factory else NOT_INSTALLED
        self.state_since = time.time()
        self.me = ""  # the linked account's number
        self.name = ""
        self.qr: str | None = None
        self.pair_code: str | None = None
        self.last_error = ""
        self.recipients: list[str] = []
        self.on_inbound: Callable[[str, str], Awaitable[str | None]] | None = None
        self.on_state: Callable[[str, str], None] | None = None  # (state, detail) for system alerts
        self.sent: OrderedDict[str, Sent] = OrderedDict()
        self.delivery_s: deque[float] = deque(maxlen=50)
        self.stats = {"sent": 0, "failed": 0, "delivered": 0, "read": 0, "inbound": 0, "capped": 0}
        self._connected = asyncio.Event()
        self._restart = asyncio.Event()
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._next_send = 0.0
        self._hour: deque[float] = deque()
        self._seen_inbound: deque[str] = deque(maxlen=200)
        self._runner: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ lifecycle

    def set_backend_factory(self, factory: Callable[[], Backend]) -> None:
        self.backend_factory = factory
        if self.state == NOT_INSTALLED:
            self.state, self.state_since = STOPPED, time.time()

    def _set_state(self, state: str, detail: str = "") -> None:
        if state == self.state and not detail:
            return
        previous = self.state
        self.state, self.state_since = state, time.time()
        if state == CONNECTED:
            self._connected.set()
            self.qr = self.pair_code = None
            self.last_error = ""
        else:
            self._connected.clear()
        if detail:
            self.last_error = detail
        log.info("WhatsApp: %s%s", state, f" ({detail})" if detail else "")
        if self.on_state is not None and previous != state:
            with contextlib.suppress(Exception):
                self.on_state(state, detail)

    async def start(self) -> None:
        if self.backend_factory is None or self._runner is not None:
            return
        self._stop.clear()
        self._runner = asyncio.ensure_future(self._supervise())

    async def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        if self.backend is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.backend.stop(), 10)
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
            self._runner = None
        for t in list(self._tasks):
            t.cancel()
        self._set_state(STOPPED if self.backend_factory else NOT_INSTALLED)

    async def _supervise(self) -> None:
        """Run the engine; if it dies, start it again (with backoff)."""
        delay = self.cfg.retry_min_s
        while not self._stop.is_set():
            self._restart.clear()
            started = time.monotonic()
            self._set_state(CONNECTING)
            try:
                self.backend = self.backend_factory()  # type: ignore[misc]
                done = await self.backend.start(self._events())
                waiter = asyncio.ensure_future(done)
                restart = asyncio.ensure_future(self._restart.wait())
                try:
                    await asyncio.wait({waiter, restart}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    restart.cancel()
                if waiter.done():
                    waiter.result()  # raises the engine's error, if any
                    if not self._stop.is_set() and self.state not in (LOGGED_OUT, BANNED):
                        self._set_state(DISCONNECTED, "the WhatsApp engine stopped; restarting")
                else:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self.backend.stop(), 10)
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await waiter
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep supervising whatever the engine throws
                self._set_state(ERROR, f"{type(exc).__name__}: {exc}"[:300])
            if self._stop.is_set():
                break
            if self.state == BANNED:
                delay = max(delay, 3600.0)  # don't hammer a banned account
            elif time.monotonic() - started > 120:
                delay = self.cfg.retry_min_s
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(self.cfg.retry_max_s, delay * 2)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ engine events

    def _events(self) -> Events:
        return Events(
            qr=self._on_qr,
            connected=self._on_connected,
            paired=self._on_paired,
            pair_failed=lambda err: self._set_state(PAIRING, f"pairing failed: {err}"),
            disconnected=self._on_disconnected,
            logged_out=self._on_logged_out,
            banned=lambda detail: self._set_state(BANNED, detail),
            receipt=self._on_receipt,
            message=self._on_message,
        )

    def _on_qr(self, codes: list[str]) -> None:
        self.qr = codes[0] if codes else None
        if self.state != PAIRING:
            self._set_state(PAIRING)

    def _on_connected(self, me: str, name: str) -> None:
        if me:
            self.me = digits(me)
        if name:
            self.name = name
        self._set_state(CONNECTED)

    def _on_paired(self, me: str, name: str) -> None:
        self.me, self.name = digits(me), name or self.name
        log.info("WhatsApp linked to +%s%s", self.me, f" ({name})" if name else "")

    def _on_disconnected(self, reason: str = "") -> None:
        if self.state == CONNECTED or reason:
            self._set_state(DISCONNECTED, reason)

    def _on_logged_out(self, reason: str) -> None:
        self.me = ""
        self._set_state(LOGGED_OUT, f"unlinked from the phone ({reason}); pair again on the Setup page")
        self._restart.set()  # a fresh engine offers a new QR code

    def _on_receipt(self, ids: list[str], kind: str, sender: str) -> None:
        now = time.time()
        for msg_id in ids:
            rec = self.sent.get(msg_id)
            if rec is None:
                continue
            if kind == "delivered" and rec.status == "sent":
                rec.status, rec.delivered_at = "delivered", now
                self.delivery_s.append(now - rec.sent_at)
                self.stats["delivered"] += 1
            elif kind == "read" and rec.status != "read":
                if rec.delivered_at is None:
                    rec.delivered_at = now
                    self.delivery_s.append(now - rec.sent_at)
                    self.stats["delivered"] += 1
                rec.status, rec.read_at = "read", now
                self.stats["read"] += 1

    def _on_message(self, sender: str, text: str, msg_id: str, from_me: bool) -> None:
        text = (text or "").strip()
        if from_me or not text or msg_id in self._seen_inbound:
            return
        self._seen_inbound.append(msg_id)
        if digits(sender) not in {digits(r) for r in self.recipients}:
            return  # only the people alerts go to may send commands
        self.stats["inbound"] += 1
        if self.on_inbound is not None:
            self._spawn(self._answer(digits(sender), text))

    async def _answer(self, sender: str, text: str) -> None:
        try:
            reply = await self.on_inbound(sender, text)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            reply = f"Sorry, that failed: {exc}"
        if reply:
            with contextlib.suppress(Exception):
                await self.send([sender], reply, title=f"↩ reply to “{text[:40]}”", count=False)

    # ------------------------------------------------------------------ actions

    async def wait_connected(self, timeout: float) -> bool:
        if self.state == CONNECTED:
            return True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout)
        return self.state == CONNECTED

    def _why_not_connected(self) -> str:
        return {
            NOT_INSTALLED: "the WhatsApp engine isn't installed (pip install 'news247[whatsapp]'; the Docker image has it)",
            STOPPED: "WhatsApp isn't running",
            PAIRING: "WhatsApp isn't linked yet: pair it on the dashboard's Setup page",
            LOGGED_OUT: "WhatsApp was unlinked from the phone: pair it again on the Setup page",
            BANNED: f"WhatsApp account restricted: {self.last_error}",
        }.get(
            self.state,
            f"WhatsApp not connected ({self.state}{': ' + self.last_error if self.last_error else ''})",
        )

    async def send(self, to: list[str], text: str, title: str = "", count: bool = True) -> list[str]:
        """Send one text to each number. Raises RuntimeError with a readable reason."""
        if not await self.wait_connected(
            self.cfg.connect_wait_s if self.state in (CONNECTING, DISCONNECTED) else 0.5
        ):
            raise RuntimeError(self._why_not_connected())
        ids = []
        async with self._send_lock:  # one at a time, in order, spaced out
            for number in to:
                now = time.monotonic()
                while self._hour and now - self._hour[0] > 3600:
                    self._hour.popleft()
                if count and len(self._hour) >= self.cfg.max_per_hour:
                    self.stats["capped"] += 1
                    raise RuntimeError(
                        f"hourly cap of {self.cfg.max_per_hour} WhatsApp messages reached (protects the account)"
                    )
                wait = self._next_send - now
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    assert self.backend is not None
                    msg_id = await self.backend.send_text(digits(number), text)
                except Exception as exc:
                    self.stats["failed"] += 1
                    raise RuntimeError(f"WhatsApp send failed: {exc}") from None
                finally:
                    self._next_send = time.monotonic() + self.cfg.min_interval_s
                if count:
                    self._hour.append(time.monotonic())
                self.stats["sent"] += 1
                self.sent[msg_id] = Sent(msg_id, digits(number), title or text.split("\n", 1)[0], time.time())
                while len(self.sent) > 300:
                    self.sent.popitem(last=False)
                ids.append(msg_id)
        return ids

    async def request_pair_code(self, phone: str) -> str:
        """An 8-character code to enter in WhatsApp > Linked devices > Link with phone number."""
        if self.state == CONNECTED:
            raise RuntimeError(f"already linked to +{self.me}; unlink first to pair another account")
        if self.backend is None or self.state not in (PAIRING, CONNECTING, ERROR, DISCONNECTED):
            raise RuntimeError(self._why_not_connected())
        if len(digits(phone)) < 8:
            raise RuntimeError("enter the WhatsApp number to link, with country code")
        self.pair_code = await self.backend.pair_code(digits(phone))
        return self.pair_code

    async def unlink(self) -> None:
        if self.backend is not None and self.state == CONNECTED:
            await self.backend.logout()
        self.me = ""
        self._set_state(LOGGED_OUT, "unlinked from the dashboard")
        self._restart.set()

    # ------------------------------------------------------------------ status

    def warnings(self) -> list[str]:
        out = []
        if self.me and self.me in {digits(r) for r in self.recipients}:
            out.append(
                "News247 is linked to the same WhatsApp account it texts: alerts land in “Message "
                "yourself” without a notification. Link a second number (e.g. WhatsApp Business "
                "with a second SIM/eSIM) for real alerts."
            )
        return out

    def status(self, qr_svg: Callable[[str], str] | None = None) -> dict[str, Any]:
        lat = sorted(self.delivery_s)
        return {
            "state": self.state,
            "state_since": self.state_since,
            "me": f"+{self.me}" if self.me else "",
            "name": self.name,
            "qr": self.qr,
            "qr_svg": qr_svg(self.qr) if (qr_svg and self.qr) else "",
            "pair_code": self.pair_code,
            "last_error": self.last_error,
            "warnings": self.warnings(),
            "stats": dict(self.stats),
            "delivery_median_s": lat[len(lat) // 2] if lat else None,
            "recent": [r.to_dict() for r in list(self.sent.values())[-10:][::-1]],
        }
