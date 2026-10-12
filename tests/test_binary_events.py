"""A binary event days away is news on the day it's announced (TRAX, Oct 11-12 2026).

First Tracks ($1.1B) said on Sunday at 16:00 ET that Phase 1b data would come Monday at 08:30;
the stock traded up about $5 overnight. The headline ("to Announce Top-Line Data … on Oct. 12")
read like a schedule notice: it scored low, went on the calendar and never reached the tape.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news247.analysis.catalyst_dates import extract_event
from news247.analysis.scorer import Scorer
from news247.config import build_config
from news247.edge import Calendar
from news247.market.radar import SmallCapFeed
from news247.market.universe import Universe
from news247.models import NewsItem, Severity
from news247.models import SourceTier as T
from news247.storage import Storage

from .conftest import CaptureNotifier

ET = ZoneInfo("America/New_York")
SUNDAY_4PM = datetime(2026, 10, 11, 16, 0, tzinfo=ET).timestamp()
TRAX = "First Tracks Biotherapeutics to Announce Top-Line Data from Cohort 1 of Phase 1b Trial of ANB033, a CD122 antagonist, in Celiac Disease on Oct. 12, 2026"  # noqa: E501
UNI = {
    "TRAX": {"name": "First Tracks Biotherapeutics, Inc. Common Stock", "mcap": 1.14e9, "price": 31.85,
             "industry": "Biotechnology: Pharmaceutical Preparations"},
    "KSTL": {"name": "Kestrel Therapeutics, Inc. Common Stock", "mcap": 900e6, "price": 12.0,
             "industry": "Biotechnology: Pharmaceutical Preparations"},
    "MIDB": {"name": "Midsize Bio Inc. Common Stock", "mcap": 20e9, "price": 80.0,
             "industry": "Biotechnology: Pharmaceutical Preparations"},
    "VRTX": {"name": "Vertex Pharmaceuticals Incorporated Common Stock", "mcap": 1.1e11, "price": 430.0},
    "AAPL": {"name": "Apple Inc. Common Stock", "mcap": 3.5e12, "price": 250.0},
}  # fmt: skip


@pytest.fixture
def scorer() -> Scorer:
    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    s.attach_universe(Universe.stub(UNI), **cfg.smallcap.filters())
    return s


def at(title: str, when: float = SUNDAY_4PM, **kw) -> NewsItem:
    item = NewsItem(source="globenewswire", title=title, tier=kw.pop("tier", T.WIRE), published=when, **kw)
    item.detected = when + 20
    return item


# --------------------------------------------------------------------------- reading the date


@pytest.mark.parametrize(
    "title,date,what",
    [
        (TRAX, "2026-10-12", "readout"),
        (
            "First Tracks Biotherapeutics (TRAX) Sets Oct. 12 For Phase 1b ANB033 Cohort 1 Data",
            "2026-10-12",
            "readout",
        ),
        ("Kestrel to Report Topline Data from Phase 2b Study Tomorrow", "2026-10-12", "readout"),
        ("Kestrel Will Report Phase 3 Results on Monday", "2026-10-12", "readout"),
        ("Kestrel Will Report Phase 3 Results Next Monday", "2026-10-19", "readout"),
        ("Kestrel Announces PDUFA Target Action Date of October 15, 2026", "2026-10-15", "pdufa"),
    ],
)
def test_the_date_is_read_from_any_phrasing(title, date, what):
    ev = extract_event(title, "", "TRAX", now=SUNDAY_4PM)
    assert ev is not None and (ev["date"], ev["event"]) == (date, what)


@pytest.mark.parametrize(
    "title",
    [
        "Kestrel to Announce Third Quarter 2026 Financial Results on November 5, 2026",
        "Kestrel Sets Date for Third Quarter Results Call",
        "Kestrel to Host R&D Day on October 13, 2026",
        "Kestrel to Participate in Upcoming Investor Conferences in October",
    ],
)
def test_schedules_that_are_not_binary_events(title):
    assert extract_event(title, "", "KSTL", now=SUNDAY_4PM) is None


def test_the_call_time_comes_from_the_release():
    summary = (
        "The company will host a conference call and webcast at 8:30 a.m. ET on Monday, October 12, 2026."
    )
    assert extract_event(TRAX, summary, "TRAX", now=SUNDAY_4PM)["time"] == "08:30"


# --------------------------------------------------------------------------- scoring


def test_trax_announcement_is_pushed(scorer):
    a = scorer.explain(at(TRAX, tickers=["TRAX"]))
    assert a.severity == Severity.HIGH and a.tickers[0] == "TRAX"
    assert any("binary event tomorrow: TRAX" in r and "before the data" in r for r in a.reasons)
    # the same news worded by an aggregator, no ticker attached
    b = scorer.explain(
        at("First Tracks Biotherapeutics (TRAX) Sets Oct. 12 For Phase 1b ANB033 Cohort 1 Data", tier=T.MEDIA)
    )
    assert b.severity == Severity.HIGH and b.tickers[0] == "TRAX"


def test_size_and_distance_set_the_level(scorer):
    soon = scorer.explain(at("Kestrel Therapeutics to Report Topline Phase 2 Data on October 21, 2026"))
    assert soon.severity == Severity.MEDIUM  # 10 days away: in the app
    mid = scorer.explain(at("Midsize Bio to Report Topline Phase 3 Data on October 12, 2026"))
    assert mid.severity == Severity.MEDIUM  # $20B: one readout matters less
    big = scorer.explain(at("Vertex Pharmaceuticals to Report Topline Phase 3 Data on October 12, 2026"))
    assert not any("binary event" in r for r in big.reasons)  # $110B: its ordinary score
    far = scorer.explain(at("Kestrel Therapeutics to Report Topline Phase 2 Data on November 30, 2026"))
    assert far.severity < Severity.HIGH and not any("binary event" in r for r in far.reasons)


def test_a_past_date_is_history(scorer):
    later = SUNDAY_4PM + 3 * 86400
    a = scorer.explain(at(TRAX, when=later, tickers=["TRAX"]))
    assert not any("binary event" in r for r in a.reasons) and a.severity < Severity.HIGH


def test_the_result_itself_still_scores_on_its_own(scorer):
    pos = scorer.explain(
        at(
            "First Tracks Biotherapeutics Announces Positive Top-Line Data from Cohort 1 of Phase 1b Trial of ANB033 in Celiac Disease",
            tickers=["TRAX"],
        )
    )  # noqa: E501
    assert pos.severity >= Severity.HIGH and pos.direction == "up"
    neg = scorer.explain(at("ANB033 Phase 1b celiac trial misses primary endpoint", tickers=["TRAX"]))
    assert neg.severity >= Severity.HIGH and neg.direction == "down"


# --------------------------------------------------------------------------- the radar watches it


def test_calendar_names_tomorrows_binary_events():
    cal = Calendar()
    for sym, d in (("TRAX", "2026-10-12"), ("KSTL", "2026-10-21"), ("OLD", "2026-10-09")):
        cal.add({"id": f"{sym}:{d}:readout", "date": d, "kind": "binary", "event": "readout", "symbol": sym})
    assert cal.binary_symbols(SUNDAY_4PM) == ["TRAX"]
    assert cal.binary_symbols(SUNDAY_4PM, days=10) == ["TRAX", "KSTL"]


def test_sweep_reads_watched_small_caps_whatever_their_size():
    cfg = build_config({})
    feed = SmallCapFeed(cfg.smallcap, None, Universe.stub(UNI), None, watch=lambda: ["TRAX", "NOPE", "AAPL"])  # type: ignore[arg-type]
    batch = [li.symbol for li in feed.sweep_batch()]
    assert "TRAX" in batch and batch.count("AAPL") == 1 and "NOPE" not in batch
    broken = SmallCapFeed(cfg.smallcap, None, Universe.stub(UNI), None, watch=lambda: 1 / 0)  # type: ignore[arg-type]
    assert "TRAX" not in [li.symbol for li in broken.sweep_batch()]  # a calendar bug never stops the sweep


# --------------------------------------------------------------------------- end to end


async def test_engine_pushes_it_and_shows_where_the_date_came_from(tmp_path: Path):
    from news247.engine import Engine
    from news247.notify import Dispatcher

    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {"console": {"enabled": False}},
            "trends": {"scan": False},
        }
    )
    Universe.stub(UNI).save(tmp_path / "universe.json.gz")
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    cap = CaptureNotifier(Severity.HIGH)
    disp.channels = [cap]
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), dispatcher=disp, sources=[])
    # the release went out a moment ago for data tomorrow morning
    now = time.time()
    tomorrow = datetime.fromtimestamp(now, ET).date() + timedelta(days=1)
    title = (
        f"First Tracks Biotherapeutics to Announce Top-Line Data from Cohort 1 of Phase 1b Trial of ANB033 "
        f"on {tomorrow:%b}. {tomorrow.day}, {tomorrow.year}"
    )
    item = NewsItem(
        source="globenewswire",
        title=title,
        summary="Conference call and webcast at 8:30 a.m. ET.",
        tier=T.WIRE,
        tickers=["TRAX"],
        published=now - 20,
        url="https://example.com/trax",
    )
    await eng.on_item(item)
    await eng.drain(2)
    assert [a.tickers[0] for a in cap.alerts] == ["TRAX"] and cap.alerts[0].severity == Severity.HIGH
    (ev,) = [e for e in eng.calendar.upcoming(3) if e.get("symbol") == "TRAX"]
    assert ev["time"] == "08:30" and ev["source"] == "globenewswire"
    assert ev["published"] == pytest.approx(now - 20) and ev["seen"] == pytest.approx(item.detected)
    assert "TRAX" in eng.calendar.binary_symbols()  # the radar reads it every sweep until then
    # after a restart the stored event still says when it was first seen
    stored = Storage(tmp_path / "e.db").catalysts(tomorrow.isoformat())
    assert stored[0]["seen"] == pytest.approx(item.detected)


def test_events_stored_before_this_change_get_their_capture_time(tmp_path: Path):
    st = Storage(tmp_path / "c.db")
    st.add_catalyst({"id": "TRAX:2099-10-12:readout", "date": "2099-10-12", "symbol": "TRAX"})
    (ev,) = st.catalysts("2099-01-01")
    assert ev["seen"] == pytest.approx(time.time(), abs=5)
