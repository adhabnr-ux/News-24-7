"""The scorecard: every big move gets a verdict on whether Foretape had it before the price."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news247.edge import brief_push
from news247.market.universe import Universe
from news247.models import Alert, Analysis, NewsItem, PriceMove, Severity
from news247.scorecard import Scorecard, big_moves, fmt_span, format_scorecard, scorecard_line
from news247.storage import Storage

ET = ZoneInfo("America/New_York")
DAY = "2026-10-09"  # a Friday


def et(h: int, m: int = 0, day: int = 9) -> float:
    return datetime(2026, 10, day, h, m, tzinfo=ET).timestamp()


def news(st: Storage, title: str, at: float, score: float, tickers: list[str], source: str = "wire") -> None:
    sev = Severity.HIGH if score >= 55 else Severity.MEDIUM if score >= 35 else Severity.LOW
    item = NewsItem(source=source, title=title, url=f"https://x/{title[:20]}", detected=at)
    an = Analysis(score=score, severity=sev, tickers=tickers)
    st.add_item(item, an)
    if sev >= Severity.MEDIUM:  # the engine publishes MEDIUM and up
        st.add_alert(
            Alert(
                kind="news",
                severity=sev,
                title=title,
                body="",
                tickers=tickers,
                created=at,
                item=item,
                analysis=an,
            )
        )


def move(
    st: Storage, sym: str, at: float, pct: float, sev: Severity = Severity.HIGH, kind: str = "price"
) -> None:
    st.add_alert(
        Alert(
            kind=kind,
            severity=sev,
            title=f"▲ {sym} {pct:+.1f}%",
            body="",
            tickers=[sym],
            created=at,
            move=PriceMove(sym, pct, 86400, 10.0, 9.0, detected=at),
        )
    )


@pytest.fixture
def day() -> Storage:
    st = Storage(":memory:")
    # pushed: a HIGH 8-K 40 minutes before the radar
    news(st, "Acme Corp (ACME) files 8-K: 1.01 Material Agreement", et(9, 0), 62, ["ACME"], "sec-8k")
    move(st, "ACME", et(9, 40), 18.0)
    # app: shown at MEDIUM the night before, never pushed
    news(st, "Bolt Bio announces positive topline data", et(18, 30, day=8), 44, ["BOLT"], "globenewswire")
    move(st, "BOLT", et(7, 5), 35.0)
    # scored_low: the headline names the company (no ticker attached) and scored LOW
    news(st, "SpaceX to acquire spectrum that enables Starlink Mobile services", et(16, 30, day=8), 23, [])
    news(st, "Crown Castle, American Tower rise premarket after SpaceX spectrum deal", et(8, 10), 30, [])
    news(st, "Crown Castle stock jumping as SpaceX acquires 800 MHz spectrum", et(9, 50), 37, [])
    move(st, "CCI", et(9, 35), 9.2)
    # radar: the price first, a headline 25 minutes later
    move(st, "DRNZ", et(10, 0), 60.0, Severity.CRITICAL)
    news(
        st,
        "Dronez Systems (DRNZ) awarded $45 million Army contract",
        et(10, 25),
        70,
        ["DRNZ"],
        "businesswire",
    )
    # unexplained: nothing either side
    move(st, "ZZZZ", et(11, 0), -14.0)
    # not big moves: a MEDIUM radar alert, a second alert on ACME, a basket move, other days
    move(st, "MEH", et(12, 0), 6.0, Severity.MEDIUM)
    move(st, "ACME", et(13, 0), 30.0, Severity.CRITICAL)
    st.add_alert(
        Alert(
            kind="price",
            severity=Severity.HIGH,
            title="semis",
            body="",
            tickers=["NVDA", "AMD"],
            created=et(14, 0),
        )
    )
    move(st, "OLD", et(10, 0, day=8), 20.0)
    return st


def build(st: Storage) -> dict:
    uni = Universe.stub({"CCI": {"name": "Crown Castle Inc. Common Stock", "mcap": 34e9}})
    return Scorecard(st, uni).build(DAY, now=et(21, 0))


def test_every_big_move_gets_one_verdict(day):
    card = build(day)
    got = {v["symbol"]: v["verdict"] for v in card["verdicts"]}
    assert got == {
        "ACME": "pushed",
        "BOLT": "app",
        "CCI": "scored_low",
        "DRNZ": "radar",
        "ZZZZ": "unexplained",
    }
    assert card["moves"] == 5 and card["ahead"] == 2
    assert card["counts"] == {"pushed": 1, "app": 1, "scored_low": 1, "radar": 1, "unexplained": 1}
    assert card["ahead_rate"] == 0.4 and card["pushed_rate"] == 0.2
    # gaps come first: they are what to fix
    assert [v["verdict"] for v in card["verdicts"]] == ["scored_low", "app", "unexplained", "radar", "pushed"]


def test_lead_lag_and_hints(day):
    v = {v["symbol"]: v for v in build(day)["verdicts"]}
    assert v["ACME"]["lead_s"] == 40 * 60 and v["ACME"]["headline"]["source"] == "sec-8k"
    assert v["BOLT"]["lead_s"] == pytest.approx(et(7, 5) - et(18, 30, day=8))
    assert "MEDIUM" in v["BOLT"]["hint"]
    # the best of the LOW headlines read before the move, found by the company's name
    cci = v["CCI"]  # read 85 min early by name; the Reuters line never named Crown Castle
    assert cci["headline"]["title"].startswith("Crown Castle, American Tower rise")
    assert cci["headline"]["score"] == 30 and cci["lead_s"] == 85 * 60
    assert "Scorer gap" in cci["hint"] and "news247 score" in cci["hint"]
    assert v["DRNZ"]["lag_s"] == 25 * 60 and v["DRNZ"]["headline"]["source"] == "businesswire"
    assert "No headline" in v["ZZZZ"]["hint"]
    assert v["ZZZZ"]["pct"] == -14.0


def test_name_match_needs_the_universe(day):
    card = Scorecard(day).build(DAY, now=et(21, 0))  # no universe: names can't be matched
    got = {v["symbol"]: v["verdict"] for v in card["verdicts"]}
    assert got["CCI"] == "unexplained"


def test_big_moves_rules():
    alerts = [
        {"kind": "price", "severity": "HIGH", "tickers": ["A"], "created": 2},
        {"kind": "price", "severity": "CRITICAL", "tickers": ["A"], "created": 3},
        {"kind": "options", "severity": "HIGH", "tickers": ["B"], "created": 1},
        {"kind": "news", "severity": "HIGH", "tickers": ["C"], "created": 1},
        {"kind": "price", "severity": "MEDIUM", "tickers": ["D"], "created": 1},
        {"kind": "price", "severity": "HIGH", "tickers": ["E", "F"], "created": 1},
        {"kind": "price", "severity": "HIGH", "tickers": ["BTC-USD"], "created": 1},
        {"kind": "price", "severity": "HIGH", "tickers": ["ES=F"], "created": 1},
    ]
    assert [(a["tickers"][0], a["created"]) for a in big_moves(alerts)] == [("B", 1), ("A", 2)]


def test_text_outputs(day):
    card = build(day)
    line = scorecard_line(card)
    assert (
        line
        == "5 big moves · ahead on 2 (pushed 1, median 40 min early) · radar first on 1 · 2 scorer gaps · 1 unexplained"
    )
    text = format_scorecard(card)
    assert "Pushed before the move:   1/5" in text and "Radar first:              1/5" in text
    assert "[scored_low]" in text and "→ Scorer gap" in text
    empty = Scorecard(Storage(":memory:")).build(DAY, now=et(21, 0))
    assert empty["moves"] == 0 and scorecard_line(empty) == ""
    assert "No big moves" in format_scorecard(empty)
    assert fmt_span(30) == "30s" and fmt_span(2400) == "40 min" and fmt_span(3 * 3600) == "3.0 h"


def test_brief_carries_the_line_but_never_pings_alone():
    brief = {
        "overnight": [{"title": "Acme files 8-K", "direction": "up"}],
        "calendar": [],
        "count": 1,
        "scorecard": {"line": "5 big moves · ahead on 2"},
    }
    assert brief_push(brief)["body"].endswith("Last session: 5 big moves · ahead on 2")
    alone = {"overnight": [], "calendar": [], "count": 0, "scorecard": {"line": "5 big moves"}}
    assert brief_push(alone) is None


def test_engine_scorecard_brief_and_daily_save(cfg, day):
    from news247.engine import Engine

    eng = Engine(cfg, storage=day, sources=[])
    eng.scorecard.universe = Universe.stub({"CCI": {"name": "Crown Castle Inc. Common Stock", "mcap": 34e9}})
    monday = et(8, 15, day=12)
    last = eng.last_session_scorecard(monday)  # Monday's Brief reads Friday
    assert last["date"] == DAY and last["moves"] == 5
    assert [g["symbol"] for g in last["gaps"]] == ["CCI", "BOLT", "ZZZZ"]
    assert eng.save_scorecard(et(19, 0)) is None  # not before 20:00 ET
    saved = eng.save_scorecard(et(20, 5))
    assert saved["moves"] == 5 and saved["line"].startswith("5 big moves")
    assert eng.save_scorecard(et(20, 30)) is None  # once a day
    assert eng.scorecard_history(30)[0]["date"] == DAY


async def test_api_and_cli(cfg, day, tmp_path, capsys):
    from aiohttp.test_utils import TestClient, TestServer

    from news247.engine import Engine
    from news247.web.server import WebServer

    cfg.web.token = "s3cret"
    eng = Engine(cfg, storage=day, sources=[])
    web = WebServer(eng, cfg.web)
    async with TestClient(TestServer(web.app)) as client:
        assert (await client.get(f"/api/scorecard?date={DAY}")).status == 401
        r = await client.get(f"/api/scorecard?date={DAY}&token=s3cret")
        assert r.status == 200
        data = await r.json()
        assert data["moves"] == 5 and data["history"] == []
        assert (await client.get("/api/scorecard?date=nope&token=s3cret")).status == 400

    from news247.cli import main

    db = tmp_path / "card.db"
    st = Storage(db)
    move(st, "ZZZZ", et(11, 0), -14.0)
    st.close()
    assert main(["scorecard", "--db", str(db), "--date", DAY]) == 0
    out = capsys.readouterr().out
    assert "Scorecard for 2026-10-09" in out and "[unexplained]" in out


def test_replay_cci_oct_9_is_now_pushed_before_the_move():
    """The real CCI case through today's rules: Reuters' Oct 8 after-hours headline now scores
    HIGH+ and tags CCI, so Friday's +15.6% scores as 'pushed', ~17 hours ahead of the radar."""
    from news247.analysis.scorer import Scorer
    from news247.config import build_config

    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    uni = Universe.stub(
        {
            "CCI": {"name": "Crown Castle Inc. Common Stock", "mcap": 33.9e9, "price": 68.89},
            "AMT": {"name": "American Tower Corporation (REIT) Common Stock", "mcap": 84.9e9, "price": 166.7},
        }
    )
    s.attach_universe(uni, **cfg.smallcap.filters())
    st = Storage(":memory:")
    title = "SpaceX to acquire spectrum that enables Starlink Mobile services"
    item = NewsItem(source="gnews-reuters", title=title, url="https://x/reuters", detected=et(16, 40, day=8))
    an = s.score(item)
    assert an.severity >= Severity.HIGH and "CCI" in an.tickers
    st.add_item(item, an)
    st.add_alert(
        Alert(
            kind="news",
            severity=an.severity,
            title=title,
            body="",
            tickers=an.tickers,
            created=item.detected,
            item=item,
            analysis=an,
        )
    )
    move(st, "CCI", et(9, 35), 7.9)
    move(st, "AMT", et(10, 5), 9.3)
    card = Scorecard(st, uni).build(DAY, now=et(21, 0))
    got = {v["symbol"]: (v["verdict"], round(v["lead_s"] / 3600)) for v in card["verdicts"]}
    assert got == {"CCI": ("pushed", 17), "AMT": ("pushed", 17)}
    assert card["ahead_rate"] == 1.0
