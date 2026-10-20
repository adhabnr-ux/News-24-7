"""Run 36: stock personality (how each stock has taken its own news here) and the exit autopsy
(what each closed paper call's stock did after the exit)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.dossier import ticker_dossier
from news247.exits import exit_kind, exit_report, from_alert, grade_exits
from news247.persona import board, persona, persona_line, persona_rates, stamp, trait
from news247.precheck import pre_trade
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

ET = ZoneInfo("America/New_York")


def ga(eng, sym, i, close, h1=None, d5=None, sign=1, title=None):
    """A graded alert on ``sym``: moves given its way, stored raw (as the grader stamps them)."""
    a = news(title or f"{sym} headline number {i}", tickers=(sym,), direction="up" if sign > 0 else "down")
    a.created = time.time() - 86400 * 8 - i * 600
    row = {"close": close * sign, "sign": sign, "h1": h1 * sign if h1 is not None else None}
    if d5 is not None:
        row["d5"] = d5 * sign
    a.edge = {"refs": {sym: 100.0}, "outcome": {sym: row}}
    eng.storage.add_alert(a)
    return a


def characters(eng):
    # ACME pops in the first hour and gives it back by the close
    for i, (h1, c) in enumerate(
        zip([2.0, 1.5, 2.5, 1.8, 2.2, 1.6], [0.2, -0.3, 0.5, 0.2, 0.4, -0.1], strict=True)
    ):
        ga(eng, "ACME", i, c, h1=h1, title=f"Acme wins contract number {i}")
    # BETA closes its way and keeps going for days
    for i, (c, d5) in enumerate(
        zip([1.0, 2.0, 1.5, 0.5, -0.5, 1.2], [4.0, 3.0, 5.0, 2.5, 1.0, 3.5], strict=True)
    ):
        ga(eng, "BETA", i, c, h1=0.5, d5=d5)
    # DLTA goes against its news (its alerts called it down; it mostly rose)
    for i, c in enumerate([-1.0, -2.0, -0.5, 1.0, -1.5, -0.8]):
        ga(eng, "DLTA", i, c, sign=-1)
    for i in range(3):  # GAMA: too few to say
        ga(eng, "GAMA", i, 0.1)


# --------------------------------------------------------------------------- stock personality


def test_each_shape_has_a_name():
    c = lambda n, hit, med: {"n": n, "hit": hit, "median": med}  # noqa: E731
    assert trait({"close": c(6, 0.5, 0.1), "size": 0.2}, 1.5) == "quiet"
    assert trait({"close": c(6, 0.17, -0.9), "size": 1.0}, 1.5) == "fights"
    assert trait({"close": c(6, 0.6, 0.2), "h1": c(6, 0.9, 1.9), "size": 1.9}, 1.5) == "fades_day"
    assert trait({"close": c(8, 0.75, 1.5), "d5": c(5, 0.4, 0.2), "size": 1.5}, 1.5) == "fades_days"
    assert trait({"close": c(8, 0.8, 1.1), "d5": c(5, 0.9, 3.25), "size": 1.1}, 1.5) == "drifts"
    assert trait({"close": c(8, 0.75, 1.2), "d5": c(5, 0.8, 1.4), "size": 1.2}, 1.5) == "runs"
    assert trait({"close": c(8, 0.5, 0.2), "size": 1.2}, 1.5) == "mixed"
    assert trait({"close": c(5, 0.9, 3.0), "size": 3.0}, 1.5) == "mixed"  # too few to have a shape


def test_each_stock_gets_its_own_profile(tmp_path):
    eng = semis_engine(tmp_path)
    characters(eng)
    rates = persona_rates(eng)
    assert {k: v["trait"] for k, v in rates.items() if not k.startswith("_")} == {
        "ACME": "fades_day",
        "BETA": "drifts",
        "DLTA": "fights",
    }  # GAMA has 3 graded: no profile yet
    acme = rates["ACME"]
    assert acme["h1"] == {"n": 6, "hit": 1.0, "median": 1.9} and acme["close"]["median"] == 0.2
    assert acme["size"] == 1.9 and rates["_typical"] is not None and acme["ratio"] > 1
    ps = stamp(rates, "ACME")
    assert ps["path"] == {"h1": 1.9, "close": 0.2} and ps["label"] == "Fades by the close"
    assert (
        persona_line(ps)
        == "ACME and its news: fades by the close (+1.9% in an hour, +0.2% by the close; median, its way, 6 graded)"
    )
    assert stamp(rates, "BETA")["path"]["d5"] == 3.25 and stamp(rates, "GAMA") is None
    assert persona_line(None) == ""


async def test_new_alerts_carry_the_stocks_personality(tmp_path):
    eng = semis_engine(tmp_path)
    characters(eng)
    eng.refresh_base_rates()
    a = news("Acme lands another contract")
    await eng.publish(a, push=False)
    assert a.edge["persona"]["trait"] == "fades_day"
    assert "ACME and its news: fades by the close" in eng.webpush.payload(a)["body"]
    b = news("Gama Health files 10-Q", tickers=("GAMA",))
    await eng.publish(b, push=False)
    assert "persona" not in b.edge  # too few graded on GAMA


def test_the_personality_on_the_page_and_in_the_check(tmp_path):
    eng = semis_engine(tmp_path)
    characters(eng)
    eng.refresh_base_rates()
    d = ticker_dossier(eng, "ACME")
    assert d["persona"]["label"] == "Fades by the close" and d["persona"]["tone"] == "warn"
    a = news("Acme lands another contract")
    eng.storage.add_alert(a)
    check = lambda r: next(c for c in r["checks"] if c["key"] == "persona")  # noqa: E731
    long = check(pre_trade(eng, DEV, "ACME", "long", a.id))
    assert long["tone"] == "warn" and "first-hour moves mostly didn't last the day" in long["text"]
    assert check(pre_trade(eng, DEV, "ACME", "short", a.id))["tone"] == "info"  # against the news: context
    assert check(pre_trade(eng, DEV, "DLTA", "long"))["tone"] == "info"  # no alert: context
    assert check(pre_trade(eng, DEV, "BETA", "long"))["text"].startswith("BETA and its news: keeps drifting")
    full = persona(eng, "ACME")
    assert full["graded"] == 6 and full["profile"]["trait"] == "fades_day" and len(full["recent"]) == 6
    assert full["kinds"][0]["key"] == "contract" and full["kinds"][0]["n"] == 6
    assert full["recent"][0]["h1"] == 2.0  # newest first, its way
    assert persona(eng, "GAMA")["profile"] is None and persona(eng, "GAMA")["graded"] == 3


def test_the_phones_stocks_and_the_clearest_characters(tmp_path):
    eng = semis_engine(tmp_path)
    characters(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    eng.desk.apply(DEV, "watch_add", {"symbol": "GAMA"})
    b = board(eng, DEV)
    assert [x["symbol"] for x in b["mine"]] == ["ACME"] and b["unknown"] == ["GAMA"]
    assert [x["symbol"] for x in b["runners"]] == ["BETA"]
    assert [x["symbol"] for x in b["faders"]] == ["DLTA"]  # ACME is already the phone's own


# --------------------------------------------------------------------------- the exit autopsy

WED = datetime(2026, 10, 14, 11, 0, tzinfo=ET).timestamp()


def at(day, hour=16, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET).timestamp()


def test_how_a_call_was_closed():
    long = {"direction": "long", "entry": 100.0, "stop": 98.0, "target": 104.0}
    assert exit_kind({**long, "exit": 97.9}) == "stop"
    assert exit_kind({**long, "exit": 104.2}) == "target"
    assert exit_kind({**long, "exit": 101.0}) == "manual"
    assert exit_kind({**long, "exit": 101.0, "lesson": "dead money"}) == "nudge"
    assert exit_kind({**long, "exit": 101.0, "lesson": "autopilot: out at the close"}) == "time"
    assert exit_kind({**long, "exit": 101.0, "lesson": "autopilot: stopped out"}) == "stop"
    short = {"direction": "short", "entry": 100.0, "stop": 102.0, "target": 96.0}
    assert exit_kind({**short, "exit": 102.1}) == "stop" and exit_kind({**short, "exit": 95.5}) == "target"


def test_each_exit_is_graded_as_the_closes_pass(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    p["closed"] = [{"id": "c1", "symbol": "ACME", "direction": "long", "entry": 98.0, "exit": 100.0, "ts": WED - 3600,
                    "closed": WED, "pnl_pct": 2.04}]  # fmt: skip
    q = eng.live_quotes
    q.update("ACME", 101.0, 100.0, at(14, 12), "tape")
    assert grade_exits(eng, at(14, 12, 1)) == 0  # no close yet
    q.update("ACME", 102.0, 100.0, at(14, 15, 59), "tape")
    assert grade_exits(eng, at(14, 16, 1)) == 1 and p["closed"][0]["after"] == {"close": 2.0}
    q.update("ACME", 103.0, 100.0, at(15, 15, 59), "tape")
    grade_exits(eng, at(15, 16, 2))
    q.update("ACME", 98.0, 100.0, at(21, 15, 59), "tape")  # five sessions on: Thu Fri Mon Tue Wed
    grade_exits(eng, at(21, 16, 3))
    assert p["closed"][0]["after"] == {"close": 2.0, "d1": 3.0, "d5": -2.0}
    assert grade_exits(eng, at(21, 16, 10)) == 0  # fully graded


def test_older_calls_are_graded_from_their_alert(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract")
    a.created = at(12, 10)  # Monday
    a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"close": 2.0, "d1": 4.0, "d5": 1.0, "sign": 1}}}
    c = {"symbol": "ACME", "direction": "long", "entry": 100.0, "exit": 101.0, "closed": at(12, 14)}
    assert from_alert(eng, c, a.to_dict()) == {"close": 0.99, "d1": 2.97, "d5": 0.0}
    # closed the next day: only its own close lines up with one of the alert's (the alert's d1)
    assert from_alert(eng, {**c, "closed": at(13, 11)}, a.to_dict()) == {"close": 2.97}
    assert from_alert(eng, c, None) == {}


def autopsy(eng):
    p = eng.desk.get(DEV)
    now = time.time()
    rows = []
    for i in range(6):  # winners taken early: the stock kept going
        rows.append({"id": f"w{i}", "symbol": "BETA", "direction": "long", "entry": 100.0, "exit": 102.0,
                     "closed": now - 86400 * (i + 7), "pnl_pct": 2.0,
                     "after": {"close": 0.5, "d1": 1.0, "d5": 2.0 + i * 0.1}})  # fmt: skip
    for i in range(5):  # losers stopped 2% out that came straight back
        rows.append({"id": f"l{i}", "symbol": "DLTA", "direction": "long", "entry": 100.0, "exit": 98.0, "stop": 98.0,
                     "closed": now - 86400 * (i + 7), "pnl_pct": -2.0, "lesson": "autopilot: stopped out",
                     "after": {"close": 1.0, "d1": 3.0, "d5": 4.0}})  # fmt: skip
    rows.append({"id": "s1", "symbol": "GAMA", "direction": "short", "entry": 20.0, "exit": 19.0, "closed": now - 86400,
                 "pnl_pct": 5.0, "after": {"close": -1.0}})  # fmt: skip
    rows.append(
        {
            "id": "x1",
            "symbol": "GAMA",
            "direction": "long",
            "entry": 20.0,
            "exit": 19.0,
            "closed": now - 86400 * 200,
            "pnl_pct": -5.0,
            "after": {"d1": 9.0},
        }
    )  # fmt: skip  (too old)
    p["closed"] = rows
    eng.heat_rates = {"*": {"dip80": 2.5, "winners": 20}}


def test_the_autopsy_reads_the_exits(tmp_path):
    eng = semis_engine(tmp_path)
    autopsy(eng)
    r = exit_report(eng, DEV)
    assert r["closed"] == 12 and r["graded"] == 12
    assert r["winners"]["d5"] == {"n": 6, "kept": 1.0, "median": 2.25}
    assert r["winners"]["close"]["n"] == 7  # with the short, whose stock fell 1% more: its way
    assert r["losers"]["d5"] == {"n": 5, "kept": 1.0, "median": 4.0}
    assert r["stops"] == {"n": 5, "back": 5, "distance": 2.0, "dip80": 2.5}
    assert r["edge"] == {"n": 11, "median": -1.0, "mean": -1.91}
    kinds = {k["kind"]: k for k in r["kinds"]}
    assert kinds["stop"]["n"] == 5 and kinds["manual"]["n"] == 7
    keys = [i["key"] for i in r["insights"]]
    assert keys == ["early", "bottom", "stops", "edge"]
    text = {i["key"]: i["text"] for i in r["insights"]}
    assert text["early"] == (
        "You sell winners early: after 100% of your winning exits the stock kept going your way, median +2.2% five sessions on (6)"
    )
    assert text["stops"] == (
        "Your stops sit inside the noise: 5 of 5 stopped calls were back at their entry by the next close; "
        "they sat 2.0% away, inside the 2.5% that 80% of winners here dipped before working"
    )
    assert text["edge"] == "Exit edge: -1.0% a call (median) against holding one more session (11)"
    short = next(x for x in r["rows"] if x["id"] == "s1")
    assert short["its"] == {"close": 1.0} and short["verdict"] == "left"  # the short's stock fell 1% more
    assert next(x for x in r["rows"] if x["id"] == "l0")["verdict"] == "left"


def test_good_exits_are_said_so(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    now = time.time()
    p["closed"] = [
        {"id": f"c{i}", "symbol": "ACME", "direction": "long", "entry": 100.0, "exit": 103.0 if i < 5 else 98.0,
         "closed": now - 86400 * (i + 7), "pnl_pct": 3.0 if i < 5 else -2.0, "after": {"d1": -1.5, "d5": -3.0}}
        for i in range(10)
    ]  # fmt: skip
    r = exit_report(eng, DEV)
    assert [i["key"] for i in r["insights"]] == ["timed", "cut", "edge"]
    assert r["insights"][-1]["tone"] == "good" and r["edge"]["median"] == 1.5
    assert exit_report(eng, "dev_nobody_0000")["graded"] == 0


async def test_run36_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    characters(eng)
    eng.refresh_base_rates()
    autopsy(eng)
    eng.desk.apply(DEV, "watch_add", {"symbol": "BETA"})
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/persona?symbol=acme"), headers=h) as r:
                assert (await r.json())["profile"]["trait"] == "fades_day"
            async with s.get(srv.make_url("/api/persona"), headers=h) as r:
                assert r.status == 400
            async with s.get(srv.make_url("/api/personas"), headers=h) as r:
                assert [x["symbol"] for x in (await r.json())["mine"]] == ["BETA"]
            async with s.get(srv.make_url("/api/exits"), headers=h) as r:
                assert (await r.json())["graded"] == 12
            async with s.get(srv.make_url("/api/exits"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()


def test_what_the_autopsy_finds_reaches_the_desk(tmp_path):
    from news247.actions import action_center
    from news247.evidence import evidence_lines, week_evidence

    eng = semis_engine(tmp_path)
    autopsy(eng)
    items = {i["key"].split(":")[1]: i for i in action_center(eng, DEV)["items"] if i["kind"] == "exits"}
    assert set(items) == {"stops", "early", "bottom"}
    assert items["stops"]["title"] == "Your stops sit inside the noise"
    assert items["stops"]["detail"].startswith("5 of 5 stopped calls were back at their entry")
    ev = week_evidence(eng, DEV)
    assert (
        ev["exits"]["graded"] == 12 and len(ev["exits"]["insights"]) == 2
    )  # 4 weeks: the 200-day-old call is out
    assert any(line.startswith("You sell winners early") for line in evidence_lines(ev))
    eng.desk.get(DEV)["closed"] = []
    assert not [i for i in action_center(eng, DEV)["items"] if i["kind"] == "exits"]
