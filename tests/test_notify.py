from __future__ import annotations

from datetime import datetime

import pytest

from news247.config import ChannelConfig, NotifyConfig
from news247.models import Alert, Analysis, NewsItem, PriceMove, Severity, SourceTier
from news247.notify import Dispatcher, in_quiet_hours
from news247.notify.channels import (
    CHANNEL_CLASSES,
    ConsoleNotifier,
    DiscordNotifier,
    NtfyNotifier,
    PushoverNotifier,
    SlackNotifier,
    TelegramNotifier,
    WebhookNotifier,
)
from news247.notify.format import move_headline, plain_body


def news_alert(sev: Severity = Severity.CRITICAL) -> Alert:
    item = NewsItem(
        source="openai-news",
        title="OpenAI launches <agents> & more",
        url="https://openai.com/x",
        tier=SourceTier.PRIMARY,
        published=1000.0,
        detected=1003.0,
    )
    an = Analysis(
        score=88,
        severity=sev,
        tickers=["CRM", "NOW"],
        themes=["ai_disrupts_software"],
        direction="down",
        summary="Agents compete with SaaS",
        llm_used=True,
    )
    return Alert(
        kind="news",
        severity=sev,
        title=item.title,
        body="Confirmed by 3 sources",
        url=item.url,
        tickers=an.tickers,
        item=item,
        analysis=an,
        related=[{"kind": "price", "text": "CRM -2.10% (5m)"}],
    )


def test_plain_body_content():
    body = plain_body(news_alert())
    assert "openai-news (primary)" in body and "caught 3s after publish" in body
    assert "Why it matters: Agents compete with SaaS" in body
    assert "Watch: CRM NOW  ▼ down" in body and "ai disrupts software" in body
    assert "(AI-reviewed)" in body and "Market now: CRM -2.10% (5m)" in body
    assert body.endswith("https://openai.com/x")


def test_move_headlines():
    assert move_headline(PriceMove("NVDA", -3.4, 300, 100, 103.5)) == "▼ NVDA -3.40% in 5m"
    g = PriceMove("GROUP:software", -2.5, 600, 0, 0, members={"CRM": -4.0, "NOW": -3.0, "ADBE": 0.5})
    assert move_headline(g) == "▼ SOFTWARE stocks -2.50% in 10m (2/3 moving together)"
    assert move_headline(PriceMove("TSLA", 8.0, 86400, 216, 200)) == "▲ TSLA +8.00% vs. previous close"


async def test_ntfy_payload(server, http):
    server.on("/", {"id": "1"})
    n = NtfyNotifier({"topic": "my-topic", "server": server.url(""), "token": "tk"}, http, Severity.HIGH)
    await n.send(news_alert())
    req = server.requests[-1]
    assert req["json"]["topic"] == "my-topic" and req["json"]["priority"] == 5
    assert req["json"]["click"] == "https://openai.com/x" and req["json"]["title"].startswith(
        "🔴 OpenAI launches"
    )
    assert req["headers"]["Authorization"] == "Bearer tk"


async def test_telegram_payload_escapes_html(server, http):
    server.on("/botABC/sendMessage", {"ok": True})
    n = TelegramNotifier(
        {"bot_token": "ABC", "chat_id": "42", "api_base": server.url("")}, http, Severity.HIGH
    )
    await n.send(news_alert())
    j = server.requests[-1]["json"]
    assert j["chat_id"] == "42" and j["parse_mode"] == "HTML"
    assert "&lt;agents&gt; &amp; more" in j["text"] and '<a href="https://openai.com/x">' in j["text"]
    assert j["disable_notification"] is False


async def test_discord_slack_webhook_pushover(server, http):
    for p in ("/discord", "/slack", "/hook", "/1/messages.json"):
        server.on(p, (204, ""))
    await DiscordNotifier(
        {"webhook_url": server.url("/discord"), "mention": "@here"}, http, Severity.HIGH
    ).send(news_alert())
    d = server.requests[-1]["json"]
    assert (
        d["content"] == "@here"
        and d["embeds"][0]["color"] == 0xE74C3C
        and d["embeds"][0]["url"] == "https://openai.com/x"
    )
    await SlackNotifier({"webhook_url": server.url("/slack")}, http, Severity.HIGH).send(news_alert())
    s = server.requests[-1]["json"]
    assert s["text"].startswith("🔴") and "<https://openai.com/x|" in s["blocks"][0]["text"]["text"]
    await WebhookNotifier({"url": server.url("/hook"), "headers": {"X-Key": "v"}}, http, Severity.HIGH).send(
        news_alert()
    )
    w = server.requests[-1]
    assert (
        w["json"]["kind"] == "news" and w["json"]["analysis"]["score"] == 88 and w["headers"]["X-Key"] == "v"
    )
    await PushoverNotifier({"token": "t", "user": "u", "api_base": server.url("")}, http, Severity.HIGH).send(
        news_alert()
    )
    body = server.requests[-1]["body"].decode()
    assert "priority=1" in body and "sound=siren" in body


async def test_console(capsys):
    await ConsoleNotifier({}, None, Severity.LOW).send(news_alert())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "CRITICAL" in out and "OpenAI launches" in out and "    Why it matters" in out


def test_channels_validate_options():
    for name, cls in CHANNEL_CLASSES.items():
        if name in ("console", "desktop"):
            continue
        with pytest.raises(ValueError, match=f"notify.{name}: missing"):
            cls({}, None, Severity.HIGH)  # type: ignore[arg-type]


def test_quiet_hours():
    q = {"start": "23:00", "end": "07:00"}
    assert in_quiet_hours(q, datetime(2026, 1, 1, 23, 30))
    assert in_quiet_hours(q, datetime(2026, 1, 1, 6, 59))
    assert not in_quiet_hours(q, datetime(2026, 1, 1, 12, 0))
    assert in_quiet_hours({"start": "12:00", "end": "13:00"}, datetime(2026, 1, 1, 12, 30))
    assert not in_quiet_hours(None)


async def test_dispatcher_routing_rate_limit_and_failure_isolation(server, http):
    server.on("/ok", (204, ""))
    server.on("/fail", (500, "down"))
    cfg = NotifyConfig(
        channels=[
            ChannelConfig("webhook", True, Severity.MEDIUM, {"url": server.url("/ok")}),
            ChannelConfig("discord", True, Severity.HIGH, {"webhook_url": server.url("/fail")}),
            ChannelConfig("slack", True, Severity.CRITICAL, {"webhook_url": server.url("/ok")}),
            ChannelConfig("telegram", True, Severity.HIGH, {}),  # misconfigured -> dropped at start
            ChannelConfig("ntfy", False, Severity.HIGH, {"topic": "x"}),  # disabled
        ],
        rate_limit_per_minute=1,
    )
    d = Dispatcher(cfg, http)
    assert [c.name for c in d.channels] == ["webhook", "discord", "slack"]
    res = await d.dispatch(news_alert(Severity.HIGH))
    assert res["webhook"] == "ok" and res["discord"].startswith("error") and "slack" not in res
    # second HIGH alert in the same minute: remote channels rate-limited, local webhook still gets it
    res2 = await d.dispatch(news_alert(Severity.HIGH))
    assert list(res2) == ["webhook"] and d.suppressed == 1
    # CRITICAL always goes through
    res3 = await d.dispatch(news_alert(Severity.CRITICAL))
    assert set(res3) == {"webhook", "discord", "slack"}
    assert d.describe()[1]["failed"] == 2


async def test_dispatcher_quiet_hours(monkeypatch, http):
    import news247.notify as mod

    cfg = NotifyConfig(
        channels=[
            ChannelConfig("ntfy", True, Severity.HIGH, {"topic": "t"}),
            ChannelConfig("console", True, Severity.LOW, {}),
        ],
        quiet_hours={"start": "00:00", "end": "23:59", "min_severity": "critical"},
    )
    d = Dispatcher(cfg, http)
    monkeypatch.setattr(mod, "in_quiet_hours", lambda q: True)
    assert [c.name for c in d.targets(news_alert(Severity.HIGH))] == ["console"]
    assert [c.name for c in d.targets(news_alert(Severity.CRITICAL))] == ["ntfy", "console"]
