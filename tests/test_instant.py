"""Instant-delivery features: VIP sources, Telegram squawks, iMessage/SMS channels."""

from __future__ import annotations

import time

import pytest

from news247.config import ConfigError, as_bool, build_config
from news247.models import Alert, Analysis, NewsItem, Severity, SourceTier
from news247.notify.channels import IMessageNotifier, TwilioSMSNotifier
from news247.notify.format import sms_text
from news247.sources import SourceContext
from news247.sources.rss import RSSSource
from news247.sources.telegram import TelegramChannelSource, parse_channel_page

from .test_engine import make_engine

CTX = SourceContext(http=None, user_agent="t")  # type: ignore[arg-type]

TG_PAGE = """
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="FastNews/101">
<div class="tgme_widget_message_text js-message_text" dir="auto"><b>OPENAI UNVEILS AGENT PLATFORM FOR ENTERPRISES</b><br/>Shares of $CRM fall in late trading</div>
<a class="tgme_widget_message_date" href="https://t.me/FastNews/101"><time datetime="2026-10-08T17:00:01+00:00" class="time">17:00</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="FastNews/102">
<a class="tgme_widget_message_photo_wrap"></a>
<a class="tgme_widget_message_date" href="https://t.me/FastNews/102"><time datetime="2026-10-08T17:01:00+00:00"></time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="FastNews/103">
<div class="tgme_widget_message_text js-message_text" dir="auto">FED&#39;S POWELL: NO RUSH TO CUT</div>
<time datetime="2026-10-08T17:02:00+00:00"></time>
</div></div>
"""


# --------------------------------------------------------------------------- VIP floor


async def test_vip_floor_applied_by_engine(cfg):
    eng, cap = make_engine(cfg)
    src = RSSSource(
        {"name": "openai-news", "url": "u", "tier": "primary", "entities": ["OpenAI"], "alert_floor": "high"},
        CTX,
    )
    item = src.make_item(
        title="An update on our work", url="https://openai.com/index/update", published=time.time() - 2
    )
    await eng.on_item(item)
    await eng.drain()
    assert len(cap.alerts) == 1 and cap.alerts[0].severity is Severity.HIGH
    assert any("VIP source" in r for r in cap.alerts[0].analysis.reasons)


async def test_vip_never_waits_for_llm(cfg):
    import asyncio

    from .test_engine import FakeLLM

    cfg.llm.budget_ms = 5000
    eng, cap = make_engine(cfg, llm=FakeLLM(None, delay=3))
    src = RSSSource(
        {"name": "openai-news", "url": "u", "tier": "primary", "entities": ["OpenAI"], "alert_floor": "high"},
        CTX,
    )
    await eng.on_item(
        src.make_item(title="An update on our work", url="https://openai.com/x", published=time.time())
    )
    await asyncio.sleep(0.05)
    assert cap.alerts, "VIP item must be pushed before the model answers"
    for t in list(eng._tasks):
        t.cancel()


async def test_vip_authors_and_mute(cfg):
    cfg.scoring.mute = []
    eng, cap = make_engine(cfg)
    tg = TelegramChannelSource(
        {"name": "tg", "channels": ["FastNews"], "vip_authors": ["@FastNews"], "vip_floor": "critical"}, CTX
    )
    items = tg.parse(TG_PAGE)
    assert all(i.extra["floor"] == "CRITICAL" for i in items)
    other = TelegramChannelSource({"name": "tg2", "channels": ["Other"], "vip_authors": ["someone"]}, CTX)
    assert "floor" not in other.parse(TG_PAGE)[0].extra
    # muted items never get the floor
    cfg2 = build_config({"scoring": {"mute": ["(?i)powell"]}, "notify": {"console": {"enabled": False}}})
    eng2, cap2 = make_engine(cfg2)
    await eng2.on_item(items[1])
    await eng2.drain()
    assert cap2.alerts == []


def test_default_sources_mark_ai_labs_vip():
    cfg = build_config({})
    by = {s["name"]: s for s in cfg.sources}
    for name in ("openai-news", "anthropic-news"):
        assert str(by[name].get("alert_floor", "")).lower() == "high", name


# --------------------------------------------------------------------------- Telegram


def test_parse_channel_page():
    posts = parse_channel_page(TG_PAGE)
    assert [p["id"] for p in posts] == [101, 103]  # photo-only post skipped
    assert (
        posts[0]["text"]
        == "OPENAI UNVEILS AGENT PLATFORM FOR ENTERPRISES\nShares of $CRM fall in late trading"
    )
    assert posts[1]["text"] == "FED'S POWELL: NO RUSH TO CUT"


def test_telegram_items():
    src = TelegramChannelSource({"name": "tg", "channels": ["@FastNews"]}, CTX)
    items = src.parse(TG_PAGE)
    first = items[0]
    assert first.title == "@FastNews: OPENAI UNVEILS AGENT PLATFORM FOR ENTERPRISES"
    assert first.summary == "Shares of $CRM fall in late trading"
    assert first.url == "https://t.me/FastNews/101" and first.uid == "tg:fastnews:101"
    assert first.published == 1791478801
    with pytest.raises(ValueError):
        TelegramChannelSource({"name": "x"}, CTX)


async def test_telegram_fetch_tolerates_one_bad_channel(server, http, monkeypatch):
    server.on("/s/FastNews", (200, TG_PAGE))
    src = TelegramChannelSource(
        {"name": "tg", "channels": ["FastNews", "Missing"]}, SourceContext(http=http, user_agent="t")
    )
    real_get = http.get

    async def get(url, **kw):
        return await real_get(url.replace("https://t.me", server.url("")), **kw)

    monkeypatch.setattr(http, "get", get)
    assert len(await src.fetch()) == 2
    src.channels = ["Missing"]
    with pytest.raises(Exception):  # noqa: B017
        await src.fetch()


# --------------------------------------------------------------------------- texts


def alert() -> Alert:
    item = NewsItem(
        source="openai-news",
        title="Introducing ChatGPT agents for finance",
        url="https://openai.com/x",
        tier=SourceTier.PRIMARY,
        published=1000.0,
        detected=1002.0,
    )
    an = Analysis(
        score=80,
        severity=Severity.CRITICAL,
        tickers=["CRM", "INTU", "NOW"],
        direction="down",
        summary="Targets SaaS",
    )
    return Alert(
        kind="news",
        severity=Severity.CRITICAL,
        title=item.title,
        body="",
        url=item.url,
        item=item,
        analysis=an,
        related=[{"kind": "price", "text": "CRM -1.20% (5m)"}],
    )


def test_sms_text_is_compact():
    t = sms_text(alert())
    assert t.splitlines() == [
        "🔴 Introducing ChatGPT agents for finance",
        "▼ CRM INTU NOW · openai-news · 2s after post",
        "Targets SaaS",
        "Now: CRM -1.20% (5m)",
        "https://openai.com/x",
    ]
    long = alert()
    long.title = "x" * 1000
    out = sms_text(long, limit=200)
    assert len(out) <= 200 and out.endswith("https://openai.com/x")


class FakeProc:
    def __init__(self, rc: int, err: bytes = b"") -> None:
        self.returncode, self.err = rc, err

    async def communicate(self):
        return b"", self.err

    def kill(self):
        pass


async def test_imessage_runs_osascript_with_argv(monkeypatch):
    import news247.notify.channels as ch

    calls = []

    async def fake_exec(*args, **kw):
        calls.append(args)
        return FakeProc(0)

    monkeypatch.setattr(ch.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ch.asyncio, "create_subprocess_exec", fake_exec)
    n = IMessageNotifier({"to": "+15551234567, me@icloud.com"}, None, Severity.HIGH)  # type: ignore[arg-type]
    await n.send(alert())
    assert [c[3] for c in calls] == ["+15551234567", "me@icloud.com"]
    assert calls[0][0] == "osascript" and calls[0][1] == "-e" and "on run argv" in calls[0][2]
    assert calls[0][4].startswith("🔴 Introducing ChatGPT") and calls[0][5] == "imessage"
    # the message text is an argument, never part of the script
    assert "Introducing" not in calls[0][2]


async def test_imessage_auto_falls_back_to_sms(monkeypatch):
    import news247.notify.channels as ch

    services = []

    async def fake_exec(*args, **kw):
        services.append(args[5])
        return (
            FakeProc(1, b"execution error: Not authorized (-1743)") if args[5] == "imessage" else FakeProc(0)
        )

    monkeypatch.setattr(ch.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ch.asyncio, "create_subprocess_exec", fake_exec)
    n = IMessageNotifier({"to": "+15551234567", "service": "auto"}, None, Severity.HIGH)  # type: ignore[arg-type]
    await n.send(alert())
    assert services == ["imessage", "sms"]
    strict = IMessageNotifier({"to": "+15551234567"}, None, Severity.HIGH)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Automation"):
        await strict.send(alert())


async def test_imessage_requires_macos(monkeypatch):
    import news247.notify.channels as ch

    monkeypatch.setattr(ch.platform, "system", lambda: "Linux")
    n = IMessageNotifier({"to": "+15551234567"}, None, Severity.HIGH)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="macOS"):
        await n.send(alert())
    with pytest.raises(ValueError):
        IMessageNotifier({"to": "+1555", "service": "carrier pigeon"}, None, Severity.HIGH)  # type: ignore[arg-type]


async def test_twilio_payload(server, http):
    server.on("/2010-04-01/Accounts/AC1/Messages.json", (201, "{}"))
    n = TwilioSMSNotifier(
        {
            "account_sid": "AC1",
            "auth_token": "tok",
            "from": "+15550000000",
            "to": ["+15551234567"],
            "api_base": server.url(""),
        },
        http,
        Severity.HIGH,
    )
    await n.send(alert())
    req = server.requests[-1]
    body = req["body"].decode()
    assert "To=%2B15551234567" in body and "From=%2B15550000000" in body and "Body=" in body
    assert req["headers"]["Authorization"].startswith("Basic ")
    with pytest.raises(ValueError, match="from"):
        TwilioSMSNotifier({"account_sid": "a", "auth_token": "b", "to": "c"}, http, Severity.HIGH)


def test_env_driven_enable_flags(monkeypatch):
    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_TO", "+15551234567")
    cfg = build_config(
        {"notify": {"imessage": {"enabled": "${IMESSAGE_ENABLED:-false}", "to": "${IMESSAGE_TO}"}}}
    )
    im = next(c for c in cfg.notify.channels if c.name == "imessage")
    assert im.enabled is True and im.options["to"] == "+15551234567"
    monkeypatch.delenv("IMESSAGE_ENABLED")
    cfg = build_config({"notify": {"imessage": {"enabled": "${IMESSAGE_ENABLED:-false}", "to": "x"}}})
    assert next(c for c in cfg.notify.channels if c.name == "imessage").enabled is False
    assert as_bool("yes") and not as_bool("off") and as_bool("", default=False) is False
    with pytest.raises(ConfigError):
        as_bool("maybe")


async def test_bluebubbles_send_and_new_chat_fallback(server, http):
    from news247.notify.channels import BlueBubblesNotifier

    calls = {"n": 0}

    def text_handler(request):
        from aiohttp import web

        calls["n"] += 1
        return web.json_response({"status": 500, "error": "chat not found"}, status=500)

    server.on("/api/v1/message/text", text_handler)
    server.on("/api/v1/chat/new", {"status": 200})
    n = BlueBubblesNotifier(
        {"server": server.url(""), "password": "pw", "to": "+15551234567"}, http, Severity.HIGH
    )
    await n.send(alert())
    first, second = server.requests[-2], server.requests[-1]
    assert first["path"] == "/api/v1/message/text" and first["query"] == {"password": "pw"}
    assert (
        first["json"]["chatGuid"] == "iMessage;-;+15551234567" and first["json"]["method"] == "apple-script"
    )
    assert first["json"]["tempGuid"] and first["json"]["message"].startswith("🔴")
    assert second["path"] == "/api/v1/chat/new" and second["json"]["addresses"] == ["+15551234567"]
    assert second["json"]["tempGuid"] != first["json"]["tempGuid"]


async def test_sendblue_payload_and_error(server, http):
    from news247.notify.channels import SendblueNotifier

    server.on("/api/send-message", {"status": "QUEUED"})
    n = SendblueNotifier(
        {
            "api_key_id": "k",
            "api_secret": "s",
            "to": "+15551234567",
            "from_number": "+15550001111",
            "api_base": server.url(""),
        },
        http,
        Severity.HIGH,
    )
    await n.send(alert())
    req = server.requests[-1]
    assert req["headers"]["sb-api-key-id"] == "k" and req["headers"]["sb-api-secret-key"] == "s"
    assert req["json"]["number"] == "+15551234567" and req["json"]["from_number"] == "+15550001111"
    assert "Introducing ChatGPT agents" in req["json"]["content"]
    server.on("/api/send-message", {"status": "ERROR", "error_message": "contact not verified"})
    with pytest.raises(RuntimeError, match="not verified"):
        await n.send(alert())


async def test_blooio_payload(server, http):
    from news247.notify.channels import BlooioNotifier

    server.on("/v2/api/chats/+15551234567/messages", {"ok": True})
    n = BlooioNotifier(
        {"api_key": "key", "to": "+15551234567", "api_base": server.url("")}, http, Severity.HIGH
    )
    await n.send(alert())
    req = server.requests[-1]
    assert req["headers"]["Authorization"] == "Bearer key" and req["headers"]["Idempotency-Key"]
    assert req["json"]["text"].startswith("🔴")


async def test_textbelt_strips_links_and_reports_errors(server, http):
    from news247.notify.channels import TextbeltNotifier

    server.on("/text", {"success": True, "quotaRemaining": 9, "textId": "1"})
    n = TextbeltNotifier({"key": "k", "to": "5551234567", "api_base": server.url("")}, http, Severity.HIGH)
    await n.send(alert())
    body = server.requests[-1]["body"].decode()
    assert "phone=5551234567" in body and "key=k" in body and "openai.com" not in body
    server.on("/text", {"success": False, "error": "Out of quota"})
    with pytest.raises(RuntimeError, match="Out of quota"):
        await n.send(alert())


async def test_imessage_tries_older_syntax_and_remembers(monkeypatch):
    import news247.notify.channels as ch

    used = []

    async def fake_exec(*args, **kw):
        idx = ch.IMESSAGE_SCRIPTS.index(args[2])
        used.append(idx)
        return (
            FakeProc(0)
            if idx == 1
            else FakeProc(1, b"syntax error: Expected class name but found identifier. (-2741)")
        )

    monkeypatch.setattr(ch.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ch.asyncio, "create_subprocess_exec", fake_exec)
    n = IMessageNotifier({"to": "+15551234567"}, None, Severity.HIGH)  # type: ignore[arg-type]
    await n.send(alert())
    await n.send(alert())
    assert used == [0, 1, 1]  # second send goes straight to the syntax that worked

    async def always_fail(*args, **kw):
        return FakeProc(1, b"Can't get participant")

    monkeypatch.setattr(ch.asyncio, "create_subprocess_exec", always_fail)
    with pytest.raises(RuntimeError, match="could not send"):
        await IMessageNotifier({"to": "+1"}, None, Severity.HIGH).send(alert())  # type: ignore[arg-type]
