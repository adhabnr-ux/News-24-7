"""Run 24: the analyst-firm scorecard (each firm's calls, graded here) and putting the odds to work
(an odds edge bar, an autopilot that follows the model, and the model's forward record)."""

from __future__ import annotations

import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.autopilot import clean_rule, matches, rule_label
from news247.desk import Desk
from news247.firms import firm_base, firm_line, firm_rates, read_call, scorecard
from news247.odds import fit_odds, live_record
from news247.storage import Storage
from news247.targets import targets_for
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import quotes, semis_engine
from .test_run3 import item, phones
from .test_run15 import graded
from .test_run17 import store
from .test_run23 import history, noon

# --------------------------------------------------------------------------- reading a call


@pytest.mark.parametrize(
    "title,firm,action",
    [
        ("Goldman Sachs upgrades Acme to buy", "Goldman Sachs", "upgrade"),
        ("Acme downgraded to sell at Morgan Stanley", "Morgan Stanley", "downgrade"),
        ("Jefferies raises Acme price target to $150 from $120", "Jefferies", "raise"),
        ("Acme PT lowered to $80 at Piper Sandler", "Piper Sandler", "cut"),
        ("Citi initiates Acme at buy with $60 target", "Citi", "initiate"),
    ],
)
def test_a_firms_call_is_read_from_the_headline(title, firm, action):
    call = read_call(news(title).to_dict())
    assert (call["firm"], call["action"]) == (firm, action)


def test_headlines_that_are_not_a_firms_call_are_skipped():
    for t in ("Acme upgraded by analysts", "Goldman Sachs reports record revenue", "Acme wins Barclays contract",
              "Wedbush reiterates outperform, $200 PT on Acme"):  # fmt: skip
        assert read_call(news(t).to_dict()) is None, t


# --------------------------------------------------------------------------- the scorecard


def calls(eng):
    for i, r in enumerate([3.0, 2.0, 1.5, -1.0, 2.5, 1.0]):  # Goldman's upgrades: followed through
        eng.storage.add_alert(graded(f"Goldman Sachs upgrades Acme {i}", r))
    for i, r in enumerate([-2.0, -1.0, 0.5, -1.5]):  # Barclays' upgrades: faded
        eng.storage.add_alert(graded(f"Barclays upgrades Acme {i}", r))
    for i, r in enumerate([1.0, -0.5]):  # Barclays' downgrades: two shorts, one worked
        eng.storage.add_alert(graded(f"Barclays downgrades Acme {i}", r, sign=-1))
    eng.storage.add_alert(graded("Mizuho upgrades Acme", 4.0))  # one call: too few


def test_each_firms_calls_are_graded(tmp_path):
    eng = semis_engine(tmp_path)
    calls(eng)
    rates = firm_rates(eng)
    gs = rates["Goldman Sachs|upgrade"]["close"]
    assert gs == {"n": 6, "hit": 0.83, "median": 1.75, "avg": 1.5}
    assert rates["Barclays|*"]["close"]["n"] == 6 and rates["Barclays|downgrade"]["close"]["hit"] == 0.5
    fb = firm_base(rates, {"firm": "Goldman Sachs", "action": "upgrade", "label": "Goldman Sachs upgrades"})
    assert firm_line(fb) == "Goldman Sachs upgrades here: 83% went their way by the close (median +1.8%, 6)"
    # two downgrades are too few for their own record: Barclays' calls of every kind stand in
    bd = firm_base(rates, {"firm": "Barclays", "action": "downgrade", "label": "Barclays downgrades"})
    assert bd["action"] == "*" and bd["label"] == "Every Barclays call" and bd["close"]["n"] == 6
    assert firm_base(rates, {"firm": "Mizuho", "action": "upgrade", "label": "x"}) is None
    sc = scorecard(rates)
    assert [f["firm"] for f in sc["best"]] == ["Goldman Sachs"] and [f["firm"] for f in sc["worst"]] == [
        "Barclays"
    ]
    assert sc["best"][0]["actions"][0]["label"] == "upgrades" and sc["thin"] == 1 and sc["firms"] == 2


async def test_a_new_call_carries_its_firms_record(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    calls(eng)
    eng.refresh_base_rates()
    a = news("Goldman Sachs upgrades Acme to buy")
    await eng.publish(a, push=False)
    edge = eng.storage.get_alert(a.id)["edge"]
    assert edge["firm"] == {"firm": "Goldman Sachs", "action": "upgrade", "label": "Goldman Sachs upgrades"}
    assert edge["firm_base"]["close"]["n"] == 6
    assert eng.webpush.payload(a)["body"].endswith(
        "Goldman Sachs upgrades here: 83% went their way by the close (median +1.8%, 6)"
    )
    # a big target change carries the firm's record on target calls
    for i, r in enumerate([2.0, 1.0, 3.0, 0.5]):
        eng.storage.add_alert(graded(f"Jefferies raises Acme price target to ${150 + i} from $120", r))
    eng.refresh_base_rates()
    await eng.http.start()
    try:
        a_ph, _ = phones(eng, server)
        quotes(eng, time.time(), ACME=8.5)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        assert await eng._target_push(item("Jefferies raises Acme price target to $160 from $120")) == 1
        body = a_ph.read(server.requests[0]["body"])["body"]
        assert body.endswith(
            "Jefferies target raises here: 100% went their way by the close (median +1.5%, 4)"
        )
    finally:
        await eng.http.close()


def test_a_stocks_targets_show_each_firms_record(tmp_path):
    eng = semis_engine(tmp_path)
    for i, r in enumerate([2.0, 1.0, 3.0, 0.5]):
        eng.storage.add_alert(graded(f"Jefferies raises Acme price target to ${150 + i} from $120", r))
    eng.refresh_base_rates()
    quotes(eng, time.time(), ACME=0.0)
    store(eng, "Jefferies raises Acme price target to $170 from $150")
    store(eng, "Acme price target cut to $90 from $100 by Piper Sandler", ago=7200)
    rows = {r["firm"]: r for r in targets_for(eng, "ACME")["rows"]}
    assert rows["Jefferies"]["record"] == {
        "label": "Jefferies target raises",
        "n": 4,
        "hit": 1.0,
        "median": 1.5,
        "avg": 1.62,
    }
    assert "record" not in rows["Piper Sandler"]


# --------------------------------------------------------------------------- putting the odds to work


def with_odds(p, title="Acme wins contract", direction="up"):
    a = news(title, direction=direction)
    a.edge = {"odds": {"p": p, "base": 0.5, "n": 200, "skill": 0.2, "why": []}}
    return a


def test_the_odds_bar_holds_back_low_odds(tmp_path):
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "prefs", {"edge_min": "odds60"})
    b = d.block(DEV, with_odds(0.55))
    assert b["code"] == "edge" and b["text"] == "its odds (55%) are under your 60% bar"
    assert b["fix"]["body"] == {"edge_min": None}
    assert d.allows(DEV, with_odds(0.62)) is True
    assert (
        d.allows(DEV, news("Acme hosts investor day")) is True
    )  # no odds (the model isn't in use): not held back
    d.apply(DEV, "stock_push", {"symbol": "ACME", "level": "all"})
    assert d.allows(DEV, with_odds(0.4)) is True  # a stock set to everything wins
    with pytest.raises(ValueError, match="odds55, odds60, odds65"):
        d.apply(DEV, "prefs", {"edge_min": "odds99"})


def test_an_autopilot_can_follow_the_model():
    r = clean_rule({"catalyst": "all", "min_sev": "HIGH", "exit": "close", "min_odds": 60}, 0)
    assert r["label"] == "Any kind of news, any size, HIGH+, odds 60%+, out at the close"
    assert matches(r, with_odds(0.64).to_dict())
    assert not matches(r, with_odds(0.58).to_dict())
    assert not matches(r, news("Acme wins contract").to_dict())  # no odds: no call
    # with a kind of news too, both must hold
    rc = clean_rule({"catalyst": "contract", "min_odds": 60}, 0)
    a = with_odds(0.7).to_dict()
    assert not matches(rc, a)  # no kind of news read on it
    a["edge"]["catalyst"] = {"key": "contract", "label": "Contract win", "band": "mid"}
    assert matches(rc, a)
    assert rule_label({**r, "min_odds": 0}) == "Any kind of news, any size, HIGH+, out at the close"
    with pytest.raises(ValueError, match="odds are"):
        clean_rule({"min_odds": 50}, 0)


async def test_the_model_autopilot_opens_paper_calls(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        now = time.time()
        eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
        eng.implied_cache["ACME"] = (now, {"rows": [{"expiry": "x", "move_pct": 4.0}]})  # the plan's target
        eng.desk.apply(
            DEV, "autopilot_add", {"catalyst": "all", "min_sev": "HIGH", "exit": "close", "min_odds": 60}
        )
        eng.desk.apply(
            DEV, "autopilot_add", {"catalyst": "all", "min_sev": "HIGH", "exit": "close"}
        )  # a different rule
        a = with_odds(0.66, "Acme hosts investor day")  # no kind of news, but the model likes it
        a.edge["refs"] = {"ACME": 100.0}
        eng.storage.add_alert(a)
        assert await eng.autopilot_open(eng.storage.get_alert(a.id), now) == 1  # only the model's rule
        t = eng.desk.get(DEV)["tracked"][0]
        assert t["auto"] == eng.desk.get(DEV)["autopilots"][0]["id"] and t["target"] == pytest.approx(104.0)
    finally:
        await eng.http.close()


async def test_the_odds_keep_a_forward_record(tmp_path):
    assert live_record([]) == {"n": 0}
    lr = live_record([(0.7, 1), (0.7, 1), (0.7, 0), (0.3, 0), (0.3, 0), (0.3, 1)])
    assert (lr["n"], lr["predicted"], lr["actual"]) == (6, 0.5, 0.5) and lr["brier"] < lr["brier_guess"]
    assert [(b["lo"], b["n"], b["actual"]) for b in lr["reliability"]] == [(0.0, 3, 0.333), (0.7, 3, 0.667)]
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    for i, good in enumerate([True, True, False]):  # three that went out with odds, graded since
        a = news(f"Acme wins contract award {i}")
        a.created = noon(1)
        await eng.publish(a, push=False)
        eng.storage.update_alert_edge(
            a.id, {"outcome": {"ACME": {"h1": None, "close": 2.0 if good else -1.0, "d1": None, "sign": 1}}}
        )
    st = fit_odds(eng)
    assert st["live"]["n"] == 3 and st["live"]["actual"] == 0.667


async def test_run24_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    calls(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/firms"), headers=h) as r:
                assert (await r.json())["best"][0]["firm"] == "Goldman Sachs"
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "prefs", "edge_min": "odds65"}, headers=h
            ) as r:
                assert (await r.json())["prefs"]["edge_min"] == "odds65"
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "autopilot_add", "min_odds": 65}, headers=h
            ) as r:
                assert (await r.json())["autopilots"][0]["min_odds"] == 65
            async with s.get(srv.make_url("/api/odds"), headers=h) as r:
                assert (await r.json())["live"] == {"n": 0}
    finally:
        await srv.close()
        await eng.http.close()
