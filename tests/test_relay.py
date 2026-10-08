"""The self-hosted iMessage relay: protocol, hub <-> agent over a real WebSocket, queueing,
backups, delivery receipts, text commands, and the one-line installer endpoints."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from news247.config import build_config
from news247.engine import Engine
from news247.models import Alert, Severity
from news247.notify import Dispatcher
from news247.relay.agent import BadSecret, RateLimiter, RelayAgent, SentLog
from news247.relay.chatdb import ChatDB, decode_attributed_body, unix_to_apple
from news247.relay.commands import parse_command, parse_duration
from news247.relay.hub import RelayFailed, RelayHub, RelayUnavailable
from news247.relay.protocol import Channel, ProtocolError, new_secret, proof, session_key, ws_url
from news247.storage import Storage
from news247.web.server import WebServer

from .conftest import CaptureNotifier

SECRET = "test-relay-secret-0123456789"
PHONE = "+15551234567"


class FakeSender:
    def __init__(self, fail: str = "", delay: float = 0.0) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail
        self.delay = delay

    async def send(self, handle: str, text: str, service: str = "imessage") -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append((handle, text, service))


class FakeChatDB:
    def __init__(self, receipt: str = "delivered", inbound: list[dict[str, Any]] | None = None) -> None:
        self.receipt = receipt
        self.inbound = list(inbound or [])
        self.last_error = ""
        self.own: list[str] = []

    def available(self) -> bool:
        return True

    async def wait_receipt(
        self, handle: str, since: float, timeout: float = 20, poll: float = 0.5
    ) -> dict[str, Any]:
        if self.receipt == "failed":
            return {"status": "failed", "error_code": 22, "service": "iMessage"}
        return {"status": self.receipt, "delivered_at": time.time()}

    def max_rowid(self) -> int:
        return 0

    def incoming_after(self, rowid: int, handles: list[str]) -> list[dict[str, Any]]:
        out = [r for r in self.inbound if r["rowid"] > rowid]
        self.inbound = []
        return out

    def own_handles(self) -> list[str]:
        return self.own


async def wait_for(cond: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture
async def hub_server():
    servers: list[TestServer] = []

    async def make(hub: RelayHub) -> TestServer:
        app = web.Application()
        app.router.add_get("/relay/ws", hub.handle)
        srv = TestServer(app)
        await srv.start_server()
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        await s.close()


class AgentRunner:
    def __init__(self) -> None:
        self.runs: list[tuple[asyncio.Event, asyncio.Task[Any]]] = []

    async def start(self, server: TestServer, hub: RelayHub | None = None, **kw: Any) -> RelayAgent:
        kw.setdefault("sender", FakeSender())
        kw.setdefault("chatdb", None)
        kw.setdefault("receipts", False)
        agent = RelayAgent(
            str(server.make_url("/")), kw.pop("secret", SECRET), name=kw.pop("name", "test-mac"), **kw
        )
        stop = asyncio.Event()
        task = asyncio.ensure_future(agent.run(stop))
        self.runs.append((stop, task))
        await asyncio.wait_for(agent.connected.wait(), 5)
        if hub is not None:
            await wait_for(lambda: any(c.name == agent.name for c in hub.connections))
        return agent

    async def stop_all(self) -> None:
        for stop, _ in self.runs:
            stop.set()
        for _, task in self.runs:
            await asyncio.wait_for(task, 5)


@pytest.fixture
async def agents():
    r = AgentRunner()
    yield r
    await r.stop_all()


def make_hub(**kw: Any) -> RelayHub:
    kw.setdefault("accept_timeout", 2.0)
    kw.setdefault("result_timeout", 5.0)
    hub = RelayHub(SECRET, **kw)
    hub.set_recipients([PHONE])
    return hub


# --------------------------------------------------------------------------- protocol


def test_channel_seal_open_and_tamper_replay():
    key = session_key(SECRET, "a" * 32, "b" * 32)
    tx, rx = Channel(key), Channel(key)
    m1 = tx.seal({"type": "send", "id": "x", "text": "hello"})
    assert rx.open(dict(m1))["text"] == "hello"
    with pytest.raises(ProtocolError, match="replayed"):
        rx.open(dict(m1))
    m2 = tx.seal({"type": "send", "id": "y", "text": "hello"})
    with pytest.raises(ProtocolError, match="bad signature"):
        rx.open({**m2, "text": "send money to +1900…"})
    other = Channel(session_key("another-secret-xxxxxxxx", "a" * 32, "b" * 32))
    with pytest.raises(ProtocolError):
        other.open(tx.seal({"type": "send"}))
    with pytest.raises(ProtocolError, match="malformed"):
        rx.open(["not", "a", "dict"])


def test_ws_url_and_secrets():
    assert ws_url("https://news.onrender.com/") == "wss://news.onrender.com/relay/ws"
    assert ws_url("http://localhost:8247") == "ws://localhost:8247/relay/ws"
    assert ws_url("news.example.com") == "wss://news.example.com/relay/ws"
    assert ws_url("wss://x.dev/relay/ws") == "wss://x.dev/relay/ws"
    assert len(new_secret()) >= 40 and new_secret() != new_secret()
    assert proof(SECRET, "hello", "a", "b") != proof(SECRET, "hello", "b", "a")


# --------------------------------------------------------------------------- end to end


async def test_send_end_to_end(hub_server, agents):
    hub = make_hub()
    srv = await hub_server(hub)
    agent = await agents.start(srv, hub)
    rec = hub.new_message("a1", [PHONE], "🔴 OpenAI launches agents", title="t", severity="CRITICAL")
    res = await hub.deliver(rec)
    assert res["ok"] and rec.status == "sent" and rec.relay == "test-mac"
    assert agent.sender.sent == [(PHONE, "🔴 OpenAI launches agents", "imessage")]  # type: ignore[attr-defined]
    st = hub.status()
    assert st["online"] and st["relays"][0]["name"] == "test-mac" and st["stats"]["sent"] == 1
    # the relay learned who the alerts go to (needed for text commands)
    assert agent.recipients == [PHONE]


async def test_wrong_secret_is_rejected_both_ways(hub_server):
    hub = make_hub()
    srv = await hub_server(hub)
    bad = RelayAgent(str(srv.make_url("/")), "wrong-secret-wrong-secret", sender=FakeSender(), receipts=False)
    async with aiohttp.ClientSession() as s, s.ws_connect(bad.url) as ws:
        with pytest.raises(BadSecret, match="rejected"):
            await bad.handshake(ws)
    assert hub.stats["rejected"] == 1 and not hub.connections

    # an impostor server that doesn't know the secret can't make the relay send anything
    async def impostor(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "challenge", "v": 1, "nonce": "n" * 32})
        await ws.receive()
        await ws.send_json({"type": "welcome", "v": 1, "proof": "0" * 64})
        await asyncio.sleep(0.2)
        return ws

    app = web.Application()
    app.router.add_get("/relay/ws", impostor)
    fake = TestServer(app)
    await fake.start_server()
    try:
        good = RelayAgent(str(fake.make_url("/")), SECRET, sender=FakeSender(), receipts=False)
        async with aiohttp.ClientSession() as s, s.ws_connect(good.url) as ws:
            with pytest.raises(BadSecret, match="could not prove"):
                await good.handshake(ws)
    finally:
        await fake.close()


async def test_offline_queues_then_delivers_late(hub_server, agents):
    hub = make_hub()
    rec = hub.new_message("q1", [PHONE], "🟠 Fed cuts rates", title="🟠 Fed cuts rates")
    with pytest.raises(RelayUnavailable, match="no iMessage relay has connected yet.*queued"):
        await hub.deliver(rec)
    assert rec.status == "queued" and hub.status()["queued"] == 1
    rec.created -= 300  # it waited five minutes for the Mac
    srv = await hub_server(hub)
    agent = await agents.start(srv, hub)
    await wait_for(lambda: rec.status == "sent")
    ((to, text, _),) = agent.sender.sent  # type: ignore[attr-defined]
    assert to == PHONE and text.startswith("⏱ 5m late (relay was offline)\n🟠 Fed cuts rates")
    assert hub.stats["late"] == 1


async def test_backlog_becomes_one_digest_and_old_ones_expire(hub_server, agents):
    hub = make_hub(max_late_s=1800, digest_min=3)
    recs = []
    for i, sev in enumerate(["HIGH", "CRITICAL", "HIGH", "HIGH"]):
        r = hub.new_message(f"d{i}", [PHONE], f"alert {i}", title=f"Headline {i}", severity=sev)
        with pytest.raises(RelayUnavailable):
            await hub.deliver(r)
        recs.append(r)
    recs[0].created -= 3600  # older than max_late: dropped, not sent
    srv = await hub_server(hub)
    agent = await agents.start(srv, hub)
    await wait_for(lambda: all(r.status in ("sent", "expired") for r in recs))
    assert recs[0].status == "expired"
    ((_, text, _),) = agent.sender.sent  # type: ignore[attr-defined]
    lines = text.splitlines()
    assert lines[0] == "📬 3 alerts while your iMessage relay was offline:"
    assert lines[1].startswith("🟠 Headline 1") is False and "Headline 1" in text and "Headline 0" not in text
    assert any(line.startswith("🔴 Headline 1") for line in lines)
    assert all("in digest" in r.note for r in recs[1:]) and hub.stats["digests"] == 1


async def test_relay_failure_is_reported_not_queued(hub_server, agents):
    hub = make_hub()
    srv = await hub_server(hub)
    await agents.start(srv, hub, sender=FakeSender(fail="Messages is not signed in"))
    rec = hub.new_message("f1", [PHONE], "x")
    with pytest.raises(RelayFailed, match="not signed in"):
        await hub.deliver(rec)
    assert rec.status == "failed" and not hub.queued()


async def test_sleeping_relay_times_out_fast_and_queues(hub_server):
    """A Mac that went to sleep leaves a half-dead connection: it must not hold alerts hostage."""
    hub = make_hub(accept_timeout=0.3)
    srv = await hub_server(hub)
    zombie = RelayAgent(str(srv.make_url("/")), SECRET, sender=FakeSender(), receipts=False, name="sleepy")
    async with aiohttp.ClientSession() as s, s.ws_connect(zombie.url) as ws:
        await zombie.handshake(ws)  # authenticated, then never reads again
        await wait_for(lambda: hub.connections)
        rec = hub.new_message("z1", [PHONE], "x")
        t0 = time.monotonic()
        with pytest.raises(RelayUnavailable, match="did not answer"):
            await hub.deliver(rec)
        assert time.monotonic() - t0 < 2 and rec.status == "queued"
        await wait_for(lambda: not hub.live())


async def test_duplicates_allow_list_and_rate_limit(hub_server, agents, tmp_path):
    hub = make_hub()
    srv = await hub_server(hub)
    agent = await agents.start(srv, hub, allow=["(555) 123-4567"], state_dir=tmp_path, rate_limit=2)
    r1 = hub.new_message("dup", [PHONE], "one")
    await hub.deliver(r1)
    # the same id again (e.g. re-sent after a reconnect) is acknowledged but never re-texted
    r1.status = "queued"
    res = await hub.deliver(r1)
    assert res.get("duplicate") and len(agent.sender.sent) == 1  # type: ignore[attr-defined]
    assert "dup" in SentLog(tmp_path / "sent-ids.json")  # survives a restart
    with pytest.raises(RelayFailed, match="allow"):
        await hub.deliver(hub.new_message("stranger", ["+19998887777"], "spam"))
    await hub.deliver(hub.new_message("two", [PHONE], "two"))
    with pytest.raises(RelayFailed, match="rate limit"):
        await hub.deliver(hub.new_message("three", [PHONE], "three"))


async def test_receipts_and_failed_delivery(hub_server, agents):
    hub = make_hub()
    srv = await hub_server(hub)
    db = FakeChatDB(receipt="delivered")
    await agents.start(srv, hub, chatdb=db, receipts=True)
    rec = hub.new_message("r1", [PHONE], "x")
    await hub.deliver(rec)
    await wait_for(lambda: rec.status == "delivered")
    assert hub.stats["delivered"] == 1 and hub.status()["delivery_median_s"] is not None
    db.receipt = "failed"
    rec2 = hub.new_message("r2", [PHONE], "y")
    await hub.deliver(rec2)
    await wait_for(lambda: rec2.status == "failed")
    assert "22" in rec2.error


async def test_text_commands_round_trip(hub_server, agents):
    hub = make_hub()
    replies: list[tuple[str, str]] = []

    async def on_inbound(sender: str, text: str) -> str | None:
        replies.append((sender, text))
        return "⏸ paused" if text.lower().startswith("pause") else None

    hub.on_inbound = on_inbound
    srv = await hub_server(hub)
    db = FakeChatDB(
        inbound=[
            {"rowid": 7, "guid": "g1", "from": "+15551234567", "text": "Pause 2h", "at": time.time()},
            {"rowid": 8, "guid": "g2", "from": "+19998887777", "text": "pause", "at": time.time()},
        ]
    )
    agent = await agents.start(srv, hub, chatdb=db, receipts=True, inbound_poll_s=0.05)
    await wait_for(lambda: agent.sender.sent)  # type: ignore[attr-defined]
    assert replies == [("+15551234567", "Pause 2h")]  # strangers are ignored
    assert agent.sender.sent == [("+15551234567", "⏸ paused", "imessage")]  # type: ignore[attr-defined]


async def test_reconnect_replaces_stale_connection(hub_server, agents, tmp_path):
    hub = make_hub()
    srv = await hub_server(hub)
    a1 = await agents.start(srv, hub, state_dir=tmp_path)
    a2 = await agents.start(srv, None, state_dir=tmp_path)  # same Mac (same relay id) reconnecting
    assert a1.relay_id == a2.relay_id
    await wait_for(lambda: len(hub.connections) == 1)


async def test_self_send_warning(hub_server, agents):
    hub = make_hub()
    srv = await hub_server(hub)
    db = FakeChatDB()
    db.own = ["+15551234567"]
    agent = await agents.start(srv, hub, chatdb=db, receipts=True)
    await wait_for(lambda: agent.warnings)
    assert "same Apple ID" in agent.warnings[0]


# --------------------------------------------------------------------------- dispatcher integration


def alert(sev: Severity = Severity.HIGH, title: str = "OpenAI launches agents") -> Alert:
    return Alert(kind="news", severity=sev, title=title, body="")


def relay_dispatcher(tmp_path: Path, http: Any = None) -> tuple[Dispatcher, Any, CaptureNotifier]:
    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "notify": {
                "console": {"enabled": False},
                "relay": {"enabled": True, "to": PHONE, "secret": SECRET, "accept_timeout": 1},
            },
        }
    )
    disp = Dispatcher(cfg.notify, http)  # type: ignore[arg-type]
    backup = CaptureNotifier(Severity.HIGH)
    backup.name = "ntfy"
    backup.backup = True
    disp.channels.append(backup)
    return disp, disp.channels[0], backup


async def test_backup_takes_over_when_relay_offline_and_is_not_repeated(tmp_path, hub_server, agents):
    disp, relay, backup = relay_dispatcher(tmp_path)
    out = await disp.dispatch(alert())
    assert out["relay"].startswith("error: RelayUnavailable") and out["ntfy"] == "ok"
    assert len(backup.alerts) == 1
    (rec,) = relay.hub.outbox.values()
    assert rec.status == "superseded" and "ntfy" in rec.note
    srv = await hub_server(relay.hub)
    agent = await agents.start(srv, relay.hub)
    await asyncio.sleep(0.2)
    assert agent.sender.sent == []  # already delivered by the backup: no stale repeat


async def test_backup_stays_quiet_when_relay_works(tmp_path, hub_server, agents):
    disp, relay, backup = relay_dispatcher(tmp_path)
    srv = await hub_server(relay.hub)
    agent = await agents.start(srv, relay.hub)
    out = await disp.dispatch(alert())
    assert out == {"relay": "ok"} and backup.alerts == []
    assert len(agent.sender.sent) == 1  # type: ignore[attr-defined]


async def test_backup_alone_acts_as_primary(tmp_path):
    cfg = build_config({"general": {"data_dir": str(tmp_path)}, "notify": {"console": {"enabled": False}}})
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    only = CaptureNotifier(Severity.HIGH)
    only.name, only.backup = "ntfy", True
    disp.channels.append(only)
    await disp.dispatch(alert())
    assert len(only.alerts) == 1


async def test_pause_and_phone_level(tmp_path):
    cfg = build_config({"general": {"data_dir": str(tmp_path)}, "notify": {"console": {"enabled": False}}})
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    phone = CaptureNotifier(Severity.HIGH)
    phone.name = "relay"
    console = CaptureNotifier(Severity.LOW)
    console.name = "console"
    disp.channels += [phone, console]
    disp.pause(60)
    await disp.dispatch(alert(Severity.CRITICAL))
    assert phone.alerts == [] and len(console.alerts) == 1  # the dashboard/console keep everything
    disp.resume()
    disp.set_phone_min(Severity.CRITICAL)
    await disp.dispatch(alert(Severity.HIGH))
    assert phone.alerts == []
    disp.set_phone_min(Severity.MEDIUM)
    await disp.dispatch(alert(Severity.MEDIUM))
    assert len(phone.alerts) == 1


# --------------------------------------------------------------------------- engine + web


def relay_engine(tmp_path: Path, token: str = "dash-token") -> Engine:
    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {"console": {"enabled": False}, "relay": {"enabled": True, "to": PHONE}},
            "web": {"token": token, "public_url": "https://news247.example.com"},
        }
    )
    return Engine(cfg, storage=Storage(tmp_path / "t.db"), sources=[])


async def test_phone_commands_change_texting(tmp_path):
    eng = relay_engine(tmp_path)
    assert eng.relay_hub is not None and len(eng.relay_hub.secret) >= 32  # generated, then persisted
    assert eng.storage.get_setting("relay_secret") == eng.relay_hub.secret
    reply = await eng.handle_phone_command(PHONE, "pause 2h")
    assert reply and reply.startswith("⏸ Texts paused until") and eng.dispatcher.paused
    assert await eng.handle_phone_command(PHONE, "thanks!") is None
    assert "RESUME" in (await eng.handle_phone_command(PHONE, "stop") or "")
    assert eng.phone_mode() == "paused until you text RESUME"
    assert "back on" in (await eng.handle_phone_command(PHONE, "resume") or "")
    assert "only CRITICAL" in (await eng.handle_phone_command(PHONE, "critical") or "")
    assert eng.dispatcher.phone_min is Severity.CRITICAL
    status = await eng.handle_phone_command(PHONE, "status")
    assert status and "News247 up" in status and "CRITICAL and above" in status
    assert "PAUSE" in (await eng.handle_phone_command(PHONE, "help") or "")
    # settings survive a restart
    eng.storage.close()
    eng2 = Engine(eng.cfg, storage=Storage(tmp_path / "t.db"), sources=[])
    assert eng2.dispatcher.phone_min is Severity.CRITICAL
    assert eng2.relay_hub is not None and eng2.relay_hub.secret == eng.relay_hub.secret


async def test_web_endpoints_install_and_websocket(tmp_path):
    eng = relay_engine(tmp_path)
    server = WebServer(eng, eng.cfg.web)
    srv = TestServer(server.app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            # the installer contains the relay secret: dashboard token required
            async with s.get(srv.make_url("/relay/install.sh")) as r:
                assert r.status == 401
            async with s.get(srv.make_url("/relay/install.sh?token=dash-token")) as r:
                assert r.status == 200
                script = await r.text()
            assert "N247_SERVER=https://news247.example.com" in script
            assert eng.relay_hub.secret in script  # type: ignore[union-attr]
            assert (
                "N247_PACKAGE_URL='https://news247.example.com/relay/package.tar.gz?token=dash-token'"
                in script
            )
            sh = tmp_path / "install.sh"
            sh.write_text(script)
            assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
            async with s.get(srv.make_url("/api/relay?token=dash-token")) as r:
                info = await r.json()
            assert info["install_command"] == (
                "curl -fsSL 'https://news247.example.com/relay/install.sh?token=dash-token' | bash"
            )
            assert info["online"] is False and "connected yet" in info["offline_reason"]
            async with s.get(srv.make_url("/relay/package.tar.gz?token=dash-token")) as r:
                data = await r.read()
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                names = tar.getnames()
            assert "news247-1.0.0/pyproject.toml" in names
            assert "news247-1.0.0/news247/relay/agent.py" in names
            assert "news247-1.0.0/news247/relay/install_relay.sh" in names
            assert not any("__pycache__" in n for n in names)
        # the relay endpoint itself doesn't need the dashboard token: it has its own handshake
        agent = RelayAgent(str(srv.make_url("/")), eng.relay_hub.secret, sender=FakeSender(), receipts=False)  # type: ignore[union-attr]
        async with aiohttp.ClientSession() as s, s.ws_connect(agent.url) as ws:
            await agent.handshake(ws)
            await wait_for(lambda: eng.relay_hub.connections)  # type: ignore[union-attr]
            st = eng.phone_status()
            assert st["relay"]["online"] and st["phone_mode"] == "normal"
    finally:
        await srv.close()
        await eng.relay_hub.close()  # type: ignore[union-attr]


async def test_relay_channel_send_test_is_not_queued(tmp_path):
    eng = relay_engine(tmp_path)
    res = await eng.send_test()
    assert res["relay"].startswith("error: RelayUnavailable")
    assert eng.relay_hub is not None and not eng.relay_hub.queued()  # a stale "test" text later is pointless


# --------------------------------------------------------------------------- Messages database


def make_chat_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
            handle_id INTEGER, is_from_me INTEGER, error INTEGER DEFAULT 0, is_sent INTEGER DEFAULT 0,
            is_delivered INTEGER DEFAULT 0, date INTEGER, date_delivered INTEGER DEFAULT 0, service TEXT,
            account TEXT);
        INSERT INTO handle VALUES (1, '+15551234567'), (2, 'friend@icloud.com');
        """
    )
    return db


def attributed(text: str) -> bytes:
    raw = text.encode()
    length = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject"
        b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + raw + b"\x86\x84\x02iI\x01"
    )


def test_attributed_body_decoding():
    assert decode_attributed_body(attributed("pause 2h")) == "pause 2h"
    long = "x" * 300
    assert decode_attributed_body(attributed(long)) == long
    assert decode_attributed_body(b"") == "" and decode_attributed_body(b"garbage") == ""


async def test_chatdb_receipts_inbound_and_own_handles(tmp_path):
    path = tmp_path / "chat.db"
    db = make_chat_db(path)
    now = time.time()
    db.execute(
        "INSERT INTO message (ROWID, guid, handle_id, is_from_me, is_sent, is_delivered, date, date_delivered, service, account)"
        " VALUES (1, 'o1', 1, 1, 1, 1, ?, ?, 'iMessage', 'E:bot@icloud.com')",
        (unix_to_apple(now), unix_to_apple(now + 1.5)),
    )
    db.execute(
        "INSERT INTO message (ROWID, guid, attributedBody, handle_id, is_from_me, date) VALUES (2, 'i1', ?, 1, 0, ?)",
        (attributed("Pause 2h"), unix_to_apple(now + 5)),
    )
    db.execute(
        "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me, date) VALUES (3, 'i2', 'hi', 2, 0, ?)",
        (unix_to_apple(now + 6),),
    )
    db.commit()
    chat = ChatDB(path)
    assert chat.available()
    r = await chat.wait_receipt("(555) 123-4567", now, timeout=1, poll=0.05)
    assert r["status"] == "delivered" and abs(r["delivered_at"] - r["sent_at"] - 1.5) < 0.01
    assert chat.incoming_after(0, [PHONE]) == [
        {"rowid": 2, "guid": "i1", "from": PHONE, "text": "Pause 2h", "at": pytest.approx(now + 5, abs=0.01)}
    ]
    assert chat.incoming_after(2, [PHONE]) == []
    assert chat.own_handles() == ["bot@icloud.com"]
    # a failed send (red "Not Delivered") is reported as such
    db.execute(
        "INSERT INTO message (ROWID, guid, handle_id, is_from_me, error, date, service) VALUES (4, 'o2', 1, 1, 22, ?, 'iMessage')",
        (unix_to_apple(now + 10),),
    )
    db.commit()
    r = await chat.wait_receipt(PHONE, now + 9, timeout=1, poll=0.05)
    assert r["status"] == "failed" and r["error_code"] == 22
    # nothing there yet -> "unknown" after the timeout
    r = await chat.wait_receipt("+19998887777", now, timeout=0.1, poll=0.05)
    assert r["status"] == "unknown"
    assert not ChatDB(tmp_path / "missing.db").available()


# --------------------------------------------------------------------------- small pieces


def test_parse_commands():
    assert parse_command("pause 2h").seconds == 7200  # type: ignore[union-attr]
    assert parse_command("Snooze 45 min").seconds == 2700  # type: ignore[union-attr]
    assert parse_command("PAUSE").seconds == 3600  # type: ignore[union-attr]
    assert parse_command("stop").seconds is None and parse_command("stop").action == "pause"  # type: ignore[union-attr]
    assert parse_command("Resume!").action == "resume"  # type: ignore[union-attr]
    assert parse_command("critical only").level == "CRITICAL"  # type: ignore[union-attr]
    assert parse_command("normal").level is None  # type: ignore[union-attr]
    assert parse_command("status?").action == "status"  # type: ignore[union-attr]
    for chatter in ("thanks!", "wow nvidia is tanking", "pause the music please now ok", ""):
        assert parse_command(chatter) is None
    assert parse_duration("for 1.5 hours") == 5400


def test_rate_limiter_and_sent_log(tmp_path):
    rl = RateLimiter(2, 60)
    assert rl.allow(0) and rl.allow(1) and not rl.allow(2) and rl.allow(62)
    log = SentLog(tmp_path / "ids.json", keep=3)
    for i in range(5):
        log.add(f"m{i}")
    again = SentLog(tmp_path / "ids.json", keep=3)
    assert "m4" in again and "m0" not in again and len(again.ids) == 3
    assert json.loads((tmp_path / "ids.json").read_text())


async def test_message_read_after_sleep_is_not_sent():
    """A send the relay only reads after waking up was already handled (backup/queue): drop it."""
    agent = RelayAgent("http://monitor.test", SECRET, sender=FakeSender(), receipts=False)
    posted: list[dict[str, Any]] = []

    async def post(msg: dict[str, Any]) -> None:
        posted.append(msg)

    agent.post = post  # type: ignore[method-assign]
    agent._clock_offset = 3600.0  # this Mac's clock is an hour behind the server's: must not matter
    hub_now = time.time() + 3600
    base = {"type": "send", "to": [PHONE], "text": "x", "created": hub_now}
    await agent._on_send({**base, "id": "fresh", "issued": hub_now - 1})
    await agent._on_send({**base, "id": "stale", "issued": hub_now - 30})
    assert posted[0] == {"type": "accepted", "id": "fresh"}
    assert posted[1]["id"] == "stale" and posted[1]["ok"] is False and "asleep" in posted[1]["error"]
