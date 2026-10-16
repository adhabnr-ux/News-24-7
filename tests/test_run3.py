"""Run 3 of the desk: keyword watch, mutes, the trade journal, the gap board, sector drill-down,
the halt board and halt pushes, watchlist board pushes, "worth a look", earnings reactions and
the calendar feed."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.desk import Desk, _kw_pattern
from news247.dossier import gaps, halts_board, sector_detail, worth_a_look
from news247.http import Response
from news247.models import Analysis, NewsItem, Severity, SourceTier
from news247.personal import _fold, calendar_ics, earnings_reactions, parse_surprises, reactions
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_webpush import Phone

ET = ZoneInfo("America/New_York")


def item(title, source="globenewswire", tickers=("ACME",), summary="", **extra):
    it = NewsItem(source=source, title=title, url="https://x.test/" + str(abs(hash(title))), summary=summary,
                  tier=SourceTier.WIRE, tickers=list(tickers), extra=dict(extra))  # fmt: skip
    return it


def phones(eng, server):
    a, b = Phone(server.url("/push/a")), Phone(server.url("/push/b"))
    sa, sb = a.subscription(), b.subscription()
    sa.device, sb.device = DEV, DEV2
    eng.webpush.subs = {sa.endpoint: sa, sb.endpoint: sb}  # type: ignore[union-attr]
    return a, b


# --------------------------------------------------------------------------- keywords


def test_keyword_patterns_match_whole_words():
    fda = _kw_pattern("fda")
    assert fda.search("FDA approves Acme's drug") and fda.search("after the fda's ruling")
    assert not fda.search("FDAX futures slide") and not fda.search("the SFDA said")
    phrase = _kw_pattern("rare earth")
    assert phrase.search("China halts RARE   EARTH exports") and not phrase.search("rare earths")
    assert _kw_pattern("$nvda").search("buying $NVDA calls")


def test_keywords_are_validated_and_kept():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "kw_add", {"keyword": "  Tariff  "})
    d.apply(DEV, "kw_add", {"keyword": "tariff"})
    d.apply(DEV, "kw_add", {"keyword": "rare   earth"})
    assert [k["k"] for k in d.get(DEV)["keywords"]] == ["tariff", "rare earth"]
    with pytest.raises(ValueError, match="2 to 40"):
        d.apply(DEV, "kw_add", {"keyword": "x"})
    for i in range(18):
        d.apply(DEV, "kw_add", {"keyword": f"word{i}"})
    with pytest.raises(ValueError, match="at most 20"):
        d.apply(DEV, "kw_add", {"keyword": "one more"})
    d.apply(DEV, "kw_remove", {"keyword": "TARIFF"})
    assert "tariff" not in [k["k"] for k in d.get(DEV)["keywords"]]
    assert set(d.keyword_devices()) == {DEV}


async def test_keyword_hits_push_to_their_phone_once_per_story(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        eng.desk.apply(DEV, "kw_add", {"keyword": "tariff"})
        eng.desk.apply(DEV2, "kw_add", {"keyword": "buyback"})
        it = item("White House weighs new tariff on chips", summary="Officials said…")
        assert (
            await eng._keyword_push(
                it, Analysis(severity=Severity.MEDIUM, score=30, tickers=["ACME"]), story_id=7
            )
            == 1
        )
        assert [r["path"] for r in server.requests] == ["/push/a"]
        msg = a.read(server.requests[0]["body"])
        assert (
            msg["title"] == "🔑 tariff: White House weighs new tariff on chips"
            and msg["open"] == "/app/#q=tariff"
        )
        assert eng.desk.get(DEV)["kw_hits"][0]["k"] == "tariff"
        # the same story from another source: no second push
        assert (
            await eng._keyword_push(
                item("Tariff on chips weighed, sources say"),
                Analysis(severity=Severity.MEDIUM, score=30),
                story_id=7,
            )
            == 0
        )
        # too weak to matter, or loud enough to be an alert anyway: no keyword push
        assert (
            await eng._keyword_push(
                item("tariff chatter"), Analysis(severity=Severity.MEDIUM, score=5), story_id=8
            )
            == 0
        )
        assert (
            await eng._keyword_push(
                item("Tariff shock: 60% on all imports"),
                Analysis(severity=Severity.MEDIUM, score=95),
                story_id=9,
            )
            == 0
        )
        assert eng.desk.get(DEV)["kw_hits"][0]["title"].startswith("Tariff shock")  # still logged on the desk
        # a muted stock stays quiet
        eng.desk.apply(DEV, "mute", {"symbol": "ACME", "hours": 2})
        assert (
            await eng._keyword_push(
                item("New tariff hits Acme"),
                Analysis(severity=Severity.MEDIUM, score=30, tickers=["ACME"]),
                story_id=10,
            )
            == 0
        )
    finally:
        await eng.http.close()


async def test_new_items_reach_the_keyword_and_halt_hooks(tmp_path):
    eng = semis_engine(tmp_path)
    calls = []

    async def kw(it, an, sid):
        calls.append(("kw", it.title))

    async def halt(it, an):
        calls.append(("halt", it.title))

    eng._keyword_push, eng._halt_watch = kw, halt
    eng.desk.apply(DEV, "kw_add", {"keyword": "acme"})
    await eng.on_item(item("Acme names a new CFO"))
    await eng.on_item(
        item("TRADING HALT ACME (Acme) — News pending [T1]", source="nasdaq-halts", halt_code="T1")
    )
    await asyncio.sleep(0.01)
    assert ("kw", "Acme names a new CFO") in calls and (
        "halt",
        "TRADING HALT ACME (Acme) — News pending [T1]",
    ) in calls


# --------------------------------------------------------------------------- mutes and the journal


def test_mutes_expire_and_need_every_stock_muted():
    d = Desk(Storage(":memory:"))
    with pytest.raises(ValueError):
        d.apply(DEV, "mute", {"symbol": "ACME", "hours": 100})
    d.apply(DEV, "mute", {"symbol": "acme", "hours": 1})
    now = time.time()
    assert d.allows(DEV, news()) is False  # only about ACME
    assert d.allows(DEV, news(tickers=("ACME", "BETA"))) is True  # BETA isn't muted
    assert d.allows(DEV, news(tickers=())) is True
    assert d.allows(DEV, news(), now + 3700) is True  # an hour later it's back
    assert set(d.view(DEV, lambda s: None)["muted"]) == {"ACME"}
    d.apply(DEV, "unmute", {"symbol": "ACME"})
    assert d.allows(DEV, news()) is True


def test_journal_tags_and_record_by_tag():
    d = Desk(Storage(":memory:"))
    px = {"ACME": 100.0, "BETA": 50.0}
    pf = lambda s: (time.time(), px[s], 0.0)  # noqa: E731
    d.apply(
        DEV, "track_add", {"symbol": "ACME", "note": "Contract beat #Earnings #options thesis"}, price_of=pf
    )
    d.apply(DEV, "track_add", {"symbol": "BETA", "tags": ["momentum", "BAD TAG!", "#sympathy"]}, price_of=pf)
    t1, t2 = d.get(DEV)["tracked"]
    assert t1["tags"] == ["earnings", "options"] and t2["tags"] == ["momentum", "sympathy"]
    px.update(ACME=110.0, BETA=45.0)
    d.apply(DEV, "track_close", {"id": t1["id"], "lesson": "Sold too early"}, price_of=pf)
    d.apply(DEV, "track_close", {"id": t2["id"]}, price_of=pf)
    closed = d.get(DEV)["closed"]
    assert closed[1]["lesson"] == "Sold too early"
    d.apply(DEV, "track_journal", {"id": closed[0]["id"], "note": "chased it #momentum #fomo"})
    by = {r["tag"]: r for r in d.view(DEV, pf)["record"]["by_tag"]}
    assert by["earnings"] == {"tag": "earnings", "n": 1, "wins": 1, "avg": 10.0}
    assert by["momentum"]["avg"] == -10.0 and "fomo" in by and "sympathy" not in by
    with pytest.raises(ValueError, match="no such call"):
        d.apply(DEV, "track_journal", {"id": "nope", "note": "x"})


# --------------------------------------------------------------------------- boards


def test_gap_board_and_sector_detail(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=6.0, BETA=-4.0, DLTA=1.0, GAMA=12.0)
    it = item("Acme wins $2B Pentagon contract")
    eng._remember(it, Analysis(score=70, severity=Severity.HIGH, tickers=["ACME"]))
    g = gaps(eng, now)
    assert [r["symbol"] for r in g["up"]] == ["GAMA", "ACME"] and [r["symbol"] for r in g["down"]] == ["BETA"]
    acme = next(r for r in g["up"] if r["symbol"] == "ACME")
    assert acme["why"]["title"] == "Acme wins $2B Pentagon contract" and g["up"][0]["why"] is None
    assert gaps(eng, now, min_pct=10)["up"][0]["symbol"] == "GAMA"
    sd = sector_detail(eng, "Technology", now)
    assert sd["n"] == 3 and [s["symbol"] for s in sd["stocks"]] == ["ACME", "DLTA", "BETA"]
    assert sd["industries"][0]["industry"] == "Semiconductors" and sd["industries"][0]["up"] == 2
    assert sector_detail(eng, "Nope", now)["stocks"] == []


def test_halt_board_reads_the_feed_items(tmp_path):
    eng = semis_engine(tmp_path)
    it = item("TRADING HALT ACME (Acme Semiconductor) — News pending [T1]", source="nasdaq-halts",
              summary="Market: NASDAQ. Halted 10/14/2026 10:02:11 ET. Resumes 10:32:11 ET.")  # fmt: skip
    eng.storage.add_item(it, Analysis(severity=Severity.MEDIUM, score=40, tickers=["ACME"]))
    row = halts_board(eng)["rows"][0]
    assert (row["symbol"], row["code"], row["reason"], row["market"], row["resume"]) == (
        "ACME",
        "T1",
        "News pending",
        "NASDAQ",
        "10:32:11",
    )


async def test_halt_pushes_reach_watchers_when_the_halt_scored_low(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        it = item(
            "TRADING HALT ACME (Acme) — Volatility pause [LUDP]",
            source="nasdaq-halts",
            summary="Market: NASDAQ.",
        )
        assert await eng._halt_watch(it, Analysis(severity=Severity.MEDIUM, score=20, tickers=["ACME"])) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"].startswith("⏸ TRADING HALT ACME") and msg["open"] == "/app/#t=ACME"
        assert (
            await eng._halt_watch(it, Analysis(severity=Severity.MEDIUM, score=99, tickers=["ACME"])) == 0
        )  # an alert carries it
    finally:
        await eng.http.close()


async def test_board_pushes_for_watched_stocks(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
        now = time.time()
        eng.radar.board["ACME"] = {"symbol": "ACME", "change_pct": 9.0, "rvol": 4.2, "flagged": True}
        eng.radar.board["BETA"] = {"symbol": "BETA", "change_pct": 9.0, "flagged": True}
        b = Alert_radar("BETA", now)
        eng.storage.add_alert(b)  # BETA already had a radar alert today
        assert await eng.check_board_watch(now) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "★ ACME on the radar: +9.0% on 4× volume"
        assert await eng.check_board_watch(now + 60) == 0
        eng._board_seen = {"day": "", "keys": []}  # a restart: the day's list is saved
        assert await eng.check_board_watch(now + 120) == 0
    finally:
        await eng.http.close()


def Alert_radar(sym, now):  # noqa: N802 - a fixture builder
    from news247.models import Alert

    return Alert(
        kind="price",
        severity=Severity.HIGH,
        title=f"{sym} +9%",
        body="",
        tickers=[sym],
        created=now - 60,
        edge={"radar": {"symbol": sym}},
    )


def test_worth_a_look_finds_moving_peers(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=1.0, BETA=5.0, DLTA=0.5)
    assert worth_a_look(eng, DEV, now) == []
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    eng.radar.board["DLTA"] = {"symbol": "DLTA", "flagged": True}
    rows = worth_a_look(eng, DEV, now)
    assert [(r["symbol"], r["why"]) for r in rows] == [("DLTA", ["on the radar"]), ("BETA", ["+5.0% today"])]
    assert rows[0]["peer_of"] == "ACME"


# --------------------------------------------------------------------------- earnings reactions


SURPRISES = {"data": {"symbol": "ACME", "earningsSurpriseTable": {"rows": [
    {"fiscalQtrEnd": "Jun 2026", "dateReported": "7/30/2026", "eps": 1.57, "consensusForecast": "1.43", "percentageSurprise": "9.79"},
    {"fiscalQtrEnd": "Mar 2026", "dateReported": "4/29/2026", "eps": "0.90", "consensusForecast": "1.00", "percentageSurprise": "-10"},
    {"fiscalQtrEnd": "Dec 2025", "dateReported": "N/A", "eps": 1, "consensusForecast": 1, "percentageSurprise": 0},
]}}}  # fmt: skip


def bars(days: dict[str, float]) -> list[tuple[float, float]]:
    return [
        (datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=ET).timestamp(), p)
        for d, p in days.items()
    ]


def test_surprises_and_two_day_reactions():
    rows = parse_surprises(SURPRISES)
    assert [r["date"] for r in rows] == ["2026-07-30", "2026-04-29"] and rows[1]["surprise_pct"] == -10.0
    b = bars(
        {
            "2026-04-28": 100,
            "2026-04-29": 98,
            "2026-04-30": 92,
            "2026-07-29": 50,
            "2026-07-30": 51,
            "2026-07-31": 55,
        }
    )
    out = reactions(rows, b)
    assert [r["move_pct"] for r in out] == [10.0, -8.0]  # close before the report day -> close after it
    assert reactions(rows, [])[0]["move_pct"] is None


async def test_earnings_reactions_reads_both_feeds_once(tmp_path):
    eng = semis_engine(tmp_path)
    seen = []
    chart = {"chart": {"result": [{"meta": {}, "timestamp": [t for t, _ in bars({"2026-07-29": 50, "2026-07-31": 55})],
                                   "indicators": {"quote": [{"close": [50, 55]}]}}]}}  # fmt: skip

    async def get(url, **kw):
        seen.append(url)
        body = SURPRISES if "nasdaq" in url else chart
        return Response(200, json.dumps(body).encode(), {}, url, 0.1)

    eng.http.get = get
    eng.implied_cache["ACME"] = (
        time.time(),
        {"rows": [{"expiry": "2026-10-23", "move_pct": 7.5, "earnings": True}]},
    )
    out = await earnings_reactions(eng, "ACME")
    assert out["available"] and out["beat"] == 1 and out["reported"] == 2
    assert out["rows"][0]["move_pct"] == 10.0 and out["avg_abs_move"] == 10.0
    assert out["implied"] == {"expiry": "2026-10-23", "move_pct": 7.5, "ratio": 0.75}
    assert (await earnings_reactions(eng, "ACME")) is out and len(seen) == 2  # cached

    async def down(url, **kw):
        raise aiohttp.ClientError("blocked")

    eng.http.get = down
    bad = await earnings_reactions(eng, "BETA")
    assert bad["available"] is False and "ClientError" in bad["why"]


# --------------------------------------------------------------------------- the calendar feed


def test_calendar_feed_is_valid_icalendar(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    nxt = (date.today() + timedelta(days=5)).isoformat()
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(
            lookup=lambda s, t=None: (
                {"date": nxt, "when": "after the close", "in_days": 5} if s == "ACME" else None
            )
        )
    )
    ics = calendar_ics(eng, DEV)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") >= 2
    assert f"DTSTART;VALUE=DATE:{nxt.replace('-', '')}" in ics and "SUMMARY:ACME reports earnings" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert "SUMMARY:ACME reports earnings" not in calendar_ics(eng, "")  # no phone, no watchlist
    long = "x" * 200 + "é" * 50
    assert _fold(long).replace("\r\n ", "") == long and all(
        len(p.encode()) <= 75 for p in _fold(long).split("\r\n")
    )


# --------------------------------------------------------------------------- the API


async def test_run3_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}

    async def down(url, **kw):
        raise aiohttp.ClientError("blocked")

    try:
        async with aiohttp.ClientSession() as s:
            quotes(eng, time.time(), ACME=6.0, BETA=-4.0, DLTA=0.5)
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "kw_add", "keyword": "tariff"}, headers=h
            ) as r:
                assert (await r.json())["keywords"][0]["k"] == "tariff"
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "mute", "symbol": "ACME", "hours": 4}, headers=h
            ) as r:
                assert "ACME" in (await r.json())["muted"]
            async with s.get(srv.make_url("/api/gaps"), headers=h) as r:
                assert [x["symbol"] for x in (await r.json())["up"]] == ["ACME"]
            async with s.get(srv.make_url("/api/sector?name=Technology"), headers=h) as r:
                assert (await r.json())["n"] == 3
            async with s.get(srv.make_url("/api/halts"), headers=h) as r:
                assert (await r.json())["rows"] == []
            async with s.get(srv.make_url("/api/worth"), headers=h) as r:
                assert (await r.json())["rows"] == []
            eng.http.get = down
            async with s.get(srv.make_url("/api/earnings/ACME"), headers=h) as r:
                assert (await r.json())["available"] is False
            async with s.get(srv.make_url(f"/api/calendar.ics?token=tok&device={DEV}")) as r:
                assert (
                    r.status == 200
                    and r.content_type == "text/calendar"
                    and (await r.text()).startswith("BEGIN:VCALENDAR")
                )
            async with s.get(srv.make_url("/api/calendar.ics")) as r:
                assert r.status == 401
    finally:
        await srv.close()
        await eng.http.close()
