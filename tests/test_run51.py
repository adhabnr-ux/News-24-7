"""Run 51: dilution risk from the company's own filings (selling shares now, a shelf on file, or
neither), graded and counted in the call; and every change made on the evidence, graded since."""

from __future__ import annotations

import json
import time
from datetime import date

import aiohttp
from aiohttp.test_utils import TestServer

from news247 import dilution
from news247.changes import changes, kind_offs, line_moves
from news247.dilution import dilution_for, dilution_rates, stamp_of, state_of
from news247.linekinds import set_kind_line
from news247.shadow import set_line
from news247.thecall import the_call
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

TODAY = date(2026, 10, 20)


def subs(*rows):
    return {"filings": {"recent": {"form": [f for f, _ in rows], "filingDate": [d for _, d in rows]}}}


# --------------------------------------------------------------------------- dilution risk


def test_reading_the_filings():
    selling = subs(
        ("8-K", "2026-10-01"), ("424B5", "2026-09-15"), ("424B5", "2026-07-01"), ("S-3", "2025-02-10")
    )
    assert state_of(selling, TODAY) == {
        "state": "selling",
        "shelf": "2025-02-10",
        "sales": 2,
        "last_sale": "2026-09-15",
    }
    shelf = subs(("S-3", "2024-06-01"), ("424B3", "2025-01-01"))  # the sale is over 180 days old
    assert state_of(shelf, TODAY) == {"state": "shelf", "shelf": "2024-06-01", "sales": 0, "last_sale": None}
    assert (
        state_of(subs(("10-K", "2026-03-01"), ("S-3", "2022-01-01")), TODAY)["state"] == "none"
    )  # shelf expired
    assert state_of({}, TODAY) is None


async def test_filings_are_read_and_kept_a_day(tmp_path):
    eng = semis_engine(tmp_path)
    seen = []

    class Resp:
        def __init__(self, doc):
            self.doc = doc

        def json(self):
            return self.doc

    async def get(url, **kw):
        seen.append((url, kw["headers"]["User-Agent"]))
        if url.endswith("company_tickers.json"):
            return Resp({"0": {"cik_str": 320193, "ticker": "ACME"}})
        return Resp(subs(("424B5", "2026-10-01")))

    eng.http.get = get
    now = time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1))
    d = await dilution.fetch(eng, "ACME", now)
    assert d["state"] == "selling" and seen[1][0] == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert seen[0][1] == eng.cfg.general.user_agent  # the SEC wants a named agent
    assert dilution.cached(eng, "ACME", now + 3600)["state"] == "selling"
    assert dilution.cached(eng, "ACME", now + 2 * 86400) is None
    assert await dilution.fetch(eng, "ZZZZ", now) is None  # no CIK: nothing stamped


def diluted(eng, n, state, close, d5=None, title="Acme wins Pentagon contract"):
    for i in range(n):
        a = news(f"{title} {state} {close} {i}")
        a.created = time.time() - 86400 * (8 + i)
        row = {"sign": 1, "close": close}
        if d5 is not None:
            row["d5"] = d5
        a.edge = {
            "dilution": {"state": state, "shelf": None, "sales": 1, "last_sale": None},
            "outcome": {"ACME": row},
        }
        eng.storage.add_alert(a)


def test_news_by_dilution_risk(tmp_path):
    eng = semis_engine(tmp_path)
    diluted(eng, 6, "selling", -2.0, d5=-6.0)
    diluted(eng, 3, "selling", 1.0, d5=-1.0)
    diluted(eng, 8, "none", 2.0, d5=3.0)
    r = dilution_rates(eng)
    s = r["selling|contract"]
    assert s["label"] == "Contract win on a stock with selling shares now"
    assert (s["close"]["n"], s["close"]["hit"], s["d5"]["hit"], s["d5"]["median"]) == (9, 0.33, 0.0, -6.0)
    assert dilution_for(r, "contract", {"state": "selling"}) is s
    assert dilution_for(r, "fda_approval", {"state": "none"}) is r["none|*"]
    assert dilution_for(r, "contract", {"state": "shelf"}) is None  # too few graded
    assert [x["state"] for x in dilution.board(r)["rows"]] == ["selling", "none"]
    assert stamp_of({"state": "shelf", "shelf": "2025-01-01", "sales": 0, "last_sale": None, "ts": 1}) == {
        "state": "shelf", "shelf": "2025-01-01", "sales": 0, "last_sale": None}  # fmt: skip


def test_the_call_reads_the_dilution_risk():
    base = {
        "label": "Contract win on a stock with selling shares now",
        "close": {"n": 9, "hit": 0.33, "median": -2.0},
    }
    c = the_call({"dilution_base": base})
    assert c["verb"] == "skip" and c["against"] == ["contract win on a stock with selling shares now 33%"]
    assert ["dilution", "-"] in c["facts"]


async def test_alerts_carry_their_dilution_risk(tmp_path):
    eng = semis_engine(tmp_path)
    diluted(eng, 9, "selling", -2.0)
    eng.refresh_base_rates()
    eng.storage.set_setting("dilution:ACME", json.dumps({"state": "selling", "shelf": "2025-02-10", "sales": 2,
                                                         "last_sale": "2026-09-15", "ts": time.time()}))  # fmt: skip
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert a.edge["dilution"]["state"] == "selling" and a.edge["dilution_base"]["key"] == "selling|contract"
    assert "dilution" in [k for k, _ in a.edge["call"]["facts"]]


# --------------------------------------------------------------------------- your changes, graded


def graded(eng, title, close, created, score=None):
    a = news(title)
    a.created = created
    if score is not None:
        a.analysis.score = score
    a.edge = {"outcome": {"ACME": {"sign": 1, "close": close}}}
    eng.storage.add_alert(a)


def test_a_kind_switched_off_is_graded(tmp_path):
    from news247.advice import advise

    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    at = time.time() - 20 * 86400
    eng.desk.apply(DEV, "cat_mute", {"key": "contract", "on": True})
    eng.desk.apply(DEV, "advice_dismiss", {"key": "off:contract", "applied": True})
    eng.desk.apply(DEV, "advice_dismiss", {"key": "off:downgrade"})  # dismissed, not applied: not graded
    eng.desk.profiles[DEV]["advice_applied"]["off:contract"] = at
    for i in range(12):  # contract wins kept winning after the switch
        graded(eng, f"Acme wins Pentagon contract {i}", 2.0 if i < 9 else -1.0, at + 86400 * (i + 1))
    graded(eng, "Acme wins old contract", -3.0, at - 86400)  # before the switch: not counted
    rows = kind_offs(eng, DEV)
    assert len(rows) == 1
    r = rows[0]
    assert r["what"] == "Switched off contract win news" and r["since"] == {
        "n": 12,
        "hit": 0.75,
        "median": 2.0,
    }
    assert r["verdict"] == "backfired" and r["undo"]["body"] == {"key": "contract", "on": False}
    item = next(i for i in advise(eng, DEV)["items"] if i["key"].startswith("undo:"))
    assert item["title"] == "Undo: switched off contract win news" and item["action"] == "cat_mute"
    assert (
        item["why"]
        == "Since then, 12 graded went their way 75% by the close (median +2.0%): the change backfired."
    )


def test_line_moves_are_graded(tmp_path):
    eng = semis_engine(tmp_path)
    line = eng.cfg.scoring.medium
    at = time.time() - 20 * 86400
    set_line(eng, line - 5, "near misses ran", now=at)
    for i in range(12):  # the alerts the lower line added lost
        graded(eng, f"Acme talks {i}", -1.0 if i < 9 else 1.0, at + 86400 * (i + 1), score=line - 3)
        graded(eng, f"Acme big news {i}", 2.0 if i < 8 else -1.0, at + 86400 * (i + 1), score=line + 2)
    set_kind_line(eng, "downgrade", 5, now=at + 3600)
    for i in range(10):  # the headlines the raise kept back won
        eng.storage.add_shadow(f"d{i}", at + 86400 * (i + 1), {"symbol": "ACME", "sign": 1, "ref": 100.0, "score": line - 3,
                                                                "line": line, "source": "x", "title": "Analyst downgrades Acme",
                                                                "kind": "downgrade", "close": 1.5})  # fmt: skip
    rows = line_moves(eng)
    kl, main = rows[0], rows[1]
    assert (
        kl["what"] == f"Raised the line for analyst downgrade news {line - 5:g} → {line:g}"
    )  # off the lowered main line
    assert kl["since"]["hit"] == 1.0 and kl["verdict"] == "backfired"
    assert kl["undo"]["api"] == "/api/line/kind" and kl["undo"]["body"]["offset"] is None
    assert main["what"] == f"Lowered the alert line {line:g} → {line - 5:g}"
    assert main["since"]["hit"] == 0.25 and main["ref"]["hit"] == 0.67 and main["verdict"] == "backfired"
    assert main["undo"]["body"] == {"medium": line, "why": "the move backfired"}
    set_line(eng, None)  # put back: the old move is no longer the line as it stands
    assert all(r["undo"] is None for r in line_moves(eng) if r["what"].startswith("Lowered the alert line"))


async def test_run51_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    diluted(eng, 8, "none", 2.0)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/dilution"), headers=h) as r:
                assert (await r.json())["rows"][0]["state"] == "none"
            async with s.get(srv.make_url("/api/changes"), headers=h) as r:
                assert await r.json() == changes(eng, DEV)
    finally:
        await srv.close()
        await eng.http.close()
