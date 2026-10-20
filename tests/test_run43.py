"""Run 43: the alert line, tested (headlines that scored just under it, graded in the shadow, beside
the alerts just over it) and macro days (each kind of news on Fed and jobs-report days vs others)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.analysis.dedup import Story
from news247.macrodays import macro_board, macro_days, macro_for, macro_line, macro_rates, today
from news247.models import Analysis, NewsItem, Severity
from news247.precheck import pre_trade
from news247.shadow import grade, line_report, record
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

ET = ZoneInfo("America/New_York")


def et(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=ET).timestamp()


def item(title, source="seekingalpha", url=None):
    return NewsItem(source=source, title=title, url=url or f"https://x.test/{abs(hash(title))}")


def an(score, sym="ACME", direction="up"):
    return Analysis(score=score, severity=Severity.LOW, tickers=[sym], direction=direction)


# --------------------------------------------------------------------------- the line, tested


def test_near_misses_are_kept(tmp_path):
    eng = semis_engine(tmp_path)
    now = et(10, 14, 11)
    eng.live_quotes.update("ACME", 100.0, 100.0, now - 5, "tape")
    line = eng.cfg.scoring.medium
    assert record(eng, item("Acme near miss"), an(line - 3), line - 3, now)
    assert not record(eng, item("Acme near miss"), an(line - 3), line - 3, now)  # once per headline
    assert not record(eng, item("Acme far miss"), an(line - 20), line - 20, now)  # too far under
    assert not record(eng, item("Acme over"), an(line + 1), line + 1, now)  # an alert, not a miss
    assert not record(eng, item("Acme flat"), an(line - 3, direction="mixed"), line - 3, now)
    assert not record(eng, item("Beta no price"), an(line - 3, "BETA"), line - 3, now)
    kept = eng.storage.shadows(now - 60)
    assert (
        len(kept) == 1 and kept[0]["ref"] == 100.0 and kept[0]["score"] == line - 3 and kept[0]["sign"] == 1
    )


async def test_the_finalize_path_keeps_a_near_miss(tmp_path):
    eng = semis_engine(tmp_path)
    eng.live_quotes.update("ACME", 100.0, 100.0, time.time() - 5, "tape")
    line = eng.cfg.scoring.medium
    it, a = item("Acme in talks, people say"), an(line - 4)
    story = Story(
        id=1,
        title=it.title,
        tokens=frozenset(),
        entities=frozenset(),
        created=time.time(),
        updated=time.time(),
        max_score=0.0,
    )
    await eng._finalize(it, a, story)
    assert [s["title"] for s in eng.storage.shadows(time.time() - 60)] == ["Acme in talks, people say"]


def test_each_near_miss_is_graded(tmp_path):
    eng = semis_engine(tmp_path)
    t0 = et(10, 14, 11)
    eng.live_quotes.update("ACME", 100.0, 100.0, t0 - 5, "tape")
    record(eng, item("Acme near miss"), an(eng.cfg.scoring.medium - 2), eng.cfg.scoring.medium - 2, t0)
    eng.live_quotes.update("ACME", 101.0, 100.0, t0 + 3600 + 30, "tape")
    assert grade(eng, t0 + 3600 + 60) == 1
    eng.live_quotes.update("ACME", 102.5, 100.0, et(10, 14, 15, 59), "tape")
    assert grade(eng, et(10, 14, 16, 1)) == 1
    s = eng.storage.shadows(t0 - 60)[0]
    assert (s["h1"], s["close"]) == (1.0, 2.5)
    assert grade(eng, et(10, 14, 16, 5)) == 0


def shadowed(eng, n, under, close, source="seekingalpha"):
    line = eng.cfg.scoring.medium
    for i in range(n):
        eng.storage.add_shadow(f"s-{under}-{close}-{i}-{source}", time.time() - 86400 * 3 - i * 600,
                               {"symbol": "ACME", "sign": 1, "ref": 100.0, "score": line - under, "line": line,
                                "source": source, "title": "x", "close": close})  # fmt: skip


def over(eng, n, close):
    line = eng.cfg.scoring.medium
    for i in range(n):
        a = news(f"Acme just over {i} {close}")
        a.analysis.score = line + 2
        a.created = time.time() - 86400 * 3 - i * 600
        a.edge = {"outcome": {"ACME": {"close": close, "sign": 1}}}
        eng.storage.add_alert(a)


def test_the_line_report(tmp_path):
    eng = semis_engine(tmp_path)
    line = eng.cfg.scoring.medium
    shadowed(eng, 12, 2, 1.0)
    shadowed(eng, 8, 2, -0.5, source="prnewswire")  # just under: 12 of 20 went their way
    shadowed(eng, 10, 12, -1.0)  # far under: none did
    over(eng, 20, 0.8)
    over(eng, 10, -0.4)  # just over: 20 of 30
    r = line_report(eng)
    assert r["line"] == line and r["graded"] == 30 and r["kept"] == 30
    b0, b1, b2 = r["bands"]
    assert (b0["n"], b0["hit"], b0["from"], b0["to"]) == (20, 0.6, line - 5, line)
    assert b1["n"] == 0 and b2["n"] == 10 and b2["hit"] == 0.0
    assert r["just_over"]["n"] == 30 and r["just_over"]["hit"] == 0.67
    assert r["verdict"] == "keep"  # 60% vs 67%: just under didn't keep up
    assert r["line_text"].startswith(
        f"The line holds: headlines just under it ({line - 5:g}-{line:g}) went their way 60%"
    )
    assert [x["source"] for x in r["sources"]] == ["seekingalpha", "prnewswire"]
    shadowed(eng, 10, 1, 1.5)  # now 22 of 30 just under: 73% vs 67%
    r = line_report(eng)
    assert r["verdict"] == "lower" and f"Lowering the line to {line - 5:g} would add about" in r["line_text"]


# --------------------------------------------------------------------------- macro days


def test_macro_days_come_from_the_calendar(tmp_path):
    eng = semis_engine(tmp_path)
    days = macro_days(eng)
    assert days["2026-10-28"] == ["FOMC decision"] and "2026-10-02" in days
    assert "2026-11-10" not in days  # a trade event at impact 2 isn't a whole-market day
    assert today(eng, et(10, 28, 10)) == ["FOMC decision"] and today(eng, et(10, 27, 10)) == []


def macro_history(eng):
    for i in range(10):  # Oct 2 was a jobs day: contract wins faded
        a = news(f"Acme wins contract macro {i}")
        a.created = et(10, 2, 10) + i * 600
        a.edge = {"outcome": {"ACME": {"close": -1.0 if i < 7 else 1.0, "sign": 1}}}
        eng.storage.add_alert(a)
    for i in range(12):  # other days: worked
        a = news(f"Acme wins contract other {i}")
        a.created = et(10, 6, 10) + i * 600
        a.edge = {"outcome": {"ACME": {"close": 1.0 if i < 9 else -1.0, "sign": 1}}}
        eng.storage.add_alert(a)


def test_each_kind_on_macro_days(tmp_path):
    eng = semis_engine(tmp_path)
    macro_history(eng)
    rates = macro_rates(eng, now=et(10, 20, 12))
    c = rates["contract"]
    assert c["macro"] == {"n": 10, "hit": 0.3, "median": -1.0} and c["other"]["hit"] == 0.75
    m = macro_for(rates, "contract", ["FOMC decision"])
    assert (
        macro_line(m)
        == "📅 FOMC decision today: contract win news on macro days went its way 30% (10) vs 75% on other days (12)"
    )
    assert macro_for(rates, "contract", []) is None
    board = macro_board(eng, rates, et(10, 20, 12))
    assert board["next"]["date"] == "2026-10-28" and board["rows"][0]["key"] in ("*", "contract")


async def test_on_a_macro_day_a_new_alert_carries_it(tmp_path):
    eng = semis_engine(tmp_path)
    macro_history(eng)
    eng.macro_rates = macro_rates(eng, now=et(10, 20, 12))
    a = news("Acme wins $900M Navy contract")
    a.created = et(10, 28, 10)
    await eng.publish(a, push=False)
    assert a.edge["macro"]["today"] == ["FOMC decision"]
    assert "on macro days 30%" in a.edge["call"]["against"]
    assert (
        "📅 FOMC decision today: contract win news on macro days went its way 30%"
        in eng.webpush.payload(a)["body"]
    )
    chk = next(c for c in pre_trade(eng, DEV, "ACME", "long", a.id)["checks"] if c["key"] == "macro")
    assert chk["tone"] == "warn"


async def test_run43_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    shadowed(eng, 3, 2, 1.0)
    macro_history(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/line"), headers=h) as r:
                body = await r.json()
                assert body["kept"] == 3 and "verdict" not in body
            async with s.get(srv.make_url("/api/macro"), headers=h) as r:
                assert "rows" in await r.json()
    finally:
        await srv.close()
        await eng.http.close()
