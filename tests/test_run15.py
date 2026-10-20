"""Run 15: catalyst base rates (what kind of news, at what company size, and how that has traded)
and "room left" (today's alerts that haven't yet made their catalyst's typical move)."""

from __future__ import annotations

import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.catalysts import (
    cat_line,
    catalyst_base,
    catalyst_rates,
    catalyst_table,
    classify,
    room_left,
    size_band,
)
from news247.models import Severity
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import quotes, semis_engine

HEADLINES = {
    "Acme wins $2B Pentagon contract": "contract",
    "Acme beats estimates, raises full-year guidance": "earnings_beat",
    "Acme Q3 revenue misses estimates": "earnings_miss",
    "FDA approves Acme's drug for lupus": "fda_approval",
    "Acme receives complete response letter from FDA": "fda_negative",
    "Acme announces pricing of $200M public offering": "offering",
    "Morgan Stanley upgrades Acme to overweight": "upgrade",
    "Acme downgraded to neutral at Goldman": "downgrade",
    "Acme CEO steps down effective immediately": "ceo_exit",
    "Hindenburg Research discloses short position in Acme": "short_report",
    "Acme to join S&P 500": "index_add",
    "Acme announces $5B share repurchase program": "buyback",
    "Acme lowers full-year guidance": "guidance_cut",
    "Acme trial meets primary endpoint": "trial_win",
    "Acme phase 3 trial fails to meet primary endpoint": "trial_fail",
    "SEC probe into Acme accounting": "probe",
    "Acme recalls 1M vehicles": "recall",
    "Acme partners with Nvidia on AI": "partnership",
    "Acme to be acquired by Beta for $40 a share": "acquired",
    "Acme files for Chapter 11 bankruptcy": "bankruptcy",
    "Acme raises quarterly dividend 10%": "dividend_up",
    "Acme to cut 4,000 jobs": "layoffs",
}


def test_headlines_read_as_catalysts():
    for title, key in HEADLINES.items():
        c = classify({"kind": "news", "title": title, "tickers": ["ACME"]})
        assert c and c["key"] == key, (title, c)
    assert classify({"kind": "news", "title": "Acme hosts investor day", "tickers": ["ACME"]}) is None
    assert classify({"kind": "options", "title": "ACME calls bought", "tickers": ["ACME"]}) is None
    ins = {
        "kind": "news",
        "title": "CEO buys $2M",
        "tickers": ["ACME"],
        "item": {"extra": {"insider_buy": {"role": "CEO"}}},
    }
    assert classify(ins)["key"] == "insider_buy"
    # a radar move is read through the headline behind it
    move = {"kind": "price", "title": "▲ ACME +8%", "tickers": ["ACME"],
            "related": [{"kind": "news", "title": "FDA approves Acme's drug"}]}  # fmt: skip
    assert classify(move)["key"] == "fda_approval"
    assert classify({**move, "related": []}) is None
    # the deal reader decides who is the target
    deal = {
        "kind": "news",
        "title": "Beta to buy Acme",
        "tickers": ["BETA"],
        "analysis": {"deal": {"target": "ACME", "buyer": "BETA"}},
    }
    assert classify(deal)["key"] == "acquirer"
    assert (
        size_band(5e8) == "small"
        and size_band(5e9) == "mid"
        and size_band(5e10) == "large"
        and size_band(None) == "*"
    )


def graded(title, close, sign=1, sym="ACME", h1=None):
    a = news(title, tickers=(sym,), direction="up" if sign > 0 else "down")
    a.created = time.time() - 86400 * 3
    a.edge = {"outcome": {sym: {"h1": h1, "close": close * sign, "d1": None, "sign": sign}}}
    return a


def test_rates_pool_by_catalyst_and_size_and_fall_back(tmp_path):
    eng = semis_engine(tmp_path)  # ACME is a $5B company: the $2-10B band
    for i, r in enumerate([3.0, 2.0, 4.0, -1.0, 2.5, 3.5]):
        eng.storage.add_alert(graded(f"Acme wins contract number {i}", r))
    for i, r in enumerate([1.0, -2.0, 1.5]):  # BETA is $20B: a different band
        eng.storage.add_alert(graded(f"Beta wins contract number {i}", r, sym="BETA"))
    eng.storage.add_alert(graded("Acme downgraded to sell", 2.0, sign=-1))
    rates = catalyst_rates(eng)
    mid = rates["contract|mid"]
    assert mid["close"] == {"n": 6, "hit": 0.83, "median": 2.75} and mid["typical"] == 2.75
    assert rates["contract|*"]["close"]["n"] == 9 and rates["contract|large"]["close"]["n"] == 3
    assert rates["downgrade|mid"]["close"]["median"] == 2.0  # a short that worked counts as its way
    assert catalyst_base(rates, {"key": "contract", "band": "mid"})["band"] == "mid"
    assert catalyst_base(rates, {"key": "contract", "band": "large"})["band"] == "*"  # 3 is too few: any size
    assert catalyst_base(rates, {"key": "downgrade", "band": "mid"}) is None
    assert catalyst_base(rates, None) is None
    assert cat_line(mid) == "Contract win, $2-10B: 83% went their way by the close (median +2.8%, 6)"
    table = catalyst_table(rates)
    assert table[0]["key"] == "contract" and [b["band"] for b in table[0]["bands"]] == ["mid", "large"]


async def test_new_alerts_carry_their_catalyst_and_its_record(tmp_path):
    eng = semis_engine(tmp_path)
    for i, r in enumerate([3.0, 2.0, 4.0, -1.0, 2.5, 3.5]):
        eng.storage.add_alert(graded(f"Acme wins contract number {i}", r))
    eng.refresh_base_rates()
    a = news("Acme wins $900M Navy contract")
    await eng.publish(a, push=False)
    stored = eng.storage.get_alert(a.id)["edge"]
    assert stored["catalyst"] == {"key": "contract", "label": "Contract win", "band": "mid"}
    assert stored["cat_base"]["close"]["n"] == 6
    assert "Contract win, $2-10B: 83% went their way" in eng.webpush.payload(a)["body"]
    plain = news("Acme hosts investor day")
    await eng.publish(plain, push=False)
    assert "catalyst" not in eng.storage.get_alert(plain.id)["edge"]


def test_room_left_ranks_todays_alerts_against_their_record(tmp_path):
    eng = semis_engine(tmp_path)
    for i, r in enumerate([3.0, 2.0, 4.0, -1.0, 2.5, 3.5]):
        eng.storage.add_alert(graded(f"Acme wins contract number {i}", r))
    for i, r in enumerate([3.0, 2.0, 4.0, -1.0, 2.5, 3.5]):
        eng.storage.add_alert(graded(f"Delta wins contract number {i}", r, sym="DLTA"))
    eng.refresh_base_rates()
    now = time.time()
    early = news("Acme wins $900M Navy contract")
    early.created = now - 600
    early.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(early)
    done = news("Delta wins Army contract", tickers=("DLTA",))
    done.created = now - 600
    done.edge = {"refs": {"DLTA": 100.0}}
    eng.storage.add_alert(done)
    quotes(eng, now, ACME=0.5, DLTA=4.0)
    r = room_left(eng, now)
    assert [x["symbol"] for x in r["rows"]] == ["ACME"]  # DLTA already made more than the typical move
    row = r["rows"][0]
    assert (row["catalyst"], row["since"], row["typical"], row["room"]) == ("Contract win", 0.5, 2.75, 2.25)
    assert row["n"] == 6 and row["hit"] == 0.83
    quotes(eng, now, ACME=-3.0)
    assert room_left(eng, now)["rows"] == []  # broken the other way
    eng.catalyst_rates = {}
    assert room_left(eng, now)["rows"] == []  # no record, nothing said


async def test_run15_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    for i, r in enumerate([3.0, 2.0, 4.0, -1.0, 2.5, 3.5]):
        eng.storage.add_alert(graded(f"Acme wins contract number {i}", r))
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/record"), headers=h) as r:
                cats = (await r.json())["catalysts"]
                assert cats[0]["label"] == "Contract win" and cats[0]["close"]["n"] == 6
            async with s.get(srv.make_url("/api/room"), headers=h) as r:
                body = await r.json()
                assert body["rows"] == [] and body["rated"] == 1
    finally:
        await srv.close()
        await eng.http.close()


@pytest.mark.parametrize("sev", [Severity.MEDIUM])
def test_medium_alerts_count_too(tmp_path, sev):
    eng = semis_engine(tmp_path)
    a = graded("Acme wins contract", 2.0)
    a.severity = sev
    eng.storage.add_alert(a)
    assert catalyst_rates(eng)["contract|mid"]["close"]["n"] == 1
