"""Run 37: the news clock (how fast each kind of news plays out, what's left for a late entry, and
the live read under an hour in) and into the open (news from outside the session: the gap, and what
the stock did after the bell)."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.dossier import record_outcomes
from news247.newsclock import clock_board, clock_for, clock_line, clock_rates, early, early_line, stamp
from news247.opening import gap_bin, open_board, open_for, open_line, open_rates, outside, premarket, split
from news247.precheck import pre_trade
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def et(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=ET).timestamp()


NOW = et(14, 11, 0)  # a Wednesday, late morning


# --------------------------------------------------------------------------- the stamps


def test_the_clock_and_the_open_are_stamped(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract")
    a.created = et(14, 10, 0)
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    q = eng.live_quotes
    for t, px, now in ((et(14, 10, 4, 55), 101.0, et(14, 10, 5, 10)), (et(14, 10, 15, 30), 102.0, et(14, 10, 16)),
                       (et(14, 10, 30, 5), 101.5, et(14, 10, 31)), (et(14, 11, 0, 30), 103.0, et(14, 11, 1))):  # fmt: skip
        q.update("ACME", px, 100.0, t, "tape")
        record_outcomes(eng, now)
    row = eng.storage.get_alert(a.id)["edge"]["outcome"]["ACME"]
    assert (row["m5"], row["m15"], row["m30"], row["h1"]) == (1.0, 2.0, 1.5, 3.0)
    assert "open" not in row  # it landed in the session
    b = news("Beta wins contract", tickers=("BETA",))
    b.created = et(14, 7, 30)  # before the bell
    b.edge = {"refs": {"BETA": 50.0}}
    eng.storage.add_alert(b)
    q.update("BETA", 53.0, 50.0, et(14, 9, 30, 20), "tape")
    record_outcomes(eng, et(14, 9, 31))
    assert eng.storage.get_alert(b.id)["edge"]["outcome"]["BETA"]["open"] == 6.0
    c = news("Dlta wins contract", tickers=("DLTA",))
    c.created = et(13, 18, 0)  # the evening before: the next session's open
    c.edge = {"refs": {"DLTA": 10.0}}
    eng.storage.add_alert(c)
    q.update("DLTA", 10.2, 10.0, et(14, 9, 45), "tape")  # 15 minutes after the bell: too late for "the open"
    record_outcomes(eng, et(14, 9, 46))
    assert "open" not in (eng.storage.get_alert(c.id)["edge"].get("outcome") or {}).get("DLTA", {})


# --------------------------------------------------------------------------- the news clock


def clocked(eng, title, sym, sign, path, close, n=10, created=None):
    for i in range(n):
        a = news(f"{title} number {i}", tickers=(sym,), direction="up" if sign > 0 else "down")
        a.created = (created or NOW - 86400 * 7) - i * 600  # the Wednesday before, 9:30-11:00
        row = {k: v * sign for k, v in zip(("m5", "m15", "m30", "h1"), path, strict=True)}
        a.edge = {"refs": {sym: 100.0}, "outcome": {sym: {**row, "close": close * sign, "sign": sign}}}
        eng.storage.add_alert(a)


def test_each_kind_of_news_has_a_clock(tmp_path):
    eng = semis_engine(tmp_path)
    clocked(eng, "Acme wins contract", "ACME", 1, (1.0, 1.2, 1.3, 1.4), 2.0)  # fast: half done in 5 minutes
    clocked(eng, "Beta downgraded to sell at Citi", "BETA", -1, (0.1, 0.2, 0.3, 0.5), 2.0)  # slow
    history(eng)  # overnight contract wins: no checkpoints, read at the open instead, not on the clock
    rates = clock_rates(eng, now=NOW)
    fast, slow = rates["contract"], rates["downgrade"]
    assert fast["n"] == 10 and fast["close"] == 2.0
    assert fast["half"] == "m5" and fast["path"] == {"m5": 1.0, "m15": 1.2, "m30": 1.3, "h1": 1.4}
    assert fast["left"]["m15"] == {"n": 10, "hit": 1.0, "median": 0.8}
    assert fast["after"]["m15"] == {"ran": {"n": 10, "hit": 1.0, "median": 0.8}}
    assert slow["half"] == "late" and slow["left"]["m30"]["median"] == 1.7
    assert rates["*"]["n"] == 20
    assert (
        clock_line(stamp(fast))
        == "⏱ Fast: Contract win news made half its day's move within 5 minutes here; bought 15 minutes in, 100% went on its way (median +0.8% more, 10)"
    )
    assert (
        clock_line(stamp(slow))
        == "⏱ Slow: Analyst downgrade news built through the day here, no rush; 30 minutes in, +1.7% more to come (median, 10)"
    )
    assert (
        clock_line(None) == "" and clock_line({**stamp(fast), "half": "m30"}) == ""
    )  # neither fast nor slow
    assert [r["key"] for r in clock_board(rates)["rows"]][-1] == "downgrade"
    a = news("Acme wins another contract").to_dict()
    assert clock_for(rates, a, eng.universe)["key"] == "contract"
    assert clock_for(rates, news("Acme names new CFO").to_dict(), eng.universe)["key"] == "*"


def test_a_kind_that_doesnt_work_has_no_clock(tmp_path):
    eng = semis_engine(tmp_path)
    clocked(eng, "Acme wins contract", "ACME", 1, (0.5, 0.3, 0.1, -0.2), -0.5)
    rates = clock_rates(eng, now=NOW)
    assert rates["contract"]["half"] is None and clock_board(rates)["rows"] == []


def test_under_an_hour_in_the_clock_reads_the_alert_live(tmp_path):
    eng = semis_engine(tmp_path)
    clocked(eng, "Acme wins contract", "ACME", 1, (1.0, 1.2, 1.3, 1.4), 2.0)
    eng.clock_rates = clock_rates(eng, now=NOW)
    a = news("Acme wins new contract")
    a.created = NOW - 20 * 60
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    eng.live_quotes.update("ACME", 100.8, 100.0, NOW - 5, "tape")
    e = early(eng, a.to_dict(), NOW)
    assert (e["step"], e["since"], e["bin"], e["n"], e["median"]) == ("m15", 0.8, "ran", 10, 0.8)
    assert (
        early_line(e)
        == "15 minutes in, +0.8% its way: Contract win news that were already 0.5%+ its way by then went on its way 100% of the time (median +0.8% more by the close, 10)"
    )
    assert early(eng, a.to_dict(), NOW + 3600) is None  # past the hour: the live odds take over
    chk = next(c for c in pre_trade(eng, DEV, "ACME", "long", a.id, NOW)["checks"] if c["key"] == "early")
    assert chk["tone"] == "good" and chk["text"].startswith("15 minutes in")
    eng.live_quotes.update("ACME", 99.0, 100.0, NOW + 60, "tape")  # now 1% against it: no row with enough
    assert early(eng, a.to_dict(), NOW + 65) is None


async def test_the_push_says_fast_or_slow(tmp_path):
    eng = semis_engine(tmp_path)
    clocked(eng, "Acme wins contract", "ACME", 1, (1.0, 1.2, 1.3, 1.4), 2.0, created=None)
    eng.refresh_base_rates()
    a = news("Acme wins big contract")
    await eng.publish(a, push=False)
    assert a.edge["clock"]["half"] == "m5"
    assert (
        "⏱ Fast: Contract win news made half its day's move within 5 minutes"
        in eng.webpush.payload(a)["body"]
    )


# --------------------------------------------------------------------------- into the open


def overnight(eng, title, sym, gaps_closes, sign=1):
    for i, (gap, close) in enumerate(gaps_closes):
        a = news(f"{title} number {i}", tickers=(sym,), direction="up" if sign > 0 else "down")
        a.created = et(10, 7, 0) - i * 86400
        a.edge = {
            "refs": {sym: 100.0},
            "outcome": {sym: {"open": gap * sign, "close": close * sign, "sign": sign}},
        }
        eng.storage.add_alert(a)


def history(eng):
    overnight(eng, "Acme wins contract", "ACME", [(6.0, 4.0)] * 4 + [(7.0, -1.0)] * 2)  # big gaps faded
    overnight(eng, "Acme wins contract", "ACME", [(3.0, 5.0)] * 6)  # mid gaps kept going
    overnight(eng, "Beta wins contract", "BETA", [(5.5, 3.0)] * 3)  # BETA's big gaps: same kind, adds up


def test_the_gap_and_after_the_open(tmp_path):
    assert split({"open": 6.0, "close": 4.0, "sign": 1}) == (6.0, -1.89, 4.0)
    assert split({"open": -2.0, "close": -5.0, "sign": -1}) == (2.0, 3.06, 5.0)  # a short: its way
    assert split({"close": 1.0, "sign": 1}) is None
    assert [gap_bin(g) for g in (-1, 0.2, 1.5, 3, 7)] == ["against", "flat", "small", "mid", "big"]
    assert (
        outside(et(14, 8, 0))
        and outside(et(14, 16, 5))
        and outside(et(17, 11, 0))
        and not outside(et(14, 11, 0))
    )
    eng = semis_engine(tmp_path)
    history(eng)
    rates = open_rates(eng, now=NOW)
    big = rates["contract|big"]
    assert big["n"] == 9 and big["after"]["hit"] == 0.0 and big["filled"] == 0.22
    assert rates["contract|mid"]["after"] == {"n": 6, "hit": 1.0, "median": 1.94}
    assert open_for(rates, "contract", 6.4)["key"] == "contract|big"
    assert (
        open_for(rates, "fda_approval", 6.4)["key"] == "*|big"
    )  # no record for the kind: any news at that gap
    assert open_for(rates, "contract", 0.1)["key"] == "contract|*"  # no flat gaps graded: the kind at any gap
    assert (
        open_line(big)
        == "Contract win that gapped 5%+ its way: kept going after the open 0% (median -2.4% open to close, 9); 22% gave the whole gap back"
    )
    board = open_board(rates)
    assert [r["gap"] for r in board["any"]] == ["mid", "big"] and board["kinds"][0]["kind"] == "contract"


def morning(eng):
    history(eng)
    eng.open_rates = open_rates(eng, now=NOW)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    a = news("Acme wins $900M contract")
    a.created = et(14, 7, 15)
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    eng.live_quotes.update("ACME", 106.0, 100.0, et(14, 9, 18), "tape")
    return a


def test_this_mornings_gappers_carry_their_record(tmp_path):
    eng = semis_engine(tmp_path)
    a = morning(eng)
    pm = premarket(eng, et(14, 9, 19), DEV)
    assert pm["phase"] == "before"
    r = pm["rows"][0]
    assert (r["symbol"], r["gap"], r["bin"], r["mine"]) == ("ACME", 6.0, "big", True)
    assert r["record"]["key"] == "contract|big" and r["line"].startswith("Contract win that gapped 5%+")
    chk = next(
        c for c in pre_trade(eng, DEV, "ACME", "long", a.id, et(14, 9, 19))["checks"] if c["key"] == "open"
    )
    assert chk["tone"] == "warn" and chk["text"].startswith(
        "+6.0% its way since the news: contract win that gapped"
    )


async def test_the_open_push_says_how_gaps_like_it_went(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    morning(eng)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        assert await eng.send_open_plans(et(14, 9, 21)) >= 1
        msg = ph.read(server.requests[0]["body"])
        assert (
            "ACME +6.0% on its news · Contract win that gapped 5%+ its way: kept going after the open 0%"
            in msg["body"]
        )
    finally:
        await eng.http.close()


async def test_run37_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    clocked(eng, "Acme wins contract", "ACME", 1, (1.0, 1.2, 1.3, 1.4), 2.0)
    history(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/clock"), headers=h) as r:
                body = await r.json()
                assert "contract" in [r["key"] for r in body["rows"]] and "early" not in body
            async with s.get(srv.make_url("/api/clock?alert=nope"), headers=h) as r:
                assert (await r.json())["early"] is None
            async with s.get(srv.make_url("/api/open"), headers=h) as r:
                body = await r.json()
                assert body["kinds"][0]["kind"] == "contract" and "rows" in body["today"]
    finally:
        await srv.close()
        await eng.http.close()


def test_stamps_survive_a_round_trip(tmp_path):
    eng = semis_engine(tmp_path)
    clocked(eng, "Acme wins contract", "ACME", 1, (1.0, 1.2, 1.3, 1.4), 2.0)
    assert json.loads(json.dumps(clock_rates(eng, now=NOW)))["contract"]["half"] == "m5"
