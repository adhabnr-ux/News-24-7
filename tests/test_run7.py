"""Run 7 of the desk: how far a stock moved before its alert (priced in?), alerts by time of day,
the typical path after alerts like this, session context (VWAP, the 9:35 price), the earnings
week, per-phone forwarding to Discord/Telegram/ntfy, automatic move alerts, and the notification's
mute button."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.desk import Desk, _mask_forward, clean_forward, forward_request
from news247.dossier import (
    base_rate,
    earnings_week,
    priced_in_record,
    session_context,
    time_bucket,
    time_of_day_record,
)
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")
HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFG-123"


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


def graded_alert(created, pre, h1, close, sign=1):
    a = news()
    a.created = created
    a.edge = {"pre": {"ACME": pre}, "outcome": {"ACME": {"h1": h1, "close": close, "sign": sign}}}
    return a


# --------------------------------------------------------------------------- priced in?


async def test_alerts_record_how_far_the_stock_moved_before(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for dt, px in ((1200, 100.0), (900, 100.5), (300, 102.0), (5, 103.0)):
        eng.live_quotes.update("ACME", px, 100.0, now - dt, "tape")
    assert eng.pre_move(["ACME", "BETA"], now) == {"ACME": 2.49}  # from 100.50 fifteen minutes before
    a = news()
    a.created = now
    await eng.publish(a, push=False)
    assert eng.storage.get_alert(a.id)["edge"]["pre"] == {"ACME": 2.49}


def test_priced_in_and_time_of_day_records(tmp_path):
    eng = semis_engine(tmp_path)
    now = at(20, 0)
    for created, pre, h1, close in [
        (at(9, 45), 0.3, 1.0, 2.0),
        (at(9, 50), 2.5, -0.5, -1.0),
        (at(12, 0), 3.0, 0.2, -0.3),
        (at(8, 0), -1.5, 1.0, 1.5),
    ]:
        eng.storage.add_alert(graded_alert(created, pre, h1, close))
    rows = {r["bucket"]: r for r in priced_in_record(eng, 30, now)["rows"]}
    assert rows["fresh"]["h1"]["hit"] == 1.0 and rows["already moved its way"]["h1"]["n"] == 2
    assert (
        rows["already moved its way"]["close"]["hit"] == 0.0
        and rows["moved against it first"]["close"]["median"] == 1.5
    )
    tod = {r["bucket"]: r for r in time_of_day_record(eng, 30, now)["rows"]}
    assert set(tod) == {"the open", "midday", "pre-market"} and tod["the open"]["h1"]["n"] == 2
    assert (
        time_bucket(at(16, 30)) == "after hours"
        and time_bucket(at(23, 0)) == "overnight"
        and time_bucket(at(12, 0, d=17)) == "weekend"
    )


def test_base_rate_carries_the_typical_path():
    rates = {"options": {"h1": {"n": 10, "hit": 0.6, "median": 0.8}, "close": {"n": 9, "hit": 0.55, "median": 1.4},
                         "d1": {"n": 8, "hit": 0.5, "median": 2.0}, "d5": {"n": 3, "hit": 0.6, "median": 4.0}}}  # fmt: skip
    b = base_rate(
        rates,
        SimpleNamespace(
            kind="options", severity=SimpleNamespace(name="HIGH"), tickers=["ACME"], edge={}, item=None
        ),
    )
    assert [(p["at"], p["median"]) for p in b["path"]] == [
        ("1h", 0.8),
        ("close", 1.4),
        ("next day", 2.0),
    ]  # 5 days: too few


# --------------------------------------------------------------------------- session context, earnings week


def test_session_context(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=6.0)
    eng.ignition = SimpleNamespace(latest=lambda s, now=None: {"vwap": 104.0} if s == "ACME" else None)
    s = session_context(eng, "ACME", now)
    assert s["vwap"] == 104.0 and s["vs_vwap"] == pytest.approx(1.92, abs=0.01)
    assert session_context(eng, "ZZZZ", now) is None


def test_earnings_week(tmp_path):
    eng = semis_engine(tmp_path)
    nxt = {
        "ACME": ("2026-10-15", 1),
        "BETA": ("2026-10-15", 1),
        "DLTA": ("2026-10-16", 2),
        "GAMA": ("2026-10-30", 16),
    }
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(
            next={s: {} for s in nxt},
            lookup=lambda s, t=None: {"date": nxt[s][0], "when": "after the close", "in_days": nxt[s][1]},
        )
    )
    w = earnings_week(eng, at(12, 0))
    assert [d["date"] for d in w["days"]] == ["2026-10-15"]  # DLTA is under $2B, GAMA is weeks out
    assert [r["symbol"] for r in w["days"][0]["rows"]] == ["BETA", "ACME"]  # biggest first
    assert earnings_week(eng, at(12, 0), min_cap=5e8)["days"][1]["rows"][0]["symbol"] == "DLTA"
    eng.trends = None
    assert earnings_week(eng)["enabled"] is False


# --------------------------------------------------------------------------- forwarding


def test_forwards_are_checked_and_masked():
    assert clean_forward({"kind": "discord", "url": HOOK}) == {"kind": "discord", "url": HOOK}
    for bad in ({"kind": "discord", "url": "https://evil.example/api/webhooks/1/x"}, {"kind": "discord", "url": "http://discord.com/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz"},
                {"kind": "telegram", "token": "nope", "chat": "1"}, {"kind": "ntfy", "topic": "a b"}, {"kind": "smtp"}, "x"):  # fmt: skip
        with pytest.raises(ValueError):
            clean_forward(bad)
    tg = clean_forward(
        {"kind": "telegram", "token": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789", "chat": "-100123456"}
    )
    url, kw = forward_request(tg, {"title": "🔔 ACME", "body": "up", "url": "https://x.test"})
    assert (
        url.startswith("https://api.telegram.org/bot123456789:")
        and kw["json"]["text"] == "🔔 ACME\nup\nhttps://x.test"
    )
    url, kw = forward_request(
        clean_forward({"kind": "ntfy", "topic": "my-foretape"}), {"title": "🔔 ACME", "body": "up"}
    )
    assert (
        url == "https://ntfy.sh/my-foretape"
        and kw["data"] == "🔔 ACME\nup".encode()
        and kw["headers"]["Title"] == "ACME"
    )
    assert _mask_forward(clean_forward({"kind": "discord", "url": HOOK})) == {
        "kind": "discord",
        "where": "Discord webhook …FG-123",
    }
    assert (
        forward_request({"kind": "discord", "url": "https://evil.example/x"}, {}) is None
    )  # never posts outside the three


async def test_alerts_and_personal_pushes_are_forwarded(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    sent = []
    real_post = eng.http.post

    async def post(url, **kw):
        if url.startswith("https://discord.com/"):
            sent.append((url, kw["json"]["content"]))
            return None
        return await real_post(url, **kw)

    eng.http.post = post
    try:
        phones(eng, server)
        eng.desk.apply(DEV, "prefs", {"forward": {"kind": "discord", "url": HOOK}})
        assert eng.desk.view(DEV, lambda s: None)["forward"]["kind"] == "discord"
        await eng.dispatcher.dispatch(news("Acme wins $2B Pentagon contract"))
        await eng._to_device(DEV, {"title": "🔔 ACME is above $105.00", "body": "$106.00"})
        await __import__("asyncio").sleep(0.05)
        assert [c.splitlines()[0] for _, c in sent] == [
            "🟠 Acme wins $2B Pentagon contract",
            "🔔 ACME is above $105.00",
        ]
        eng.desk.apply(DEV, "prefs", {"forward": None})
        await eng._to_device(DEV, {"title": "again"})
        await __import__("asyncio").sleep(0.05)
        assert len(sent) == 2
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- auto move alerts, the mute button


def test_auto_move_alert_when_watching():
    d = Desk(Storage(":memory:"))
    with pytest.raises(ValueError):
        d.apply(DEV, "prefs", {"auto_move": 4})
    d.apply(DEV, "prefs", {"auto_move": 5})
    d.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    d.apply(DEV, "watch_add", {"symbol": "ACME"})  # already watched: no second alert
    pa = d.get(DEV)["price_alerts"]
    assert [(a["symbol"], a["kind"], a["value"], a["auto"]) for a in pa] == [
        ("ACME", "move", 5.0, True),
        ("BETA", "move", 5.0, True),
    ]
    d.apply(DEV2, "watch_add", {"symbol": "ACME"})
    assert d.get(DEV2)["price_alerts"] == []  # off by default


def test_pushes_carry_the_stock_to_mute(tmp_path):
    eng = semis_engine(tmp_path)
    assert eng.webpush.payload(news())["mute"] == "ACME"
    from news247.models import Alert, Severity

    assert eng.webpush.payload(Alert(kind="system", severity=Severity.HIGH, title="x", body=""))["mute"] == ""


async def test_run7_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/earnings-week"), headers=h) as r:
                assert r.status == 200
            eng.storage.add_alert(graded_alert(time.time() - 7200, 0.2, 1.0, 1.0))
            async with s.get(srv.make_url("/api/record?days=7"), headers=h) as r:
                body = await r.json()
                assert body["priced_in"][0]["bucket"] == "fresh" and body["by_time"]
            async with s.post(
                srv.make_url("/api/desk"),
                json={"action": "prefs", "forward": {"kind": "discord", "url": "https://evil.example/x"}},
                headers=h,
            ) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
