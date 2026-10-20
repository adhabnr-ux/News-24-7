"""Run 29: confirmation pushes (an alert on your stock holding up an hour in, with 80%+ live odds,
graded from the flag price) and your week in evidence (picks against what you passed on, the odds
model's week, how calls ended, why misses missed)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from news247.catalysts import confirm_record
from news247.evidence import evidence_lines, week_evidence
from news247.personal import week_card, week_push

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run14 import market_open
from .test_run15 import graded
from .test_run28 import NOW, fresh, history

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- confirmation


async def test_news_that_is_holding_gets_one_push_and_a_grade(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    history(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    try:
        a_ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        a = fresh(eng)  # 90 minutes in, +1.5%: contract wins like this finished their way 90% (10)
        assert await eng.check_confirm(NOW) == 1
        msg = a_ph.read(server.requests[0]["body"])
        assert (
            msg["title"] == "✅ ACME's news is holding: +1.5% 1.5 hours in"
            and msg["open"] == f"/app/#a={a['id']}"
        )
        assert "finished its way 90% of the time (10)" in msg["body"]
        assert await eng.check_confirm(NOW + 60) == 0  # once
        log = json.loads(eng.storage.get_setting("confirm_log"))
        assert [(e["symbol"], e["ref"], e["p"]) for e in log] == [("ACME", 101.5, 0.9)]
        close = datetime(2026, 10, 14, 16, 1, tzinfo=ET).timestamp()
        eng.live_quotes.update("ACME", 103.0, 99.0, close, "tape")
        assert eng.grade_confirm(close) == 1
        rec = confirm_record(eng, close)
        assert rec["flags"] == 1 and rec["close"]["median"] == 1.48  # on from the flag price
    finally:
        await eng.http.close()


async def test_no_confirmation_when_the_odds_are_poor_or_the_phone_says_no(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    history(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    try:
        phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        fresh(eng, px=98.5)  # down 1.5%: alerts like this finished their way 20%
        assert await eng.check_confirm(NOW) == 0
        eng.storage.db.execute("DELETE FROM alerts")
        history(eng)
        fresh(eng)
        eng.desk.apply(DEV, "prefs", {"confirm": False})
        assert await eng.check_confirm(NOW) == 0
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- the week in evidence


def week(eng):
    now = time.time()
    ids = {}

    def add(title, r, odds=None):
        a = graded(title, r)
        a.created = now - 120
        if odds is not None:
            a.edge["odds"] = {"p": odds, "n": 100}
        eng.storage.add_alert(a)
        ids[title] = a.id
        return a.id

    picks = [add("Acme wins contract pick 0", 3.0, 0.7), add("Acme wins contract pick 1", 2.0, 0.7)]
    passed = [add(f"Morgan Stanley upgrades Acme {i}", r, 0.7) for i, r in enumerate([-1.0, 0.5, -0.5])]
    for i in range(2):
        add(f"Acme wins contract other {i}", 1.0)  # never reached this phone
    eng.webpush.log[DEV] = [{"id": x, "status": "sent", "ts": now} for x in picks + passed]
    eng.desk.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100, "alert_id": picks[0]}
    )
    eng.desk.apply(DEV, "save", {"id": picks[1]})
    eng.desk.get(DEV)["closed"] = [
        {"id": "c1", "symbol": "ACME", "direction": "long", "entry": 100, "exit": 103, "ts": now - 3600, "closed": now, "pnl_pct": 3.0, "lesson": "target reached"},
        {"id": "c2", "symbol": "BETA", "direction": "long", "entry": 50, "exit": 49.6, "ts": now - 3600, "closed": now, "pnl_pct": -0.8, "lesson": "cut on the live odds"},
    ]  # fmt: skip
    return now


def test_your_picks_against_what_you_passed_on(tmp_path):
    eng = semis_engine(tmp_path)
    now = week(eng)
    ev = week_evidence(eng, DEV, now)
    assert ev["picks"] == {"n": 2, "hit": 1.0, "avg": 2.5}
    assert ev["passed"] == {"n": 3, "hit": 0.33, "avg": -0.33} and ev["all"]["n"] == 7
    assert ev["model"] == {"n": 5, "said": 0.7, "went": 0.6}
    assert [(e["ending"], e["n"]) for e in ev["endings"]] == [
        ("target reached", 1),
        ("cut on the live odds", 1),
    ]
    assert [b["key"] for b in ev["best"]] == ["contract"] and [w["key"] for w in ev["worst"]] == ["upgrade"]
    assert ev["misses"]["n"] == 2 and ev["misses"]["causes"]["faded"] == 2
    assert evidence_lines(ev) == [
        "Your picks beat what you passed on: +2.5% vs -0.3% by the close (2 vs 3)",
        "The odds model said 70%; 60% went their way (5)",
        "Worked: contract win news +1.8% (4)",
    ]


def test_the_friday_card_carries_the_evidence(tmp_path):
    eng = semis_engine(tmp_path)
    now = week(eng)
    card = week_card(eng, DEV, now)
    assert card["evidence"]["picks"]["n"] == 2
    body = week_push(card)["body"]
    assert "Your picks beat what you passed on" in body and "The odds model said 70%" in body
    empty = week_evidence(eng, "dev_nobody_here1", now)
    assert empty["picks"] is None and empty["passed"] is None and evidence_lines({}) == []
    a = news("x")
    assert a.id
