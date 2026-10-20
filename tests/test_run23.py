"""Run 23: calibrated odds (a model fitted on the graded history, trusted only once it beats the
plain hit rate on alerts it never saw) and what your settings cost you (every alert a phone's
settings kept back, graded beside what reached it)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.actions import action_center
from news247.keptback import settings_cost
from news247.odds import MIN_TRAIN, features, fit_odds, odds_line, predict
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run15 import graded

DAY = 86400.0
ET = ZoneInfo("America/New_York")


def noon(days_ago: int = 0) -> float:
    d = datetime.now(ET).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET).timestamp()


def history(eng, n=160, flip_after=None):
    """Contract wins went their way 3 times in 4, analyst upgrades 1 in 4, one a day at noon ET.
    With ``flip_after``, the newest alerts flip: the record breaks."""
    for i in range(n):
        contract = i % 2 == 0
        good = (i // 2) % 4 != 0 if contract else (i // 2) % 4 == 0
        if flip_after is not None and i >= flip_after:
            good = not good
        title = f"Acme wins contract number {i}" if contract else f"Morgan Stanley upgrades Acme {i}"
        a = graded(title, 2.0 if good else -1.5)
        a.created = noon(n - i + 3)  # oldest first, all graded by now
        eng.storage.add_alert(a)


# --------------------------------------------------------------------------- calibrated odds


def test_features_are_what_was_known_when_it_went_out(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins $900M Navy contract").to_dict()
    a["edge"] = {"pre": {"ACME": 3.0}, "confluence": {"kinds": ["news", "options"]}}
    x = features(a, "risk-off", eng.universe)
    assert x["kind=news"] == 1 and x["sev"] == 0.5 and x["cat=contract"] == 1 and x["band=mid"] == 1
    assert x["dir=up"] == 1 and x["pre"] == 1.5 and x["regime=risk-off"] == 1 and x["conf"] == 1
    assert sum(1 for k in x if k.startswith("hour=")) == 1
    short = news("Acme downgraded to sell", direction="down").to_dict()
    short["edge"] = {"pre": {"ACME": 3.0}}  # up 3% before a short call: 3% against it
    y = features(short, None, eng.universe)
    assert y["pre"] == -1.5 and y["dir=down"] == 1 and not any(k.startswith("regime=") for k in y)
    assert features(news("Acme hosts investor day").to_dict(), None)["cat:none"] == 1


def test_no_model_until_there_is_enough_history(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng, n=MIN_TRAIN - 2)
    st = fit_odds(eng)
    assert (st["ready"], st["trusted"], st["n"], st["need"]) == (False, False, MIN_TRAIN - 2, MIN_TRAIN)
    assert predict(st, news("Acme wins contract").to_dict(), None) is None


def test_the_model_learns_the_record_and_proves_it_out_of_sample(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    st = fit_odds(eng)
    t = st["test"]
    assert st["ready"] and st["trusted"] and (t["train"], t["n"]) == (120, 40)
    assert t["brier"] < t["brier_guess"] and t["skill"] > 0.1 and t["auc"] > 0.7
    rel = t["reliability"]
    assert sum(r["n"] for r in rel) == 40 and all(r["lo"] < r["hi"] for r in rel)
    hi = max(rel, key=lambda r: r["predicted"])
    lo = min(rel, key=lambda r: r["predicted"])
    assert hi["actual"] > lo["actual"]  # what it rated higher did go its way more often
    labels = [f["label"] for f in st["factors"][:3]]
    assert "Contract win news" in labels or "Analyst upgrade news" in labels
    win = predict(st, eng.storage.get_alert(_store(eng, "Acme wins $900M Navy contract")), None)
    up = predict(st, eng.storage.get_alert(_store(eng, "Morgan Stanley upgrades Acme to buy")), None)
    assert 0.65 < win["p"] < 0.85 and 0.15 < up["p"] < 0.35 and win["n"] == 160
    assert (
        win["why"][0] == {"label": "Contract win news", "pts": win["why"][0]["pts"]}
        and win["why"][0]["pts"] > 0
    )
    assert odds_line(win) == f"Odds {round(win['p'] * 100)}% by the close (model, 160 graded)"
    assert odds_line(None) == ""
    assert (
        predict(st, news("Acme hosts investor day", direction="").to_dict(), None) is None
    )  # no call: no odds


def _store(eng, title):
    a = news(title)
    a.created = noon()
    eng.storage.add_alert(a)
    return a.id


def test_a_model_that_fails_its_test_is_not_used(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng, flip_after=120)  # the newest quarter does the opposite
    st = fit_odds(eng)
    assert st["ready"] and not st["trusted"] and st["test"]["skill"] < 0
    assert predict(st, news("Acme wins contract").to_dict(), None) is None


async def test_new_alerts_carry_their_odds_in_the_push(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    assert eng.odds["trusted"] and eng._odds_at > 0
    fitted = eng.odds["fitted"]
    eng.refresh_base_rates()
    assert eng.odds["fitted"] == fitted  # refitted hourly, not every 5 minutes
    a = news("Acme wins $900M Navy contract")
    a.created = noon()
    await eng.publish(a, push=False)
    o = eng.storage.get_alert(a.id)["edge"]["odds"]
    assert o["p"] > 0.65 and o["skill"] == eng.odds["test"]["skill"]
    body = eng.webpush.payload(a)["body"]
    assert f"Odds {round(o['p'] * 100)}% by the close (model, 160 graded)\nContract win, $2-10B:" in body


# --------------------------------------------------------------------------- what your settings cost you


def kinds(eng):
    """Graded HIGH alerts over the last two weeks: upgrades mostly won, contracts mostly lost, and
    plain headlines broke even."""
    now = time.time()
    rows = [("Morgan Stanley upgrades Acme {}", "upgrade", "Analyst upgrade", [3.0, 2.0, 4.0, 1.0, 2.5, 3.5, -1.0, 2.0]),
            ("Acme wins contract number {}", "contract", "Contract win", [-2.0, -1.0, 1.0, -3.0, -1.5, -2.5, 0.5, -1.0]),
            ("Acme headline {}", None, None, [1.0, -1.0, 2.0, -2.0, 1.5, -1.5, 0.5, -0.5, 1.0, -1.0])]  # fmt: skip
    for title, key, label, rets in rows:
        for i, r in enumerate(rets):
            a = graded(title.format(i), r)
            a.created = now - DAY * (1 + i)
            if key:
                a.edge["catalyst"] = {"key": key, "label": label, "band": "mid"}
            eng.storage.add_alert(a)
    m = graded("Acme medium note", 9.0)  # MEDIUM on a stock nobody watches: no phone gets it anyway
    m.severity = type(m.severity).MEDIUM
    m.created = now - DAY
    eng.storage.add_alert(m)


def test_settings_are_graded_on_what_they_kept_back(tmp_path):
    eng = semis_engine(tmp_path)
    kinds(eng)
    eng.desk.apply(DEV, "cat_mute", {"key": "upgrade"})
    eng.desk.apply(DEV, "cat_mute", {"key": "contract"})
    eng.desk.apply(DEV, "focus", {"minutes": 60})  # passing: not judged
    r = settings_cost(eng, DEV)
    assert r["came"] == {"n": 10, "hit": 0.5, "avg": 0.0, "big": 0}
    g = {x["code"] + ":" + x["setting"]: x for x in r["gates"]}
    up = g["catalyst_off:Analyst upgrade news turned off"]
    assert (up["n"], up["hit"], up["avg"], up["big"], up["verdict"]) == (8, 0.88, 2.12, 3, "costing")
    assert up["fix"] == {"label": "Push analyst upgrade news again", "action": "cat_mute",
                         "body": {"key": "upgrade", "on": False}}  # fmt: skip
    assert [e["ret"] for e in up["examples"]] == [4.0, 3.5, 3.0]
    ct = g["catalyst_off:Contract win news turned off"]
    assert (ct["verdict"], ct["hit"]) == ("saving", 0.25)
    assert r["gates"][0]["verdict"] == "costing" and r["costing"] == 1
    assert [m["ret"] for m in r["misses"]] == [4.0, 3.5, 3.0] and r["misses"][0][
        "setting"
    ] == "Analyst upgrade news turned off"
    assert r["kept"] == {"n": 16, "big": 3}
    # a severity floor over everything: nothing came through, so it's judged against a coin flip
    eng.desk.apply(DEV2, "prefs", {"min_sev": "CRITICAL"})
    r2 = settings_cost(eng, DEV2)
    assert (
        r2["came"]["n"] == 0 and len(r2["gates"]) == 1 and r2["gates"][0]["setting"] == "Your severity floor"
    )
    assert r2["gates"][0]["n"] == 26 and r2["gates"][0]["verdict"] == "even"


def test_a_costly_setting_shows_up_in_needs_you(tmp_path):
    eng = semis_engine(tmp_path)
    kinds(eng)
    eng.desk.apply(DEV, "cat_mute", {"key": "upgrade"})
    r = action_center(eng, DEV)
    item = next(i for i in r["items"] if i["kind"] == "settings")
    assert item["title"] == "Analyst upgrade news turned off kept back 8 alerts that beat what reached you"
    assert (
        item["detail"]
        == "88% went their way by the close vs 39% of what reached you · average +2.1% · 8 kept back"
    )
    eng.desk.apply(DEV, item["do"]["action"], item["do"]["body"])  # one tap: upgrades push again
    assert eng.desk.get(DEV)["cat_off"] == []
    assert not any(i["kind"] == "settings" for i in action_center(eng, DEV)["items"])


async def test_run23_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/odds"), headers=h) as r:
                body = await r.json()
                assert body["trusted"] and "model" not in body and body["test"]["n"] == 40
            async with s.get(srv.make_url("/api/record"), headers=h) as r:
                assert (await r.json())["odds"]["trusted"] is True
            async with s.get(srv.make_url("/api/settings-cost?days=7"), headers=h) as r:
                assert (await r.json())["days"] == 7
            async with s.get(
                srv.make_url("/api/settings-cost"), headers={"Authorization": "Bearer tok"}
            ) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
