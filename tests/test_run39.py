"""Run 39: the call (what an alert's records add up to, in one graded line) and coverage (how late an
alert came to its story and how far the story spread, graded from every story on file)."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.coverage import (
    coverage_board,
    coverage_rates,
    late_bin,
    late_line,
    read,
    stamp_coverage,
    wide_bin,
)
from news247.models import Analysis, NewsItem, Severity, SourceTier
from news247.thecall import call_rates, the_call
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

AN = Analysis(score=70, severity=Severity.HIGH, tickers=["ACME"], direction="up")


def story(eng, sid, t0, sources):
    """File a story: one item per (source, minutes after t0); returns the items by source."""
    items = {}
    for src, mins in sources:
        it = NewsItem(source=src, title=f"Acme story {sid} via {src}", url=f"https://x.test/{sid}/{src}",
                      detected=t0 + mins * 60, tier=SourceTier.WIRE)  # fmt: skip
        eng.storage.add_item(it, AN, story_id=sid)
        items[src] = it
    return items


def alert_from(item, created=None, **edge):
    a = news(item.title)
    a.item = item
    a.created = created or item.detected
    a.edge = dict(edge)
    return a


# --------------------------------------------------------------------------- coverage


def test_how_late_and_how_wide(tmp_path):
    eng = semis_engine(tmp_path)
    t0 = time.time() - 4 * 3600
    items = story(eng, 7, t0, [("reuters", 0), ("bloomberg", 5), ("cnbc", 20), ("yahoo", 50), ("x-feed", 70)])
    c = read(eng, alert_from(items["cnbc"]).to_dict())
    assert c == {"late": 20.0, "first_source": "reuters", "n30": 3, "n60": 4, "sources": 5}
    assert read(eng, alert_from(items["reuters"]).to_dict())["late"] == 0.0
    lone = NewsItem(source="prnewswire", title="Acme alone", url="https://x.test/lone", detected=t0)
    eng.storage.add_item(lone, AN)
    assert read(eng, alert_from(lone).to_dict()) == {
        "late": 0.0,
        "first_source": "prnewswire",
        "n30": 1,
        "n60": 1,
        "sources": 1,
    }
    assert [late_bin(m) for m in (0, 1.9, 2, 14.9, 15)] == ["first", "first", "early", "early", "late"]
    assert [wide_bin(n) for n in (1, 2, 3, 4)] == ["one", "few", "few", "many"]


def test_the_history_is_filled_in(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    items = story(eng, 8, now - 5 * 3600, [("reuters", 0), ("cnbc", 30)])
    old = alert_from(items["cnbc"])
    eng.storage.add_alert(old)
    fresh_item = story(eng, 9, now - 600, [("reuters", 0)])["reuters"]
    fresh = alert_from(fresh_item)
    eng.storage.add_alert(fresh)
    assert stamp_coverage(eng, now) == 1  # the fresh one waits until it's an hour old
    assert eng.storage.get_alert(old.id)["edge"]["coverage"]["late"] == 30.0
    assert "coverage" not in eng.storage.get_alert(fresh.id)["edge"]
    assert stamp_coverage(eng, now) == 0
    assert stamp_coverage(eng, now + 3600) == 1


def graded_cov(eng, late, n60, closes, sign=1):
    for i, close in enumerate(closes):
        it = NewsItem(
            source="reuters", title=f"Acme item {late} {n60} {i}", url=f"https://x.test/c/{late}/{n60}/{i}"
        )
        a = alert_from(it, created=time.time() - 86400 * 3 - i * 60,
                       coverage={"late": late, "first_source": "reuters", "n30": n60, "n60": n60, "sources": n60},
                       outcome={"ACME": {"h1": close * sign / 2, "close": close * sign, "sign": sign}})  # fmt: skip
        eng.storage.add_alert(a)


def test_late_alerts_and_wide_stories_are_graded(tmp_path):
    eng = semis_engine(tmp_path)
    graded_cov(eng, 0.5, 1, [1.0, 2.0, 1.5, -0.5, 1.0, 2.5, 0.5, 1.2])  # first, one outlet: worked
    graded_cov(eng, 25.0, 5, [-1.0, 0.5, -0.8, -1.2, 0.3, -0.5, -0.2, -0.9])  # late, wide: didn't
    rates = coverage_rates(eng)
    assert rates["late:first"]["close"] == {"n": 8, "hit": 0.88, "median": 1.1}
    assert rates["late:late"]["close"]["hit"] == 0.25 and rates["wide:many"]["label"] == "4+ outlets"
    assert (
        late_line(25.0, "reuters", rates)
        == "🕰 Late to the story: it first broke 25 minutes earlier (reuters): alerts that late went their way 25% by the close (median -0.7%, 8)"
    )
    assert late_line(5.0, "reuters", rates) == ""
    assert late_line(20.0, "", {}) == "🕰 Late to the story: it first broke 20 minutes earlier"
    board = coverage_board(rates)
    assert [r["bin"] for r in board["late"]] == ["first", "late"] and [r["bin"] for r in board["wide"]] == [
        "one",
        "many",
    ]


# --------------------------------------------------------------------------- the call

REC = lambda hit, n=20: {"close": {"n": n, "hit": hit, "median": 1.0}, "label": "Contract win"}  # noqa: E731


def test_the_call_adds_up_the_records():
    assert the_call({}) is None  # nothing graded: no call
    skip = the_call({"odds": {"p": 0.4}, "cat_base": REC(0.35)})
    assert (
        skip["verb"] == "skip"
        and skip["line"] == "✋ The call: skip · odds 40%, contract win went its way 35%"
    )
    act = the_call({"odds": {"p": 0.66, "ev": 0.8}, "clock": {"half": "m5"}})
    assert act["verb"] == "act" and act["line"] == "▶ The call: act now · fast news, odds 66%"
    two = the_call({"odds": {"p": 0.62}, "cat_base": REC(0.7)})
    assert two["verb"] == "act" and two["for"] == ["odds 62%", "contract win went its way 70%"]
    gap = the_call(
        {"odds": {"p": 0.62}, "open_base": {"after": {"n": 22, "hit": 0.36, "median": -1.2}}}, outside=True
    )
    assert (
        gap["verb"] == "wait"
        and gap["line"] == "⏸ The call: wait · for the open: news like it kept going after the bell 36%"
    )
    assert (
        the_call({"odds": {"p": 0.62}, "open_base": {"after": {"n": 22, "hit": 0.36}}})["verb"] == "watch"
    )  # in session
    fade = the_call(
        {
            "odds": {"p": 0.62},
            "persona": {"symbol": "ACME", "trait": "fades_day", "label": "Fades by the close"},
        }
    )
    assert fade["verb"] == "wait" and "ACME's first-hour moves usually fade" in fade["line"]
    slow = the_call({"odds": {"p": 0.62}, "clock": {"half": "late"}})
    assert (
        slow["verb"] == "watch"
        and slow["line"] == "👀 The call: watch · no rush: it builds through the day, odds 62%"
    )
    late = the_call({"odds": {"p": 0.62}, "late": {"min": 20, "rec": REC(0.3)}})
    assert late["verb"] == "watch" and late["against"] == [
        "late to the story (20 min)"
    ]  # one each way: mixed
    neg_ev = the_call({"odds": {"p": 0.5, "ev": -0.4}})
    assert neg_ev["verb"] == "skip" and neg_ev["against"] == ["expected -0.4%"]
    thin = the_call({"cat_base": REC(0.9, n=5), "clock": {"half": "m30"}})  # 5 graded: not counted
    assert thin["verb"] == "watch" and thin["for"] == [] and thin["against"] == []
    fights = the_call({"odds": {"p": 0.61}, "persona": {"symbol": "DLTA", "trait": "fights", "label": "Fights its news"},
                       "regime_base": {"n": 12, "hit": 0.33, "regime": "risk-off"}})  # fmt: skip
    assert fights["verb"] == "skip" and fights["against"] == ["on risk-off days 33%", "DLTA fights its news"]


async def test_a_new_alert_leads_with_the_call_and_its_lateness(tmp_path):
    eng = semis_engine(tmp_path)
    graded_cov(eng, 25.0, 5, [-1.0, 0.5, -0.8, -1.2, 0.3, -0.5, -0.2, -0.9])
    eng.refresh_base_rates()
    now = time.time()
    items = story(eng, 11, now - 25 * 60, [("reuters", 0), ("globenewswire", 25)])
    a = alert_from(items["globenewswire"], created=now)
    a.edge = {}
    await eng.publish(a, push=False)
    assert a.edge["late"]["min"] == 25.0 and a.edge["late"]["source"] == "reuters"
    body = eng.webpush.payload(a)["body"]
    assert (
        "🕰 Late to the story: it first broke 25 minutes earlier (reuters): alerts that late went their way 25%"
        in body
    )
    # everything on file counts: those graded alerts also make a weak headline record and a stock that fights its news
    assert a.edge["call"]["verb"] == "skip" and "late to the story (25 min)" in a.edge["call"]["against"]
    lines = body.split("\n")
    assert lines[1] == "✋ The call: skip · headlines · HIGH went its way 25%, ACME fights its news"
    assert lines[2].startswith(
        "🚫 The playbook passes"
    )  # the playbook's line sits right under the call (run 77)
    assert lines[3].startswith("🕰 Late to the story")


def test_the_call_is_graded_and_can_set_the_push_bar(tmp_path):
    eng = semis_engine(tmp_path)
    for verb, close, n in (("act", 1.5, 10), ("skip", -0.8, 10)):
        for i in range(n):
            a = news(f"Acme {verb} {i}")
            a.created = time.time() - 86400 * 2 - i * 60
            a.edge = {
                "call": {"verb": verb, "label": verb},
                "outcome": {"ACME": {"h1": close, "close": close, "d1": close, "sign": 1}},
            }
            eng.storage.add_alert(a)
    r = call_rates(eng)
    assert r["act"]["close"] == {"n": 10, "hit": 1.0, "median": 1.5} and r["skip"]["close"]["hit"] == 0.0
    assert r["act"]["icon"] == "▶" and "watch" not in r
    eng.desk.apply(DEV, "prefs", {"edge_min": "call"})
    held = news("Acme thing")
    held.edge = {"call": {"verb": "skip", "label": "Skip", "against": ["odds 40%"]}}
    why = eng.desk.block(DEV, held.to_dict(), time.time())
    assert why and why["code"] == "edge" and "the call is skip (odds 40%)" in why["text"]
    held.edge = {"call": {"verb": "act", "label": "Act now"}}
    assert eng.desk.block(DEV, held.to_dict(), time.time()) is None
    held.edge = {}
    assert eng.desk.block(DEV, held.to_dict(), time.time()) is None  # no call: not held back


async def test_run39_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    graded_cov(eng, 0.5, 1, [1.0, 2.0, 1.5, -0.5, 1.0, 2.5, 0.5, 1.2])
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/coverage"), headers=h) as r:
                assert (await r.json())["late"][0]["bin"] == "first"
            async with s.get(srv.make_url("/api/call"), headers=h) as r:
                assert (await r.json())["rows"] == []
    finally:
        await srv.close()
        await eng.http.close()
