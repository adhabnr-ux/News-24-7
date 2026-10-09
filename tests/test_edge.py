"""The edge desk: the play, precedents, lead over mainstream, the tape since, the Brief and
the catalyst calendar - and how they reach the phone and the app."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.analysis.scorer import Scorer
from news247.config import build_config
from news247.edge import ET, Calendar, EdgeDesk, brief_push, build_brief, conviction
from news247.models import Alert, NewsItem, Severity, SourceTier
from news247.notify.channels import WebPushNotifier
from news247.storage import Storage
from news247.web.server import WebServer

from .test_engine import make_engine, news
from .test_webpush import Phone


def et(y: int, mo: int, d: int, h: int = 12, mi: int = 0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()


@pytest.fixture(scope="module")
def desk() -> EdgeDesk:
    cfg = build_config({})
    d = EdgeDesk(Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols))
    assert len(d.precedents) >= 150 and d.ready
    return d


def annotated(desk: EdgeDesk, title: str, tier=SourceTier.PRIMARY, source: str = "x", **extra: Any) -> Alert:
    item = NewsItem(source=source, title=title, tier=tier, published=time.time() - 3, extra=extra)
    an = desk.scorer.score(item)
    a = Alert(
        kind="news", severity=an.severity, title=title, body="", item=item, analysis=an, tickers=an.tickers
    )
    desk.annotate(a)
    return a


# --------------------------------------------------------------------------- the play


def test_conviction_scale():
    assert conviction(92) == ("Maximum", 5)
    assert conviction(76) == ("High", 4)
    assert conviction(58)[0] == "Elevated" and conviction(40)[0] == "Watch" and conviction(5) == ("Low", 1)


def test_fhfa_post_recalls_both_fico_crashes(desk):
    a = annotated(
        desk,
        "@pulte: Fannie Mae and Freddie Mac will accept VantageScore 10T on all loans, effective immediately",
        entities=["FHFA"],
    )
    play, precs = a.edge["play"], a.edge["precedents"]
    assert play["direct"][0] == "FICO" and play["conviction"] in ("High", "Maximum")
    assert {p["date"] for p in precs[:2]} == {"2026-09-28", "2026-09-04"}
    assert all("FICO" in p["move"] for p in precs[:2])
    # the headline itself has no direction word: history says FICO fell both times
    assert play["direction"] == "down" and play["direction_basis"] == "precedents"
    assert EdgeDesk.push_line(a) == "Last time (Sep 28, 2026): FICO -25 to -27%"
    assert a.edge["first_source"] == "x" and a.edge["caught_after_s"] >= 0


def test_direct_names_vs_read_through(desk):
    a = annotated(
        desk, "OpenAI told investors annualized revenue is $45 billion, below targets - FT", SourceTier.MEDIA
    )
    play = a.edge["play"]
    assert play["direction"] == "down" and "direction_basis" not in play  # "below" says it
    assert {"NVDA", "ORCL"} <= set(play["direct"] + play["read_through"])
    assert not set(play["direct"]) & set(play["read_through"])
    assert any(p["date"] == "2026-10-08" for p in a.edge["precedents"])


def test_macro_and_prediction_market_directions(desk):
    fed = annotated(desk, "Fed raises rates by 50 basis points in surprise move", SourceTier.MEDIA)
    assert fed.edge["play"]["direct"][:2] == ["SPY", "QQQ"] and fed.edge["play"]["direction"] == "down"
    pm = annotated(
        desk,
        "Polymarket: 'US strikes Iran by October 31?' jumps 24 pts to 61% in 4 min ($3.1M traded in 24h)",
        SourceTier.SOCIAL,
        source="polymarket",
    )
    # "jumps" is about the odds, not stocks: the call comes from how Iran escalations traded
    assert pm.edge["play"]["direction"] == "down" and pm.edge["play"]["direction_basis"] == "precedents"


def test_precedent_direction_reads_market_words():
    precs = [
        {"move": "Brent +4.6%; S&P -0.7%; 10y 4.79%", "similarity": 8},
        {"move": "Oil -10%+ on the ceasefire announcement", "similarity": 8},
        {"move": "Oil +5-6.5%; Dow -577; airlines hit", "similarity": 8},
    ]
    assert EdgeDesk.precedent_direction(precs, ["SPY", "USO"]) == "down"
    assert EdgeDesk.precedent_direction([{"move": "WDAY +18% close", "similarity": 5}], ["WDAY"]) == "up"
    assert EdgeDesk.precedent_direction([{"move": "no numbers here"}], ["X"]) == ""


def test_no_precedents_before_history_is_scored():
    cfg = build_config({})
    fresh = EdgeDesk(Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols))
    assert not fresh.ready
    item = NewsItem(source="x", title="FED RAISES RATES BY 50 BASIS POINTS", tier=SourceTier.MEDIA)
    an = fresh.scorer.score(item)
    a = Alert(kind="news", severity=an.severity, title=item.title, body="", item=item, analysis=an)
    t0 = time.perf_counter()
    fresh.annotate(a)
    assert time.perf_counter() - t0 < 0.05 and a.edge["precedents"] == []  # never blocks an alert
    assert a.edge["play"]["direct"]


# --------------------------------------------------------------------------- the tape since


def test_reference_prices_and_since(desk):
    desk.tracked.clear()
    now = time.time()
    a = annotated(desk, "Exclusive-Silver Lake in talks to buy Workday, sources say", SourceTier.MEDIA)
    assert a.edge["play"]["direct"] == ["WDAY"] and a.edge["refs"] == {}  # not quoted yet
    a2 = Alert(kind="news", severity=Severity.HIGH, title="t", body="", item=a.item, analysis=a.analysis)
    desk.annotate(a2, {"WDAY": (now - 30, 236.0)})
    assert a2.edge["refs"] == {"WDAY": 236.0}  # fresh quote at alert time
    updates = desk.fill_refs(lambda sym, ts, tol: 235.0 if sym == "WDAY" else None, now)
    assert updates[a.id] == {"WDAY": 235.0} and a2.id not in updates
    assert desk.fill_refs(lambda *_: 1.0, now) == {}  # already filled: nothing new
    assert EdgeDesk.since({"WDAY": 235.0}, {"WDAY": (now, 270.25)}) == {"WDAY": 15.0}
    desk.fill_refs(lambda *_: None, now + 7 * 3600)
    assert a.id not in desk.tracked  # forgotten after 6 hours


# --------------------------------------------------------------------------- calendar


def test_calendar_upcoming_and_generated_dates():
    cal = Calendar()
    ev = cal.upcoming(days=45, now=et(2026, 10, 9, 9))
    titles = [(e["date"], e["title"]) for e in ev]
    assert ("2026-10-16", "Monthly options expiration") in titles
    assert ("2026-10-28", "FOMC decision") in titles
    assert any(d == "2026-11-03" and "midterm" in t for d, t in titles)
    assert [e["date"] for e in ev] == sorted(e["date"] for e in ev)
    fomc = next(e for e in ev if e["title"] == "FOMC decision")
    assert fomc["in_days"] == 19 and fomc["impact"] == 3 and fomc["ts"] == et(2026, 10, 28, 14)
    dec = cal.upcoming(days=80, now=et(2026, 10, 9))
    assert ("2026-12-18", "Quad witching (quarterly options + futures expiry)") in [
        (e["date"], e["title"]) for e in dec
    ]
    assert ("2026-11-26", "US markets closed") in [(e["date"], e["title"]) for e in dec]


@pytest.mark.parametrize(
    "when,phase,opn",
    [
        ((2026, 10, 9, 10, 0), "open", True),
        ((2026, 10, 9, 8, 0), "pre-market", False),
        ((2026, 10, 9, 17, 30), "after-hours", False),
        ((2026, 10, 10, 12, 0), "closed", False),  # Saturday
        ((2026, 11, 26, 11, 0), "closed", False),  # Thanksgiving
        ((2026, 11, 27, 13, 30), "after-hours", False),  # early close at 13:00
    ],
)
def test_market_status(when, phase, opn):
    st = Calendar().market_status(et(*when))
    assert st["phase"] == phase and st["open"] is opn and st["until_s"] > 0


def test_until_open_skips_weekend_and_holiday():
    st = Calendar().market_status(et(2026, 11, 25, 17, 0))  # Wed evening before Thanksgiving
    assert st["until_s"] == et(2026, 11, 27, 9, 30) - et(2026, 11, 25, 17, 0)
    assert Calendar.last_close(et(2026, 10, 12, 8)) == et(
        2026, 10, 9, 16
    )  # Monday pre-market -> Friday close


# --------------------------------------------------------------------------- the Brief


def brief_alert(
    aid: str, title: str, created: float, score: float, direction: str = "down"
) -> dict[str, Any]:
    return {
        "id": aid,
        "kind": "news",
        "title": title,
        "severity": "CRITICAL" if score >= 75 else "HIGH",
        "created": created,
        "tickers": ["NVDA"],
        "analysis": {"score": score, "themes": ["ai_lab_financials"], "direction": direction},
        "item": {"source": "ft-technology"},
        "edge": {"play": {"direction": direction, "direct": ["NVDA", "ORCL"]}},
    }


def test_brief_overnight_market_calendar_edge():
    now = et(2026, 10, 28, 8, 15)
    alerts = [
        brief_alert("a", "OpenAI revenue below targets - FT", now - 3600, 80),
        brief_alert("b", "Older story from yesterday morning", et(2026, 10, 27, 10), 90),
        brief_alert("c", "Kimi K4 released", now - 7200, 60, "down"),
    ]
    snap = {
        "SPY": {"chg_day": -0.4, "price": 660},
        "NVDA": {"chg_day": -3.1, "price": 180},
        "WDAY": {"chg_day": 1.0, "price": 270},
    }
    b = build_brief(alerts, Calendar(), snap, [400.0, 1100.0, 60.0], now)
    assert [a["id"] for a in b["overnight"]] == ["a", "c"] and b["count"] == 2  # since yesterday's close
    assert b["overnight"][0]["tickers"] == ["NVDA", "ORCL"] and b["themes"] == ["ai lab financials"]
    assert b["indexes"] == {"SPY": -0.4} and b["movers"][0]["symbol"] == "NVDA"
    assert b["market"]["phase"] == "pre-market" and b["greeting"] == "Good morning"
    assert b["date"] == "Wednesday, October 28"
    assert b["calendar"][0]["title"] == "FOMC decision" and b["calendar"][0]["in_days"] == 0
    assert b["edge"] == {"stories": 3, "median_lead_s": 400.0, "best_lead_s": 1100.0}
    assert b["overnight"][0]["smallcap"] is None
    small = brief_alert("d", "Acme Biotech Announces FDA Approval", now - 600, 85)
    small["edge"]["smallcap"] = {
        "symbol": "ACMB",
        "cap": "$180M",
        "band": "micro cap",
        "label": "FDA approval",
        "move_text": "+21–55% typical",
        "direction": "up",
        "material": True,
        "points": 24,
    }
    sc = build_brief([small], Calendar(), {}, [], now)["overnight"][0]["smallcap"]
    assert sc == {
        "symbol": "ACMB",
        "cap": "$180M",
        "band": "micro cap",
        "label": "FDA approval",
        "move_text": "+21–55% typical",
        "direction": "up",
    }
    push = brief_push(b)
    assert push["title"] == "☀️ The Brief · 2 overnight catalysts" and push["id"] == "brief"
    assert push["body"].splitlines() == [
        "▼ OpenAI revenue below targets - FT",
        "▼ Kimi K4 released",
        "Today 14:00 ET: FOMC decision",
    ]
    quiet = build_brief([], Calendar(), {}, [], et(2026, 10, 14, 8, 15))
    assert brief_push(quiet) is None  # nothing overnight and nothing big today: no ping
    assert build_brief([], Calendar(), {}, [], et(2026, 10, 14, 1, 30))["greeting"] == "Good evening"
    assert build_brief([], Calendar(), {}, [], et(2026, 10, 14, 13))["greeting"] == "Good afternoon"


# --------------------------------------------------------------------------- engine + push + API


async def test_engine_annotates_measures_lead_and_tracks(cfg, server):
    eng, cap = make_engine(cfg)
    eng.edge.warm()  # the engine does this in a thread at start-up
    events = eng.subscribe()
    title = "OpenAI told investors annualized revenue is $45 billion, below targets"
    await eng.on_item(news(title, "ft-technology", SourceTier.MEDIA, ()))
    await eng.drain()
    alert = cap.alerts[0]
    assert alert.edge["play"]["direct"] and alert.edge["precedents"]
    stored = eng.storage.get_alert(alert.id)
    assert stored["edge"]["play"] == alert.edge["play"]
    # 7 minutes later CNBC writes it up: that's Foretape's lead
    later = news(title + " - CNBC", "cnbc-tech", SourceTier.MEDIA, (), extra={})
    later.detected = alert.created + 420
    await eng.on_item(later)
    lead = eng.storage.get_alert(alert.id)["edge"]["lead"]
    assert lead["source"] == "cnbc-tech" and lead["lead_s"] == pytest.approx(420, abs=1)
    assert eng.storage.leads() == [pytest.approx(420, abs=1)]
    msgs = []
    while not events.empty():
        msgs.append(events.get_nowait())
    assert any(m["event"] == "edge" and m["data"]["id"] == alert.id for m in msgs)
    # a second mainstream outlet doesn't overwrite the first
    again = news(title + " - Yahoo", "yahoo-watchlist", SourceTier.MEDIA, ())
    again.detected = alert.created + 900
    await eng.on_item(again)
    assert eng.storage.get_alert(alert.id)["edge"]["lead"]["source"] == "cnbc-tech"


def test_track_symbols_only_with_yahoo(cfg):
    eng, _ = make_engine(cfg)
    eng.prices = object()  # type: ignore[assignment]
    eng.cfg.market.provider = "yahoo"
    before = len(eng.cfg.market.symbols)
    eng._track_symbols(["FICO", "NVDA", "bad ticker", "RKT"])
    added = eng.cfg.market.symbols[before:]
    assert "FICO" in added and "RKT" in added and "bad ticker" not in added
    assert eng.cfg.market.symbols.count("NVDA") == 1


def push_engine_with_phone(tmp_path, server) -> tuple[Any, Phone]:
    c = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {"console": {"enabled": False}, "webpush": {"enabled": True, "brief_time": "08:15"}},
            "web": {"token": "tok"},
        }
    )
    from news247.engine import Engine

    eng = Engine(c, storage=Storage(tmp_path / "e.db"), sources=[])
    server.on("/push/me", (201, ""))
    phone = Phone(server.url("/push/me"))
    eng.webpush.subs[phone.endpoint] = phone.subscription()  # type: ignore[union-attr]
    return eng, phone


async def test_morning_brief_is_scheduled_and_pushed_once(tmp_path, server):
    eng, phone = push_engine_with_phone(tmp_path, server)
    await eng.http.start()
    try:
        assert not eng._brief_due(et(2026, 10, 28, 8, 0))  # too early
        assert eng._brief_due(et(2026, 10, 28, 8, 20))
        assert not eng._brief_due(et(2026, 10, 31, 8, 20))  # Saturday
        assert not eng._brief_due(et(2026, 11, 26, 8, 20))  # Thanksgiving
        assert not eng._brief_due(et(2026, 10, 28, 11, 0))  # missed by hours: skip, don't nag
        assert await eng.send_brief(et(2026, 10, 28, 8, 20)) is True  # FOMC day: worth a ping
        shown = phone.read(server.requests[-1]["body"])
        assert shown["title"].startswith("☀️ The Brief") and "FOMC decision" in shown["body"]
        assert server.requests[-1]["headers"]["Urgency"] == "normal"
        assert not eng._brief_due(et(2026, 10, 28, 8, 25))  # once per day
        n = len(server.requests)
        assert (
            await eng.send_brief(et(2026, 10, 14, 8, 20)) is False and len(server.requests) == n
        )  # quiet day
    finally:
        await eng.http.close()


async def test_push_carries_the_precedent(tmp_path, http, server):
    cfg = build_config({})
    desk = EdgeDesk(Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols))
    desk.warm()
    a = annotated(
        desk,
        "@pulte: FHFA moves Fannie and Freddie to VantageScore only, effective immediately",
        entities=["FHFA"],
    )
    server.on("/push/p", (201, ""))
    ch = WebPushNotifier({}, http, Severity.HIGH)
    ch.attach(Storage(tmp_path / "w.db"), secret="tok")
    phone = Phone(server.url("/push/p"))
    ch.subs[phone.endpoint] = phone.subscription()
    await ch.send(a)
    shown = phone.read(server.requests[-1]["body"])
    assert shown["body"].endswith("Last time (Sep 28, 2026): FICO -25 to -27%")
    assert shown["direction"] == "down" and shown["conviction"]


async def test_app_apis(tmp_path, server):
    eng, _ = push_engine_with_phone(tmp_path, server)
    eng.edge.warm()
    await eng.http.start()
    now = time.time()
    eng.detector.set_prev_close("NVDA", 190.0)
    eng.detector.update("NVDA", 185.0, now - 30)
    title = "OpenAI told investors annualized revenue is $45 billion, below targets"
    await eng.on_item(news(title, "ft-technology", SourceTier.MEDIA, ()))
    await eng.drain()
    aid = eng.storage.recent_alerts(1)[0]["id"]
    eng.detector.update("NVDA", 181.3, now)
    later = news(title + " - CNBC", "cnbc-tech", SourceTier.MEDIA, ())
    later.detected = time.time() + 300
    await eng.on_item(later)
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    auth = {"Authorization": "Bearer tok"}
    try:
        async with aiohttp.ClientSession(headers=auth) as s:
            async with s.get(srv.make_url("/api/app")) as r:
                app = await r.json()
            a = next(x for x in app["alerts"] if x["id"] == aid)
            assert a["since"]["NVDA"] == pytest.approx(-2.0, abs=0.01)
            assert a["edge"]["lead"]["source"] == "cnbc-tech"
            assert app["edge"]["stories"] == 1 and app["market"]["phase"] and app["next"]
            async with s.get(srv.make_url(f"/api/alert/{aid}")) as r:
                detail = await r.json()
            assert [x["source"] for x in detail["story_sources"]] == ["ft-technology", "cnbc-tech"]
            assert detail["edge"]["precedents"] and detail["since"]["NVDA"] < 0
            async with s.get(srv.make_url("/api/alert/nope")) as r:
                assert r.status == 404
            async with s.get(srv.make_url("/api/brief")) as r:
                brief = await r.json()
            assert {"overnight", "calendar", "market", "edge", "indexes", "movers"} <= set(brief)
            async with s.get(srv.make_url("/api/calendar?days=90")) as r:
                cal = await r.json()
            assert cal["events"] and all({"date", "title", "in_days"} <= set(e) for e in cal["events"])
            async with s.get(srv.make_url("/api/watch")) as r:
                watch = await r.json()
            assert watch["symbols"][0]["symbol"] == "NVDA" and watch["symbols"][0]["chg_day"] < 0
        async with aiohttp.ClientSession() as s, s.get(srv.make_url("/api/brief")) as r:
            assert r.status == 401
    finally:
        await srv.close()
        await eng.http.close()


def test_alert_edge_round_trips_through_storage(tmp_path):
    st = Storage(tmp_path / "s.db")
    a = Alert(kind="news", severity=Severity.HIGH, title="t", body="", edge={"play": {"direct": ["X"]}})
    st.add_alert(a)
    st.update_alert_edge(a.id, {"refs": {"X": 1.5}})
    assert st.get_alert(a.id)["edge"] == {"play": {"direct": ["X"]}, "refs": {"X": 1.5}}
    assert st.update_alert_edge("missing", {"x": 1}) is None
    assert st.add_lead(a.id, "cnbc-top", 61.0) and not st.add_lead(a.id, "yahoo", 99.0)
    assert st.leads() == [61.0]
    assert json.loads(json.dumps(a.to_dict()))["edge"]["play"]["direct"] == ["X"]
