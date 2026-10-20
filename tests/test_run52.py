"""Run 52: the mainstream catch-up graded (what the head start captured, what's left after it, a push
when it happens on your stock) and halts graded (what a stock did after trading resumed, by halt code
and direction, carried by the resumption push)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247 import haltrec
from news247.analysis.dedup import Story
from news247.catchup import bucket, catchup_for, catchup_line, catchup_rates, outlet, split
from news247.models import NewsItem
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def et(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- the mainstream catch-up


def caught(eng, n, px=102.0, close=3.0, lead_s=1800, created=None):
    out = []
    for i in range(n):
        a = news(f"Acme wins Pentagon contract {px} {close} {i}")
        a.created = created or et(14, 10) - 86400 * i
        a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": close}},
                  "lead": {"source": "cnbc-markets", "lead_s": lead_s, "at": a.created + lead_s, "px": px}}  # fmt: skip
        eng.storage.add_alert(a)
        out.append(a)
    return out


def test_the_day_splits_at_the_catch_up(tmp_path):
    assert [bucket(s) for s in (60, 900, 7200, 20000)] == ["fast", "hour", "hours", "late"]
    assert (
        outlet("cnbc-markets") == "CNBC"
        and outlet("reuters-biz") == "Reuters"
        and outlet("wsj-markets") == "the WSJ"
    )
    eng = semis_engine(tmp_path)
    (a,) = caught(eng, 1)
    assert split(eng.storage.get_alert(a.id)) == (1800, 2.0, 0.98, 3.0)
    (late,) = caught(eng, 1, lead_s=8 * 3600)  # carried at 18:00: after the close
    assert split(eng.storage.get_alert(late.id)) is None


def test_the_catch_up_record(tmp_path):
    eng = semis_engine(tmp_path)
    caught(eng, 8)
    r = catchup_rates(eng, 400, et(20, 12))
    h = r["contract|hour"]
    assert (h["n"], h["after"]["hit"], h["after"]["median"], h["before_median"], h["captured"]) == (
        8,
        1.0,
        0.98,
        2.0,
        0.67,
    )
    assert h["label"] == "Contract win the mainstream caught up on in 15-60 minutes"
    assert catchup_for(r, "contract", 1200) is h
    assert catchup_for(r, "fda_approval", 1200) is r["*|hour"]
    assert catchup_line(h) == ("Contract win the mainstream caught up on in 15-60 minutes: 67% of the winners' day came "
                               "before it; after it, 100% went further by the close (median +1.0%, 8)")  # fmt: skip


async def test_the_catch_up_is_stamped_and_pushed(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    caught(eng, 8)
    eng.refresh_base_rates()
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        now = time.time()
        a = news("Acme wins $2B Pentagon contract")
        a.created = now - 1200
        a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1}}}
        eng.storage.add_alert(a)
        eng.live_quotes.update("ACME", 102.5, 99.0, now - 5, "tape")
        story = Story(id=7, title=a.title, tokens=frozenset(), entities=frozenset(), created=a.created,
                      updated=now, alert_id=a.id, alert_created=a.created)  # fmt: skip
        it = NewsItem(source="cnbc-markets", title="Acme lands Pentagon deal", url="https://cnbc.test/1")
        it.detected = now
        eng._record_lead(story, it)
        await asyncio.sleep(0.2)
        ld = eng.storage.get_alert(a.id)["edge"]["lead"]
        assert ld["px"] == 102.5 and ld["its"] == 2.5 and round(ld["lead_s"]) == 1200
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "📰 CNBC has ACME now, 20 min after your alert"
        assert (
            "Since the alert: +2.5% its way. Contract win the mainstream caught up on in 15-60 minutes:"
            in msg["body"]
        )
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- halts, graded


def halt_item(sym="ACME", code="LUDP", ts=None, uid="h1"):
    return SimpleNamespace(tickers=[sym], uid=uid, published=ts, extra={"halt_code": code})


def resume_item(sym="ACME", at=None):
    return SimpleNamespace(
        tickers=[sym], uid="r1", published=at, extra={"halt_resume": "LUDP", "resume_at": at}
    )


def test_a_halt_is_graded_from_its_resumption(tmp_path):
    eng = semis_engine(tmp_path)
    t0 = et(14, 10)
    eng.live_quotes.update("ACME", 110.0, 100.0, t0 - 5, "tape")  # +10% into the halt
    r = haltrec.on_halt(eng, halt_item(ts=t0), t0)
    assert (r["px"], r["into"], r["sign"]) == (110.0, 10.0, 1)
    assert haltrec.on_halt(eng, halt_item(ts=t0), t0) is None  # once
    eng.live_quotes.update("BETA", 50.0, 50.1, t0 - 5, "tape")
    assert haltrec.on_halt(eng, halt_item("BETA", ts=t0, uid="h2"), t0) is None  # no direction into it
    assert haltrec.on_resume(eng, resume_item(at=t0 + 300), t0 + 60)["resume"] == t0 + 300
    eng.live_quotes.update("ACME", 115.5, 100.0, t0 + 600, "tape")
    assert haltrec.grade(eng, t0 + 600 + 30) == 1
    eng.live_quotes.update("ACME", 104.5, 100.0, t0 + 2100, "tape")
    assert haltrec.grade(eng, t0 + 2100 + 30) == 1
    eng.live_quotes.update("ACME", 99.0, 100.0, et(14, 15, 59), "tape")
    assert haltrec.grade(eng, et(14, 16, 5)) == 1
    row = haltrec._log(eng)[0]
    assert (row["m5"], row["m30"], row["close"]) == (5.0, -5.0, -10.0)
    assert haltrec.grade(eng, et(14, 16, 10)) == 0  # done


def logged(eng, n, code, sign, m5, close, sym="ACME"):
    rows = haltrec._log(eng)
    for i in range(n):
        rows.append({"id": f"{code}{sign}{i}", "sym": sym, "code": code, "ts": time.time() - 3600 * (i + 2),
                     "px": 10.0, "into": 8.0 * sign, "sign": sign, "resume": time.time() - 3600 * (i + 2) + 300,
                     "m5": m5 if i < n - 2 else -m5, "m30": 1.0, "close": close})  # fmt: skip
    eng.storage.set_setting(haltrec.KEY, __import__("json").dumps(rows))


def test_the_halt_record(tmp_path):
    eng = semis_engine(tmp_path)
    logged(eng, 10, "LUDP", 1, 2.0, -3.0)
    logged(eng, 4, "T1", -1, 1.0, 1.0, sym="BETA")
    r = haltrec.halt_rates(eng)
    up = r["LUDP|up"]
    assert up["label"] == "Volatility Trading Pause (LULD) on the way up"
    assert (up["n"], up["m5"]["hit"], up["m30"]["hit"], up["close"]["hit"]) == (10, 0.8, 1.0, 0.0)
    assert "T1|down" not in r  # too few
    assert haltrec.halt_for(r, "T5", 1) is r["*|up"]
    assert haltrec.halt_line(up) == ("Volatility Trading Pause (LULD) on the way up kept going: 5 minutes after the resumption 80%, "
                                     "30 minutes after 100%, by the close 0% (10 graded)")  # fmt: skip
    eng.halt_rates = r
    assert haltrec.resume_line(eng, "ACME").startswith(
        "Volatility Trading Pause (LULD) on the way up kept going"
    )


async def test_the_resumption_push_carries_the_record(tmp_path, server):
    from news247.models import Analysis, Severity

    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    logged(eng, 10, "LUDP", 1, 2.0, -3.0)
    eng.halt_rates = haltrec.halt_rates(eng)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        it = NewsItem(source="nasdaq-halts", title="▶ ACME resumes trading at 10:05 ET", url="https://x.test/h",
                      summary="Halted 10:00 ET for Volatility Trading Pause (LULD) [LUDP].", tickers=["ACME"])  # fmt: skip
        it.extra.update({"halt_resume": "LUDP", "resume_at": time.time()})
        assert await eng._halt_watch(it, Analysis(score=10, severity=Severity.LOW, tickers=["ACME"])) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["body"].endswith("Volatility Trading Pause (LULD) on the way up kept going: 5 minutes after the resumption "
                                    "80%, 30 minutes after 100%, by the close 0% (10 graded).")  # fmt: skip
    finally:
        await eng.http.close()


async def test_run52_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    caught(eng, 8)
    logged(eng, 10, "LUDP", 1, 2.0, -3.0)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/catchup"), headers=h) as r:
                assert (await r.json())["all"]["n"] == 8
            async with s.get(srv.make_url("/api/halts/record"), headers=h) as r:
                assert (await r.json())["rows"][0]["key"] == "*|up"
    finally:
        await srv.close()
        await eng.http.close()
