"""Run 50: the alert line per kind of news (each kind's near misses and just-over alerts graded, its
line moved on that evidence), and float and short interest graded (how news runs on a stock with few
shares to trade, or many sold short)."""

from __future__ import annotations

import json
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247 import floatshort
from news247.floatshort import fband, float_for, float_line, float_rates, parse, sband, stamp_of
from news247.linekinds import kind_of, kind_report, line_for, offsets, set_kind_line
from news247.thecall import the_call
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

# --------------------------------------------------------------------------- the line per kind


def shadow(eng, title, score, close, sym="ACME", line=None, i=0):
    line = eng.cfg.scoring.medium if line is None else line
    eng.storage.add_shadow(f"s-{title}-{i}", time.time() - 86400 * (2 + i % 20),
                           {"symbol": sym, "sign": 1, "ref": 100.0, "score": score, "line": line,
                            "source": "globenewswire", "title": title, "close": close})  # fmt: skip


def over(eng, title, score, close, i=0):
    a = news(f"{title} {i}")
    a.created = time.time() - 86400 * (2 + i % 20)
    a.analysis.score = score
    a.edge = {"outcome": {"ACME": {"sign": 1, "close": close}}}
    eng.storage.add_alert(a)


def test_each_kind_of_news_has_its_own_line(tmp_path):
    eng = semis_engine(tmp_path)
    line = eng.cfg.scoring.medium
    for i in range(16):  # contract wins just under the line ran like the ones just over it
        shadow(eng, f"Acme wins Pentagon contract {i}", line - 2, 2.0 if i < 11 else -1.0, i=i)
        over(eng, "Acme wins Pentagon contract", line + 1, 2.0 if i < 10 else -1.0, i=i)
    for i in range(16):  # analyst downgrades just over the line lost
        over(eng, "Analyst downgrades Acme to sell", line + 2, -1.5 if i < 11 else 0.5, i=i)
    rows = {r["kind"]: r for r in kind_report(eng, 60)}
    c = rows["contract"]
    assert c["verdict"] == "lower" and c["to"] == line - 5
    assert (c["under"]["n"], c["under"]["hit"], c["over"]["hit"]) == (16, 0.69, 0.62)
    assert c["text"].startswith(
        f"Contract win headlines just under their line ({line - 5:g}-{line:g}) went their way 69%"
    )
    d = rows["downgrade"]
    assert d["verdict"] == "raise" and d["to"] == line + 5 and d["over"]["hit"] == 0.31
    assert [r["verdict"] for r in kind_report(eng, 60)][:2] == ["lower", "raise"]


def test_moving_a_kinds_line(tmp_path):
    eng = semis_engine(tmp_path)
    line = eng.cfg.scoring.medium
    assert kind_of(eng, "Acme wins $2B Pentagon contract", ["ACME"]) == "contract"
    out = set_kind_line(eng, "contract", -5, "near misses ran", now=1000.0)
    assert out["line"] == line - 5 and offsets(eng) == {"contract": -5.0}
    assert line_for(eng, "contract") == line - 5 and line_for(eng, "downgrade") == line
    log = json.loads(eng.storage.get_setting("line_log"))
    assert log[-1] == {"at": 1000.0, "kind": "contract", "label": "Contract win", "from": line, "to": line - 5,
                       "why": "near misses ran"}  # fmt: skip
    with pytest.raises(ValueError):
        set_kind_line(eng, "contract", -15)  # at most 10 from the main line
    with pytest.raises(ValueError):
        set_kind_line(eng, "nonsense", -5)
    with pytest.raises(ValueError):
        set_kind_line(eng, "contract", eng.cfg.scoring.high - line)  # never up to HIGH
    set_kind_line(eng, "contract", None)
    assert offsets(eng) == {} and eng.line_offsets == {}


async def test_headlines_are_read_against_their_kinds_line(tmp_path):
    from news247.analysis.dedup import Story
    from news247.models import Analysis, NewsItem, Severity

    eng = semis_engine(tmp_path)
    eng.live_quotes.update("ACME", 100.0, 100.0, time.time() - 5, "tape")
    line = eng.cfg.scoring.medium
    set_kind_line(eng, "contract", -5)
    set_kind_line(eng, "downgrade", 5)

    async def run(title, score):
        it = NewsItem(source="globenewswire", title=title, url=f"https://x.test/{abs(hash(title))}")
        a = Analysis(score=score, severity=Severity.LOW, tickers=["ACME"], direction="up")
        story = Story(id=abs(hash(title)) % 10000, title=title, tokens=frozenset(), entities=frozenset(),
                      created=time.time(), updated=time.time(), max_score=0.0)  # fmt: skip
        await eng._finalize(it, a, story)

    await run("Acme wins $2B Pentagon contract", line - 3)  # under the main line, over contract's
    await run("Analyst downgrades Acme to sell", line + 3)  # over the main line, under downgrade's
    await run("Acme in talks, people say", line - 3)  # no line of its own: the main one
    alerts = [a["title"] for a in eng.storage.recent_alerts(10)]
    assert alerts == ["Acme wins $2B Pentagon contract"]
    sh = {s["title"]: s for s in eng.storage.shadows(time.time() - 60)}
    assert (
        sh["Analyst downgrades Acme to sell"]["line"] == line + 5
        and sh["Analyst downgrades Acme to sell"]["kind"] == "downgrade"
    )
    assert sh["Acme in talks, people say"]["line"] == line


# --------------------------------------------------------------------------- float and short interest


def test_reading_float_and_short():
    doc = {"quoteSummary": {"result": [{"defaultKeyStatistics": {"floatShares": {"raw": 8_200_000, "fmt": "8.2M"},
                                                                 "shortPercentOfFloat": {"raw": 0.2213},
                                                                 "sharesOutstanding": {"raw": 12_000_000}}}]}}  # fmt: skip
    assert parse(doc) == {"float": 8_200_000, "short": 0.2213, "out": 12_000_000}
    by_shares = {
        "quoteSummary": {
            "result": [{"defaultKeyStatistics": {"floatShares": {"raw": 40e6}, "sharesShort": {"raw": 2e6}}}]
        }
    }
    assert parse(by_shares)["short"] == 0.05
    assert parse({}) is None and parse({"quoteSummary": {"result": [{"defaultKeyStatistics": {}}]}}) is None
    assert [fband(x) for x in (5e6, 10e6, 120e6, 2e9, None)] == ["tiny", "small", "mid", "large", None]
    assert [sband(x) for x in (0.01, 0.05, 0.2, 0.45, None)] == ["low", "some", "high", "crowded", None]
    assert stamp_of({"float": 8_200_000, "short": 0.22}) == {
        "float": 8_200_000,
        "short": 0.22,
        "fband": "tiny",
        "sband": "high",
    }


async def test_statistics_are_kept_a_week(tmp_path):
    eng = semis_engine(tmp_path)
    calls = []

    class Feed:
        async def yahoo_json(self, url, params):
            calls.append((url, params))
            return {"quoteSummary": {"result": [{"defaultKeyStatistics": {"floatShares": {"raw": 30e6}}}]}}

    eng.smallcap_feed = Feed()
    d = await floatshort.fetch(eng, "ACME", now=1000.0)
    assert d["float"] == 30_000_000 and calls[0][1] == {"modules": "defaultKeyStatistics"}
    assert floatshort.cached(eng, "ACME", 1000.0 + 86400)["float"] == 30_000_000
    assert floatshort.cached(eng, "ACME", 1000.0 + 8 * 86400) is None  # a week on: read again

    class Broken:
        async def yahoo_json(self, url, params):
            raise RuntimeError("401")

    eng.smallcap_feed = Broken()
    assert await floatshort.fetch(eng, "BETA") is None and floatshort.cached(eng, "BETA") is None


def floated(eng, n, fl, close, title="Acme wins Pentagon contract", run=None):
    for i in range(n):
        a = news(f"{title} {fl} {close} {i}")
        a.created = time.time() - 86400 * (2 + i)
        a.edge = {
            "float": stamp_of({"float": fl, "short": 0.25}),
            "outcome": {"ACME": {"sign": 1, "close": close}},
        }
        if run is not None:
            a.edge["exc"] = {"mfe": run, "mae": -0.5}
        eng.storage.add_alert(a)


def test_news_by_float_and_short(tmp_path):
    eng = semis_engine(tmp_path)
    floated(eng, 7, 6e6, 6.0, run=9.0)
    floated(eng, 3, 6e6, -2.0, run=1.0)
    floated(eng, 9, 400e6, -0.5)
    r = float_rates(eng)
    t = r["f:tiny|contract"]
    assert (t["n"], t["hit"], t["median"], t["run"]) == (10, 0.7, 6.0, 9.0)
    assert t["label"] == "Contract win on a float under 10M shares"
    assert r["s:high|*"]["n"] == 19 and r["f:large|*"]["hit"] == 0.0
    assert float_for(r, "contract", stamp_of({"float": 5e6, "short": 0.2})) is t
    assert float_for(r, "fda_approval", stamp_of({"float": 5e6})) is r["f:tiny|*"]  # any news on that float
    assert float_for(r, "contract", None) is None
    assert (
        float_line(t)
        == "Contract win on a float under 10M shares: went its way 70% by the close (median +6.0%, the winners' median run +9.0%, 10)"
    )
    b = floatshort.board(r)
    assert [x["band"] for x in b["float"]] == ["tiny", "large"] and b["short"][0]["band"] == "high"


def test_the_call_reads_the_float():
    rec = {"label": "Contract win on a float under 10M shares", "n": 12, "hit": 0.75, "median": 5.0}
    c = the_call({"float_base": rec, "odds": {"p": 0.65}})
    assert c["verb"] == "act" and "contract win on a float under 10M shares 75%" in c["for"]
    assert ["float", "+"] in c["facts"]
    assert the_call({"float_base": {**rec, "hit": 0.3}})["verb"] == "skip"
    assert the_call({"float_base": {**rec, "n": 5}}) is None  # too few graded to speak"


async def test_run50_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    floated(eng, 9, 6e6, 2.0)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/line"), headers=h) as r:
                assert "kinds" in await r.json()
            async with s.post(
                srv.make_url("/api/line/kind"),
                headers=h,
                json={"kind": "contract", "offset": -5, "why": "test"},
            ) as r:
                assert (await r.json())["offset"] == -5
            async with s.post(
                srv.make_url("/api/line/kind"), headers=h, json={"kind": "contract", "offset": -50}
            ) as r:
                assert r.status == 400
            async with s.get(srv.make_url("/api/float"), headers=h) as r:
                assert (await r.json())["float"][0]["band"] == "tiny"
    finally:
        await srv.close()
        await eng.http.close()


async def test_alerts_carry_their_float(tmp_path):
    from news247.floatshort import stamp_later

    eng = semis_engine(tmp_path)
    floated(eng, 9, 6e6, 3.0)
    eng.refresh_base_rates()
    eng.storage.set_setting("float:ACME", json.dumps({"float": 7_000_000, "short": 0.31, "ts": time.time()}))
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert a.edge["float"] == {"float": 7_000_000, "short": 0.31, "fband": "tiny", "sband": "crowded"}
    assert a.edge["float_base"]["key"] == "f:tiny|contract" and a.edge["float_base"]["hit"] == 1.0

    # a stock not read this week: read in the background, then stamped
    class Feed:
        async def yahoo_json(self, url, params):
            return {"quoteSummary": {"result": [{"defaultKeyStatistics": {"floatShares": {"raw": 5e6}}}]}}

    eng.smallcap_feed = Feed()
    b = news("Beta wins $1B Army contract", tickers=("BETA",))
    eng.storage.add_alert(b)
    await stamp_later(eng, b.id, "BETA", "contract")
    e = eng.storage.get_alert(b.id)["edge"]
    assert e["float"]["fband"] == "tiny" and e["float_base"]["key"] == "f:tiny|contract"


def test_the_advice_moves_a_kinds_line(tmp_path):
    from news247.advice import advise

    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    line = eng.cfg.scoring.medium
    for i in range(16):
        over(eng, "Analyst downgrades Acme to sell", line + 2, -1.5 if i < 11 else 0.5, i=i)
    item = next(i for i in advise(eng, DEV)["items"] if i["key"].startswith("kline:downgrade"))
    assert item["title"] == f"Raise the line for analyst downgrade news to {line + 5:g}"
    assert (
        item["api"] == "/api/line/kind"
        and item["body"]["kind"] == "downgrade"
        and item["body"]["offset"] == 5
    )
