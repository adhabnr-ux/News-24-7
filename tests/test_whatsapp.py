"""Self-hosted WhatsApp sending (linked device) and the official Cloud API channel."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.config import build_config
from news247.engine import Engine
from news247.models import Alert, Analysis, NewsItem, Severity, SourceTier
from news247.notify import Dispatcher
from news247.notify.format import whatsapp_text
from news247.storage import Storage
from news247.web.server import WebServer
from news247.whatsapp.session import (
    BANNED,
    CONNECTED,
    DISCONNECTED,
    ERROR,
    LOGGED_OUT,
    NOT_INSTALLED,
    PAIRING,
    Events,
    SessionConfig,
    WhatsAppSession,
)

from .conftest import CaptureNotifier

ME = "+16235550146"  # the person receiving alerts
BOT = "15550001111"  # the linked (sending) account


class FakeBackend:
    """Stands in for whatsmeow: records sends, lets tests fire WhatsApp events."""

    instances: list[FakeBackend] = []

    def __init__(self, fail_start: str = "") -> None:
        self.ev: Events | None = None
        self.done: asyncio.Future[None] | None = None
        self.sent: list[tuple[str, str, float]] = []
        self.fail_start = fail_start
        self.send_error = ""
        self.logged_out = False
        FakeBackend.instances.append(self)

    async def start(self, events: Events) -> Awaitable[None]:
        if self.fail_start:
            raise RuntimeError(self.fail_start)
        self.ev = events
        self.done = asyncio.get_running_loop().create_future()
        return self.done

    async def stop(self) -> None:
        if self.done and not self.done.done():
            self.done.set_result(None)

    async def pair_code(self, phone: str) -> str:
        return f"ABCD-{phone[-4:]}"

    async def send_text(self, to: str, text: str) -> str:
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.sent.append((to, text, time.monotonic()))
        return f"MSG{len(self.sent)}"

    async def logout(self) -> None:
        self.logged_out = True

    # helpers
    def link(self) -> None:
        assert self.ev is not None
        self.ev.paired(BOT, "News247 Bot")
        self.ev.connected(BOT, "News247 Bot")


async def wait_for(cond: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def session():
    FakeBackend.instances = []
    s = WhatsAppSession(FakeBackend, SessionConfig(min_interval_s=0, retry_min_s=0.05, connect_wait_s=1))
    s.recipients = [ME]
    await s.start()
    await wait_for(lambda: FakeBackend.instances and FakeBackend.instances[-1].ev)
    yield s
    await s.stop()


def news_alert(title: str = "*OPENAI LAUNCHES ENTERPRISE AGENTS FOR FINANCE & LEGAL") -> Alert:
    item = NewsItem(
        source="x", title=title, url="https://x.com/1", tier=SourceTier.SOCIAL, published=time.time() - 3
    )
    an = Analysis(
        score=90,
        severity=Severity.CRITICAL,
        tickers=["CRM", "INTU"],
        direction="down",
        summary="AI agents vs SaaS",
    )
    return Alert(
        kind="news", severity=Severity.CRITICAL, title=title, body="", url=item.url, item=item, analysis=an
    )


# --------------------------------------------------------------------------- session


async def test_pairing_then_sending_with_receipts(session):
    be = FakeBackend.instances[-1]
    be.ev.qr(["2@qr-payload"])  # type: ignore[union-attr]
    assert session.state == PAIRING and session.status()["qr"] == "2@qr-payload"
    with pytest.raises(RuntimeError, match="isn't linked yet"):
        await session.send([ME], "too early")
    assert await session.request_pair_code("+1 555 000 1111") == "ABCD-1111"
    be.link()
    st = session.status()
    assert (
        st["state"] == CONNECTED
        and st["me"] == "+15550001111"
        and st["qr"] is None
        and st["pair_code"] is None
    )
    ids = await session.send([ME], "🔴 hello", title="🔴 hello")
    assert be.sent[0][:2] == ("16235550146", "🔴 hello") and ids == ["MSG1"]
    be.ev.receipt(["MSG1"], "delivered", "16235550146")  # type: ignore[union-attr]
    be.ev.receipt(["MSG1"], "read", "16235550146")  # type: ignore[union-attr]
    st = session.status()
    assert st["recent"][0]["status"] == "read" and st["stats"]["delivered"] == 1 and st["stats"]["read"] == 1
    assert st["delivery_median_s"] is not None
    with pytest.raises(RuntimeError, match="already linked"):
        await session.request_pair_code("+15550001111")


async def test_spacing_and_hourly_cap():
    FakeBackend.instances = []
    s = WhatsAppSession(FakeBackend, SessionConfig(min_interval_s=0.15, max_per_hour=3, retry_min_s=0.05))
    await s.start()
    await wait_for(lambda: FakeBackend.instances and FakeBackend.instances[-1].ev)
    be = FakeBackend.instances[-1]
    be.link()
    await asyncio.gather(*(s.send([ME], f"m{i}") for i in range(3)))
    times = [t for _, _, t in be.sent]
    assert [text for _, text, _ in be.sent] == ["m0", "m1", "m2"]  # in order
    assert all(b - a >= 0.14 for a, b in zip(times, times[1:]))  # spaced out
    with pytest.raises(RuntimeError, match="hourly cap of 3"):
        await s.send([ME], "m3")
    await s.send([ME], "reply", count=False)  # command replies don't count against the cap
    await s.stop()


async def test_commands_only_from_recipients(session):
    be = FakeBackend.instances[-1]
    be.link()
    got: list[tuple[str, str]] = []

    async def on_inbound(sender: str, text: str) -> str | None:
        got.append((sender, text))
        return "⏸ paused"

    session.on_inbound = on_inbound
    be.ev.message("16235550146", "pause 2h", "IN1", False)  # type: ignore[union-attr]
    be.ev.message(
        "16235550146", "pause 2h", "IN1", False
    )  # duplicate delivery: ignored  # type: ignore[union-attr]
    be.ev.message("447700900123", "pause", "IN2", False)  # a stranger  # type: ignore[union-attr]
    be.ev.message(BOT, "note to self", "IN3", True)  # our own message  # type: ignore[union-attr]
    await wait_for(lambda: be.sent)
    assert got == [("16235550146", "pause 2h")]
    assert be.sent[0][:2] == ("16235550146", "⏸ paused")


async def test_logout_offers_new_pairing_and_reports(session):
    states: list[tuple[str, str]] = []
    session.on_state = lambda st, detail: states.append((st, detail))
    first = FakeBackend.instances[-1]
    first.link()
    first.ev.logged_out("401")  # type: ignore[union-attr]
    assert session.state == LOGGED_OUT and session.me == ""
    await wait_for(lambda: len(FakeBackend.instances) == 2 and FakeBackend.instances[-1].ev)  # fresh engine
    assert (LOGGED_OUT, "unlinked from the phone (401); pair again on the Setup page") in states
    FakeBackend.instances[-1].ev.qr(["2@new"])  # type: ignore[union-attr]
    assert session.state == PAIRING


async def test_engine_crash_is_restarted():
    calls = {"n": 0}

    def factory() -> FakeBackend:
        calls["n"] += 1
        return FakeBackend(fail_start="dial failed" if calls["n"] == 1 else "")

    s = WhatsAppSession(factory, SessionConfig(retry_min_s=0.05))
    await s.start()
    await wait_for(lambda: s.state == ERROR)
    assert "dial failed" in s.last_error
    await wait_for(lambda: calls["n"] >= 2 and FakeBackend.instances[-1].ev)
    FakeBackend.instances[-1].link()
    assert s.state == CONNECTED
    await s.stop()


async def test_send_waits_for_reconnect(session):
    be = FakeBackend.instances[-1]
    be.link()
    be.ev.disconnected("")  # type: ignore[union-attr]
    assert session.state == DISCONNECTED
    asyncio.get_running_loop().call_later(0.2, lambda: be.ev.connected(BOT, ""))  # type: ignore[union-attr]
    await session.send([ME], "after blip")
    assert be.sent[-1][1] == "after blip"


async def test_ban_and_self_link_warning(session):
    be = FakeBackend.instances[-1]
    session.recipients = ["+1 555 000 1111"]  # alerts would go to the linked account itself
    be.link()
    assert "Message yourself" in session.status()["warnings"][0]
    be.ev.banned("temporary ban (code 101), expires in 3600s")  # type: ignore[union-attr]
    assert session.state == BANNED
    with pytest.raises(RuntimeError, match="restricted"):
        await session.send([ME], "x")


async def test_unlink(session):
    be = FakeBackend.instances[-1]
    be.link()
    await session.unlink()
    assert be.logged_out and session.state == LOGGED_OUT


def test_not_installed_message():
    s = WhatsAppSession(None)
    assert s.state == NOT_INSTALLED
    with pytest.raises(RuntimeError, match="isn't installed"):
        asyncio.run(s.send([ME], "x"))


# --------------------------------------------------------------------------- channel + dispatcher + engine


def wa_config(tmp_path: Path, **web: Any) -> Any:
    return build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {
                "console": {"enabled": False},
                "whatsapp": {"enabled": True, "to": ME, "min_interval_s": 0},
            },
            "web": {"token": "tok", **web},
        }
    )


async def test_channel_formats_and_falls_back_to_backup(tmp_path):
    FakeBackend.instances = []
    disp = Dispatcher(wa_config(tmp_path).notify, None)  # type: ignore[arg-type]
    wa = disp.channels[0]
    backup = CaptureNotifier(Severity.HIGH)
    backup.name, backup.backup = "ntfy", True
    disp.channels.append(backup)
    # not linked yet: the alert goes to the backup instead
    out = await disp.dispatch(news_alert())
    assert out["whatsapp"].startswith("error") and out["ntfy"] == "ok"
    # linked: WhatsApp gets it, bold headline (the squawk's '*' removed), backup stays quiet
    wa.session.set_backend_factory(FakeBackend)  # type: ignore[attr-defined]
    await wa.session.start()  # type: ignore[attr-defined]
    await wait_for(lambda: FakeBackend.instances and FakeBackend.instances[-1].ev)
    FakeBackend.instances[-1].link()
    out = await disp.dispatch(news_alert())
    assert out == {"whatsapp": "ok"} and len(backup.alerts) == 1
    text = FakeBackend.instances[-1].sent[0][1]
    assert text.startswith("*🔴 OPENAI LAUNCHES ENTERPRISE AGENTS FOR FINANCE & LEGAL*\n▼ CRM INTU")
    await wa.session.stop()  # type: ignore[attr-defined]


async def test_engine_web_pairing_and_unlink_alert(tmp_path):
    FakeBackend.instances = []
    eng = Engine(wa_config(tmp_path), storage=Storage(tmp_path / "t.db"), sources=[])
    assert eng.whatsapp is not None
    eng.whatsapp.set_backend_factory(FakeBackend)  # what attach() does when the engine is installed
    eng.whatsapp.cfg.retry_min_s = 0.05
    capture = CaptureNotifier(Severity.LOW)
    eng.dispatcher.channels.append(capture)
    await eng.whatsapp.start()
    await wait_for(lambda: FakeBackend.instances and FakeBackend.instances[-1].ev)
    FakeBackend.instances[-1].ev.qr(["2@pair-me"])  # type: ignore[union-attr]
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/whatsapp")) as r:
                assert r.status == 401  # pairing is behind the dashboard token
            async with s.get(srv.make_url("/api/whatsapp?token=tok")) as r:
                st = await r.json()
            assert st["state"] == PAIRING and st["qr"] == "2@pair-me" and st["writable"]
            async with s.post(
                srv.make_url("/api/whatsapp/pair?token=tok"), json={"phone": "+1 555 000 1111"}
            ) as r:
                assert (await r.json()) == {"code": "ABCD-1111"}
            FakeBackend.instances[-1].link()
            res = await eng.send_test()
            assert res["whatsapp"] == "ok" and "News247 test" in FakeBackend.instances[-1].sent[-1][1]
            # the phone unlinks News247: you're told through the other channels
            FakeBackend.instances[-1].ev.logged_out("401")  # type: ignore[union-attr]
            await wait_for(lambda: any("unlinked" in a.title for a in capture.alerts))
            async with s.post(srv.make_url("/api/whatsapp/pair?token=tok"), json={"phone": "12"}) as r:
                assert r.status == 409
    finally:
        await srv.close()
        await eng.whatsapp.stop()


async def test_phone_commands_over_whatsapp(tmp_path):
    FakeBackend.instances = []
    eng = Engine(wa_config(tmp_path), storage=Storage(tmp_path / "t.db"), sources=[])
    eng.whatsapp.set_backend_factory(FakeBackend)  # type: ignore[union-attr]
    await eng.whatsapp.start()  # type: ignore[union-attr]
    await wait_for(lambda: FakeBackend.instances and FakeBackend.instances[-1].ev)
    be = FakeBackend.instances[-1]
    be.link()
    be.ev.message("16235550146", "critical", "c1", False)  # type: ignore[union-attr]
    await wait_for(lambda: be.sent)
    assert "only CRITICAL" in be.sent[0][1] and eng.dispatcher.phone_min is Severity.CRITICAL
    await eng.whatsapp.stop()  # type: ignore[union-attr]


def test_whatsapp_text():
    assert whatsapp_text(news_alert("Fed cuts rates by 50bp")).startswith("*🔴 Fed cuts rates by 50bp*\n")


async def test_real_engine_starts_and_stops(tmp_path):
    """With neonize installed: the real whatsmeow engine boots, reaches WhatsApp (QR offered) or
    reports why it can't (e.g. no network), and shuts down cleanly."""
    pytest.importorskip("neonize")
    from news247.whatsapp.neonize_backend import NeonizeBackend, available, qr_svg

    assert available() == (True, "")
    assert qr_svg("2@abc,def").startswith("<svg")
    s = WhatsAppSession(lambda: NeonizeBackend(tmp_path / "session.db"), SessionConfig(retry_min_s=0.5))
    await s.start()
    await wait_for(lambda: s.state in (PAIRING, ERROR, DISCONNECTED), timeout=30)
    if s.state == PAIRING:
        assert s.status(qr_svg)["qr_svg"].startswith("<svg")
    else:
        assert s.last_error
    t0 = time.monotonic()
    await s.stop()
    assert time.monotonic() - t0 < 15
