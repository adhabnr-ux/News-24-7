"""Regression for Oct 9 2026: MRNA +14% (Nasdaq-100 entry at the open + an NYT report of an NIH
cancer-vaccine push) produced no push. Every link of that chain must now fire."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news247.analysis.scorer import Scorer
from news247.config import build_config
from news247.edge import brief_push, build_brief
from news247.engine import Engine
from news247.market import radar as radar_mod
from news247.market.radar import MoversRadar, Quote, SmallCapFeed
from news247.market.universe import Universe
from news247.models import NewsItem, Severity
from news247.models import SourceTier as T
from news247.storage import Storage

from .conftest import CaptureNotifier

ET = ZoneInfo("America/New_York")
UNI = {
    "MRNA": {
        "name": "Moderna, Inc.",
        "mcap": 75e9,
        "price": 197.0,
        "industry": "Biotechnology: Biological Products",
    },
    "BNTX": {"name": "BioNTech SE", "mcap": 30e9, "price": 120.0},
    "NVAX": {"name": "Novavax, Inc.", "mcap": 1.5e9, "price": 9.0},
    "NDAQ": {"name": "Nasdaq, Inc.", "mcap": 50e9, "price": 90.0},
    "AAPL": {"name": "Apple Inc.", "mcap": 3.5e12, "price": 250.0},
    "MIDC": {"name": "Midcorp Industries Inc.", "mcap": 5e9, "price": 40.0},
}


def et(*a: int) -> float:
    return datetime(*a, tzinfo=ET).timestamp()


def sc_cfg(**kw):
    return build_config({"smallcap": kw}).smallcap


def engine(tmp_path: Path) -> tuple[Engine, CaptureNotifier]:
    from news247.notify import Dispatcher

    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {"console": {"enabled": False}},
        }
    )
    Universe.stub(UNI).save(tmp_path / "universe.json.gz")
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    cap = CaptureNotifier()
    disp.channels = [cap]
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), dispatcher=disp, sources=[])
    eng.coalesce_s = 0
    eng.radar = MoversRadar(cfg.smallcap, eng.universe)
    return eng, cap


# --------------------------------------------------------------------------- the move itself


@pytest.mark.parametrize(
    "sym,cap,pct,want",
    [
        ("MRNA", 75e9, 9.22, "HIGH"),  # Oct 9 pre-market: a $75B company +9% is a major event
        ("MRNA", 75e9, 6.5, "MEDIUM"),  # on the board, in the app
        ("MRNA", 75e9, 4.0, None),  # an ordinary day for a volatile large cap
        ("AAPL", 3.5e12, 4.5, "MEDIUM"),  # mega caps: 4% is already news
        ("MIDC", 5e9, 16.0, "HIGH"),  # mid caps: 10% to flag, 15% to push
        ("MIDC", 5e9, 8.0, None),
    ],
)
def test_radar_thresholds_scale_with_size(sym, cap, pct, want):
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    quote = Quote(
        sym,
        price=100 * (1 + pct / 100),
        prev_close=100.0,
        change_pct=pct,
        market_cap=cap,
        session="pre",
        source="sweep",
    )
    hits = r.scan([quote], et(2026, 10, 9, 8, 5), provider="sweep")
    assert (hits[0].severity if hits else None) == want


def test_big_caps_need_no_volume_figure():
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    # spark gives no volume; a $75B company is liquid by definition
    hits = r.scan(
        [Quote("MRNA", price=215.2, prev_close=197.0, change_pct=9.24, market_cap=75e9, source="sweep")],
        et(2026, 10, 9, 8, 5),
        provider="sweep",
    )
    assert hits and hits[0].severity == "HIGH"


async def test_sweep_reads_premarket_prices_for_the_largest(server, http, monkeypatch):
    def spark(request):
        from aiohttp import web

        syms = request.query["symbols"].split(",")
        assert request.query["includePrePost"] == "true"
        data = {
            s: {
                "timestamp": [1, 2],
                "close": [200.0, 215.2 if s == "MRNA" else 101.0],
                "chartPreviousClose": 197.0 if s == "MRNA" else 100.0,
            }
            for s in syms
        }
        return web.json_response(data)

    server.on("/v8/finance/spark", spark)
    monkeypatch.setattr(radar_mod, "SPARK_URL", server.url("/v8/finance/spark"))
    uni = Universe.stub(UNI)
    feed = SmallCapFeed(sc_cfg(sweep_size=3), http, uni, None, MoversRadar(sc_cfg(), uni))
    quotes = await feed.sweep_quotes("pre-market")
    assert [q.symbol for q in quotes] == ["AAPL", "MRNA", "NDAQ"]  # the 3 largest, NVAX ($1.5B) not swept
    mrna = next(q for q in quotes if q.symbol == "MRNA")
    assert (
        mrna.change_pct == pytest.approx(9.24, abs=0.01) and mrna.session == "pre" and mrna.market_cap == 75e9
    )


# --------------------------------------------------------------------------- the calendar: index entry


async def test_index_entry_goes_on_the_calendar_and_into_the_brief(tmp_path: Path):
    eng, _ = engine(tmp_path)
    item = NewsItem(
        source="globenewswire",
        title="Nasdaq Announces Moderna to Join Nasdaq-100 Index, Replacing Warner Bros. Discovery",
        summary="Moderna, Inc. (Nasdaq: MRNA) will become a component of the Nasdaq-100 Index prior to market open "
        f"on Friday, October 9, {datetime.now(ET).year + 1}.",
        tier=T.WIRE,
        url="https://example.com/ndx",
        published=time.time() - 5,
    )
    assert eng._subject_symbol(item) == "MRNA"  # the subject, not the announcer (NDAQ)
    await eng.on_item(item)
    await eng.drain(2)
    ev = next(e for e in eng.calendar.dynamic.values() if e["event"] == "index")
    assert ev["title"] == "MRNA joins the Nasdaq-100 (effective at the open)" and ev["time"] == "09:30"
    assert ev["impact"] == 2 and "large cap" in ev["note"]
    y = datetime.now(ET).year + 1
    b = build_brief([], eng.calendar, {}, [], et(y, 10, 8, 8, 15))
    assert "Tomorrow 09:30 ET: MRNA joins the Nasdaq-100 (effective at the open)" in brief_push(b)["body"]
    b = build_brief([], eng.calendar, {}, [], et(y, 10, 9, 8, 15))
    assert "Today 09:30 ET: MRNA joins the Nasdaq-100 (effective at the open)" in brief_push(b)["body"]


async def test_radar_alert_cites_todays_calendar(tmp_path: Path):
    eng, cap = engine(tmp_path)
    now = time.time()
    day = datetime.fromtimestamp(now, ET).date().isoformat()
    eng.calendar.add(
        {
            "id": f"MRNA:{day}:index",
            "date": day,
            "time": "09:30",
            "kind": "binary",
            "event": "index",
            "symbol": "MRNA",
            "title": "MRNA joins the Nasdaq-100 (effective at the open)",
            "impact": 2,
        }
    )
    hits = eng.radar.scan(
        [
            Quote(
                "MRNA",
                price=215.2,
                prev_close=197.0,
                change_pct=9.24,
                market_cap=75e9,
                session="pre",
                source="sweep",
            )
        ],
        now,
        provider="sweep",
    )
    await eng.on_radar(hits)
    await eng.drain(2)
    (a,) = cap.alerts
    assert a.severity is Severity.HIGH and a.title.startswith(
        "▲ MRNA +9.24% pre-market · Moderna, Inc. · $75B large cap"
    )
    assert "On the calendar today: MRNA joins the Nasdaq-100" in a.body and "No headline yet" not in a.body
    assert a.edge["smallcap"]["label"] == "Radar: on the calendar today"


# --------------------------------------------------------------------------- the news: health policy


def scorer(with_universe: bool = True) -> Scorer:
    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    if with_universe:
        s.attach_universe(Universe.stub(UNI), **cfg.smallcap.filters())
    return s


def test_nyt_report_of_nih_cancer_vaccine_push_is_pushed():
    cfg = build_config({})
    src = next(x for x in cfg.sources if x["name"] == "nyt-health")
    a = scorer().score(
        NewsItem(
            source="nyt-health",
            title="N.I.H. Plans National Push to Speed Development of Cancer Vaccines",
            tier=T.MEDIA,
            extra={"boost": src["boost"]},
        )
    )
    assert a.severity >= Severity.HIGH and a.direction == "up" and "vaccine_policy_push" in a.themes
    assert a.tickers[:3] == ["MRNA", "BNTX", "NVAX"]


def test_vaccine_pullback_from_the_agency_is_pushed():
    a = scorer().score(
        NewsItem(
            source="hhs",
            title="HHS cancels $500 million in mRNA vaccine projects under BARDA",
            tier=T.PRIMARY,
        )
    )
    assert a.severity >= Severity.HIGH and a.direction == "down"


@pytest.mark.parametrize(
    "title,tier",
    [
        ("NIH funds study of vaccine hesitancy in rural communities", T.MEDIA),
        ("NIH launches new website for vaccine trial volunteers", T.PRIMARY),
        ("CDC updates guidance on flu vaccines for older adults", T.MEDIA),
    ],
)
def test_routine_health_agency_news_stays_quiet(title, tier):
    assert scorer().score(NewsItem(source="t", title=title, tier=tier)).severity < Severity.HIGH


def test_listed_big_companies_are_not_untracked():
    title = "$BNTX shares rally in premarket trading"
    with_uni = scorer().score(NewsItem(source="t", title=title, tier=T.MEDIA))
    without = scorer(False).score(NewsItem(source="t", title=title, tier=T.MEDIA))
    assert not any("untracked" in r for r in with_uni.reasons) and with_uni.score > without.score


def test_health_sources_are_on_by_default():
    names = {s["name"] for s in build_config({}).sources}
    assert {
        "nyt-health",
        "statnews",
        "endpoints-news",
        "fierce-biotech",
        "biopharma-dive",
        "gnews-health-policy",
    } <= names
    scoops = next(s for s in build_config({}).sources if s["name"] == "gnews-scoops")
    assert "site:nytimes.com" in scoops["url"] and "site:statnews.com" in scoops["url"]
