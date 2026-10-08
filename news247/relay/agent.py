"""News247 iMessage relay: runs on any Mac signed in to Messages and texts News247 alerts.

The Mac opens one outbound, authenticated WebSocket to your News247 monitor (in the cloud or
anywhere else) and waits. When an alert comes in, it sends it through the Messages app, reports
the result straight back, and then watches the Messages database for your iPhone's delivery
receipt. If you reply to the alert texts with a command ("pause 2h", "critical", "status"...)
the relay passes it to the monitor and texts back the answer.

    news247 relay --server https://your-monitor.example.com --secret <RELAY_SECRET>
    news247 relay doctor            # check Messages, permissions and the connection
    news247 relay send-test +15551234567

The installer from the dashboard's Setup page sets all of this up as a background service.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import platform
import random
import signal
import socket
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from .. import __version__
from .chatdb import ChatDB
from .messages_app import MessagesApp
from .protocol import (
    CLOSE_BAD_SECRET,
    PROTOCOL_VERSION,
    Channel,
    ProtocolError,
    check_proof,
    new_nonce,
    proof,
    session_key,
    ws_url,
)

log = logging.getLogger("news247.relay")

DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "News247Relay"
MAX_MESSAGE_AGE_S = 24 * 3600  # never send anything the hub queued longer ago than this


class Sender(Protocol):
    async def send(self, handle: str, text: str, service: str = "imessage") -> None: ...


class BadSecret(Exception):
    """The monitor rejected our secret (or the server could not prove it knows it)."""


def _norm(handle: str) -> str:
    h = str(handle).strip().lower()
    if "@" in h:
        return h
    digits = "".join(c for c in h if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def computer_name() -> str:
    if platform.system() == "Darwin":
        with contextlib.suppress(Exception):
            import subprocess

            out = subprocess.run(
                ["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
    return socket.gethostname().split(".")[0] or "relay"


class RateLimiter:
    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self.times: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self.times and now - self.times[0] > self.window_s:
            self.times.popleft()
        if self.limit and len(self.times) >= self.limit:
            return False
        self.times.append(now)
        return True


class SentLog:
    """Remembers recently sent message ids (on disk) so a reconnect never double-texts."""

    def __init__(self, path: Path | None, keep: int = 1000) -> None:
        self.path, self.keep = path, keep
        self.ids: dict[str, float] = {}
        if path and path.is_file():
            with contextlib.suppress(Exception):
                self.ids = {str(k): float(v) for k, v in json.loads(path.read_text()).items()}

    def __contains__(self, msg_id: str) -> bool:
        return msg_id in self.ids

    def add(self, msg_id: str) -> None:
        self.ids[msg_id] = time.time()
        if len(self.ids) > self.keep:
            for k in sorted(self.ids, key=self.ids.get)[: len(self.ids) - self.keep]:  # type: ignore[arg-type]
                del self.ids[k]
        if self.path:
            with contextlib.suppress(OSError):
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.ids))
                tmp.replace(self.path)


class RelayAgent:
    def __init__(
        self,
        server: str,
        secret: str,
        *,
        sender: Sender | None = None,
        chatdb: ChatDB | None = None,
        name: str = "",
        service: str = "imessage",
        allow: list[str] | None = None,
        rate_limit: int = 40,
        rate_window_s: float = 600,
        receipts: bool = True,
        receipt_timeout: float = 30.0,
        inbound_poll_s: float = 2.0,
        state_dir: Path | None = None,
    ) -> None:
        if not server or not secret:
            raise ValueError("the relay needs --server and --secret (or RELAY_SERVER / RELAY_SECRET)")
        if service not in ("imessage", "sms", "auto"):
            raise ValueError("service must be imessage, sms or auto")
        self.url = ws_url(server)
        self.secret = secret.strip()
        self.sender: Sender = sender or MessagesApp(timeout_s=25)
        self.chatdb = chatdb if chatdb is not None else (ChatDB() if receipts else None)
        self.name = name or computer_name()
        self.service = service
        self.allow = {_norm(a) for a in (allow or []) if str(a).strip()}
        self.rate = RateLimiter(rate_limit, rate_window_s)
        self.receipt_timeout = receipt_timeout
        self.inbound_poll_s = inbound_poll_s
        self.state_dir = state_dir
        if state_dir:
            state_dir.mkdir(parents=True, exist_ok=True)
        self.relay_id = self._load_id()
        self.sent_log = SentLog(state_dir / "sent-ids.json" if state_dir else None)
        self.recipients: list[str] = []
        self.connected = asyncio.Event()
        self.stats = {"sent": 0, "failed": 0, "delivered": 0, "duplicates": 0, "rejected": 0, "inbound": 0}
        self.warnings: list[str] = []
        self.receipts_ok = False
        self.messages_ok: bool | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._chan: Channel | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._clock_offset = 0.0  # hub clock minus ours, measured at each handshake
        self.accept_timeout = 5.0  # how long the hub waits for "accepted" (sent in config)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inbound_rowid: int | None = None

    def _load_id(self) -> str:
        if self.state_dir:
            p = self.state_dir / "relay-id"
            if p.is_file():
                return p.read_text().strip()
            rid = uuid.uuid4().hex
            with contextlib.suppress(OSError):
                p.write_text(rid)
            return rid
        return uuid.uuid4().hex

    # ------------------------------------------------------------------ info

    def info(self) -> dict[str, Any]:
        return {
            "id": self.relay_id,
            "name": self.name,
            "version": __version__,
            "macos": platform.mac_ver()[0] or platform.platform(),
            "service": self.service,
            "receipts": self.receipts_ok,
            "messages_ok": self.messages_ok,
            "warnings": self.warnings,
            "stats": self.stats,
        }

    def _check_receipts(self) -> None:
        self.receipts_ok = bool(self.chatdb and self.chatdb.available())
        if self.chatdb and not self.receipts_ok:
            log.warning("delivery receipts and text commands are off: %s", self.chatdb.last_error)

    async def _check_messages(self) -> None:
        """Ask Messages for its accounts: confirms it's signed in, and makes macOS show the
        one-time "allow python to control Messages" prompt now, while someone is at the Mac."""
        accounts = getattr(self.sender, "accounts", None)
        if accounts is None or not MessagesApp.supported():
            return
        try:
            rows = await accounts()
        except RuntimeError as exc:
            self.messages_ok = False
            self.warnings.append(f"cannot control Messages: {exc}")
            log.warning("cannot control Messages: %s", exc)
            return
        self.messages_ok = any(svc.lower() == "imessage" and enabled == "true" for svc, enabled in rows)
        if not self.messages_ok:
            msg = "Messages is not signed in to iMessage (Messages > Settings > iMessage)"
            self.warnings.append(msg)
            log.warning(msg)

    def _self_send_warning(self) -> None:
        if not (self.receipts_ok and self.chatdb and self.recipients):
            return
        with contextlib.suppress(Exception):
            own = {_norm(h) for h in self.chatdb.own_handles()}
            mine = [r for r in self.recipients if _norm(r) in own]
            msg = (
                "this Mac's Messages is signed in to the same Apple ID you're texting, so alerts land in "
                "a note-to-self thread without a notification: sign Messages into a separate Apple ID"
            )
            if mine and msg not in self.warnings:
                self.warnings.append(msg)
                log.warning(msg)

    # ------------------------------------------------------------------ connection

    async def run(self, stop: asyncio.Event) -> None:
        """Stay connected until ``stop`` is set, reconnecting with backoff."""
        self._check_receipts()
        await self._check_messages()
        backoff = 1.0
        async with aiohttp.ClientSession(trust_env=True) as session:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    if await self._session_until_stop(session, stop):
                        break
                    wait = 1.0
                except BadSecret as exc:
                    log.error("%s — re-run the installer from the Setup page. Retrying in 5 minutes.", exc)
                    wait = 300.0
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ProtocolError) as exc:
                    log.warning("connection to %s failed: %s", self.url, exc or type(exc).__name__)
                    wait = backoff
                finally:
                    self.connected.clear()
                    self._ws = self._chan = None
                # after a good (long) connection reconnect quickly, otherwise back off
                backoff = 1.0 if time.monotonic() - started > 60 else min(60.0, backoff * 2)
                wait = wait * random.uniform(0.8, 1.2)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=wait)
        for t in list(self._tasks):
            t.cancel()

    async def _session_until_stop(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> bool:
        """Run one connection; returns True if it ended because ``stop`` was set."""
        sess = asyncio.ensure_future(self._session(session, stop))
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sess, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
        if not sess.done():
            sess.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sess
            return True
        sess.result()  # re-raise connection errors for the backoff logic
        return stop.is_set()

    async def handshake(self, ws: aiohttp.ClientWebSocketResponse) -> Channel:
        challenge = await ws.receive_json(timeout=15)
        if challenge.get("type") != "challenge" or not isinstance(challenge.get("nonce"), str):
            raise ProtocolError(f"unexpected greeting from server: {str(challenge)[:200]}")
        if int(challenge.get("v", 0)) != PROTOCOL_VERSION:
            raise ProtocolError(
                f"server speaks relay protocol v{challenge.get('v')}, this relay v{PROTOCOL_VERSION}"
            )
        hub_nonce, agent_nonce = challenge["nonce"], new_nonce()
        if isinstance(challenge.get("time"), (int, float)):
            self._clock_offset = float(challenge["time"]) - time.time()
        await ws.send_json(
            {
                "type": "hello",
                "v": PROTOCOL_VERSION,
                "nonce": agent_nonce,
                "proof": proof(self.secret, "hello", hub_nonce, agent_nonce),
                "info": self.info(),
            }
        )
        try:
            welcome = await ws.receive_json(timeout=15)
        except TypeError:  # the server closed the socket instead of answering
            welcome = {}
        if welcome.get("type") == "error" or ws.close_code == CLOSE_BAD_SECRET:
            raise BadSecret("the monitor rejected this relay's secret")
        if welcome.get("type") != "welcome" or not check_proof(
            proof(self.secret, "welcome", agent_nonce, hub_nonce), welcome.get("proof")
        ):
            raise BadSecret("the server could not prove it knows the relay secret (wrong server URL?)")
        return Channel(session_key(self.secret, hub_nonce, agent_nonce))

    async def _session(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        async with session.ws_connect(self.url, heartbeat=20, max_msg_size=1 << 20) as ws:
            self._chan = await self.handshake(ws)
            self._ws = ws
            self._queue = asyncio.Queue()  # jobs from an earlier connection are the hub's problem now
            self.connected.set()
            log.info("connected to %s as '%s'", self.url, self.name)
            workers = [
                self._spawn(self._worker()),
                self._spawn(self._status_loop()),
                self._spawn(self._inbound_loop()),
            ]
            try:
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        msg = self._chan.open(json.loads(m.data))
                        await self._on_message(msg)
                    elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
                    if stop.is_set():
                        break
            finally:
                for w in workers:
                    w.cancel()
            if ws.close_code == CLOSE_BAD_SECRET:
                raise BadSecret("the monitor rejected this relay's secret")
            log.info("disconnected (code %s)", ws.close_code)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("relay task failed: %r", task.exception())

    async def post(self, msg: dict[str, Any]) -> None:
        if self._ws is None or self._chan is None or self._ws.closed:
            return  # disconnected: the hub re-sends anything without a result
        await self._ws.send_str(json.dumps(self._chan.seal(msg), ensure_ascii=False))

    # ------------------------------------------------------------------ messages from the hub

    async def _on_message(self, msg: dict[str, Any]) -> None:
        kind = msg["type"]
        if kind == "config":
            self.recipients = [str(r) for r in msg.get("recipients") or []]
            if isinstance(msg.get("accept_timeout"), (int, float)):
                self.accept_timeout = float(msg["accept_timeout"])
            self._self_send_warning()
        elif kind == "send":
            await self._on_send(msg)

    async def _on_send(self, msg: dict[str, Any]) -> None:
        msg_id = str(msg.get("id", ""))
        to = [str(t) for t in msg.get("to") or [] if str(t).strip()]
        text = str(msg.get("text", ""))
        if msg_id in self.sent_log:
            self.stats["duplicates"] += 1
            await self.post({"type": "result", "id": msg_id, "ok": True, "duplicate": True})
            return
        problem = ""
        if not to or not text:
            problem = "empty message"
        elif self.allow and any(_norm(t) not in self.allow for t in to):
            problem = "recipient not on this relay's --allow list"
        elif time.time() - float(msg.get("created") or 0) > MAX_MESSAGE_AGE_S:
            problem = "message too old"
        elif self._arrived_late(msg):
            problem = (
                "arrived after the monitor stopped waiting (this Mac was asleep); it was handled elsewhere"
            )
        elif not self.rate.allow():
            problem = f"relay rate limit ({self.rate.limit} texts per {self.rate.window_s / 60:.0f} min) protects your Apple ID"
        if problem:
            self.stats["rejected"] += 1
            log.warning("refusing message %s: %s", msg_id, problem)
            await self.post({"type": "result", "id": msg_id, "ok": False, "error": problem})
            return
        await self.post({"type": "accepted", "id": msg_id})
        await self._queue.put({"id": msg_id, "to": to, "text": text})

    def _arrived_late(self, msg: dict[str, Any]) -> bool:
        issued = msg.get("issued")
        if not isinstance(issued, (int, float)):
            return False
        age = time.time() + self._clock_offset - float(issued)
        return age > self.accept_timeout + 2.0  # 2 s grace for network delay and clock jitter

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._deliver(job)
            finally:
                self._queue.task_done()

    async def _send_one(self, handle: str, text: str) -> str:
        """Send to one handle; returns the service used."""
        if self.service == "auto":
            try:
                await self.sender.send(handle, text, "imessage")
                return "imessage"
            except RuntimeError as exc:
                if "Automation" in str(exc):
                    raise
                await self.sender.send(handle, text, "sms")
                return "sms"
        await self.sender.send(handle, text, self.service)
        return self.service

    async def _deliver(self, job: dict[str, Any]) -> None:
        msg_id, text = job["id"], job["text"]
        per: list[dict[str, Any]] = []
        watch: list[tuple[str, float]] = []  # receipts are watched after the result is reported
        for handle in job["to"]:
            started = time.time()
            try:
                service = await self._send_one(handle, text)
                per.append({"to": handle, "ok": True, "service": service})
                watch.append((handle, started))
            except Exception as exc:  # noqa: BLE001 - report every failure to the hub
                per.append({"to": handle, "ok": False, "error": str(exc)[:300]})
        ok = all(p["ok"] for p in per)
        if any(p["ok"] for p in per):
            self.sent_log.add(msg_id)
        self.stats["sent" if ok else "failed"] += 1
        error = "; ".join(f"{p['to']}: {p['error']}" for p in per if not p["ok"])
        if error:
            log.warning("message %s failed: %s", msg_id, error)
        else:
            log.info("sent %s to %s", msg_id, ", ".join(job["to"]))
        await self.post(
            {"type": "result", "id": msg_id, "ok": ok, "error": error, "per": per, "sent_at": time.time()}
        )
        if self.receipts_ok and self.chatdb is not None:
            for handle, started in watch:
                self._spawn(self._watch_receipt(msg_id, handle, text, started))

    async def _watch_receipt(self, msg_id: str, handle: str, text: str, since: float) -> None:
        assert self.chatdb is not None
        r = await self.chatdb.wait_receipt(handle, since, timeout=self.receipt_timeout)
        if (
            r["status"] == "failed"
            and self.service == "auto"
            and str(r.get("service", "")).lower() == "imessage"
        ):
            log.warning("iMessage to %s failed (error %s); retrying as SMS", handle, r.get("error_code"))
            try:
                again = time.time()
                await self.sender.send(handle, text, "sms")
                r = await self.chatdb.wait_receipt(handle, again, timeout=self.receipt_timeout)
            except RuntimeError as exc:
                r = {"status": "failed", "error": f"iMessage failed and SMS fallback failed: {exc}"}
        if r["status"] == "delivered":
            self.stats["delivered"] += 1
        await self.post(
            {
                "type": "receipt",
                "id": msg_id,
                "to": handle,
                "status": r["status"],
                "error_code": r.get("error_code"),
                "error": r.get("error", ""),
                "delivered_at": r.get("delivered_at"),
            }
        )

    # ------------------------------------------------------------------ background loops

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.post({"type": "status", "info": self.info()})

    async def _inbound_loop(self) -> None:
        """Forward texts you send to the relay ("pause 2h", "status") to the monitor."""
        if not (self.receipts_ok and self.chatdb is not None):
            return
        if self._inbound_rowid is None:
            self._inbound_rowid = await asyncio.to_thread(self.chatdb.max_rowid)  # never replay history
        while True:
            await asyncio.sleep(self.inbound_poll_s)
            if not self.recipients:
                continue
            try:
                rows = await asyncio.to_thread(
                    self.chatdb.incoming_after, self._inbound_rowid, self.recipients
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("inbound poll failed: %s", exc)
                continue
            for row in rows:
                self._inbound_rowid = max(self._inbound_rowid, row["rowid"])
                if row["text"]:
                    self.stats["inbound"] += 1
                    await self.post(
                        {
                            "type": "inbound",
                            "from": row["from"],
                            "text": row["text"][:500],
                            "guid": row["guid"],
                            "at": row["at"],
                        }
                    )


# --------------------------------------------------------------------------- doctor


async def doctor(
    server: str, secret: str, *, sender: MessagesApp | None = None, chatdb: ChatDB | None = None
) -> int:
    """Check everything the relay needs and print what to fix."""
    ok = True

    def line(good: bool | None, text: str, fix: str = "") -> None:
        nonlocal ok
        mark = {True: "\033[32m✓\033[0m", False: "\033[31m✗\033[0m", None: "\033[33m!\033[0m"}[good]
        print(f" {mark} {text}" + (f"\n     → {fix}" if fix else ""))
        if good is False:
            ok = False

    sender = sender or MessagesApp(timeout_s=15)
    chatdb = chatdb or ChatDB()
    print(f"News247 relay {__version__} on {computer_name()}")
    if not MessagesApp.supported():
        line(
            False,
            f"this is {platform.system()}, not macOS",
            "the relay must run on a Mac signed in to Messages",
        )
    else:
        line(True, f"macOS {platform.mac_ver()[0]}")
        try:
            accounts = await sender.accounts()
            imsg = [a for a in accounts if a[0].lower() == "imessage"]
            if imsg and any(e == "true" for _, e in imsg):
                line(True, "Messages is signed in to iMessage")
            elif imsg:
                line(
                    False,
                    "the iMessage account in Messages is disabled",
                    "Messages > Settings > iMessage: sign in / enable",
                )
            else:
                line(False, "Messages has no iMessage account", "open Messages and sign in with an Apple ID")
        except RuntimeError as exc:
            line(
                False,
                f"cannot control Messages: {exc}",
                "System Settings > Privacy & Security > Automation: allow Messages",
            )
    if chatdb.available():
        line(True, "can read the Messages database (delivery receipts and text commands work)")
        own = chatdb.own_handles()
        if own:
            line(
                None,
                f"this Mac sends as: {', '.join(own[:3])}",
                "make sure that is NOT the Apple ID on your iPhone",
            )
    else:
        line(None, "delivery receipts and text commands are off", chatdb.last_error)
    if server and secret:
        agent = RelayAgent(server, secret, sender=sender, chatdb=chatdb, receipts=False)
        try:
            async with aiohttp.ClientSession(trust_env=True) as session, session.ws_connect(agent.url) as ws:
                await agent.handshake(ws)
            line(True, f"connected and authenticated to {agent.url}")
        except BadSecret as exc:
            line(False, str(exc), "copy the install command again from the dashboard's Setup page")
        except Exception as exc:  # noqa: BLE001
            line(
                False,
                f"cannot reach {agent.url}: {exc}",
                "check the server URL and this Mac's internet connection",
            )
    else:
        line(False, "no server/secret configured", "run the installer from the dashboard's Setup page")
    return 0 if ok else 1


# --------------------------------------------------------------------------- CLI


def load_env_file(path: str | None) -> None:
    if not path or not Path(path).is_file():
        return
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="news247 relay", description="Text News247 alerts from this Mac's Messages app"
    )
    p.add_argument("action", nargs="?", default="run", choices=["run", "doctor", "send-test"])
    p.add_argument("number", nargs="?", help="for send-test: phone number or Apple ID e-mail")
    p.add_argument("--server", help="your News247 monitor's URL (env RELAY_SERVER)")
    p.add_argument("--secret", help="the relay secret from the Setup page (env RELAY_SECRET)")
    p.add_argument("--env-file", help="read RELAY_* settings from this file")
    p.add_argument("--name", help="name shown on the dashboard (default: this Mac's name)")
    p.add_argument(
        "--service", choices=["imessage", "sms", "auto"], help="default imessage (env RELAY_SERVICE)"
    )
    p.add_argument(
        "--allow", action="append", help="only ever text this number (repeatable; env RELAY_ALLOW)"
    )
    p.add_argument("--no-receipts", action="store_true", help="don't read the Messages database")
    p.add_argument("--allow-sleep", action="store_true", help="don't keep the Mac awake while running")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file(args.env_file)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    server = args.server or os.environ.get("RELAY_SERVER", "")
    secret = args.secret or os.environ.get("RELAY_SECRET", "")
    service = args.service or os.environ.get("RELAY_SERVICE", "imessage")
    allow = args.allow or [a for a in os.environ.get("RELAY_ALLOW", "").split(",") if a.strip()]

    if args.action == "doctor":
        return asyncio.run(doctor(server, secret))
    if args.action == "send-test":
        if not args.number:
            print("usage: news247 relay send-test +15551234567", file=sys.stderr)
            return 2
        return asyncio.run(_send_test(args.number, service, not args.no_receipts))
    try:
        agent = RelayAgent(
            server,
            secret,
            name=args.name or os.environ.get("RELAY_NAME", ""),
            service=service,
            allow=allow,
            receipts=not args.no_receipts,
            state_dir=Path(args.state_dir),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not MessagesApp.supported():
        log.warning("not running on macOS: messages will fail (use this only for testing)")
    return asyncio.run(_run_forever(agent, awake=not args.allow_sleep))


def keep_awake() -> Any:
    """Hold a macOS power assertion (no idle sleep) for as long as this process lives."""
    import shutil
    import subprocess

    if platform.system() != "Darwin" or not shutil.which("caffeinate"):
        return None
    with contextlib.suppress(OSError):
        return subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
    return None


async def _run_forever(agent: RelayAgent, awake: bool = True) -> int:
    caffeinate = keep_awake() if awake else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    log.info("News247 relay %s starting: %s → Messages (%s)", __version__, agent.url, agent.service)
    try:
        await agent.run(stop)
    finally:
        if caffeinate is not None:
            caffeinate.terminate()
    return 0


async def _send_test(number: str, service: str, receipts: bool) -> int:
    app, db = MessagesApp(), ChatDB()
    started = time.time()
    try:
        await app.send(
            number,
            "News247 relay test: alerts will arrive like this.",
            "sms" if service == "sms" else "imessage",
        )
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1
    print("✓ handed to Messages")
    if receipts and db.available():
        r = await db.wait_receipt(number, started, timeout=30)
        print(
            {
                "delivered": "✓ delivered to the phone",
                "failed": f"✗ not delivered (error {r.get('error_code')})",
                "sent": "! sent, but the phone hasn't confirmed delivery yet",
            }.get(r["status"], f"? {r.get('error', r['status'])}")
        )
        return 0 if r["status"] in ("delivered", "sent") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
