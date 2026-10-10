"""Regression for Oct 8-9 2026: SpaceX agreed to buy Grain Management's 800 MHz spectrum (~$8B,
announced Oct 8 after the close). The next day Crown Castle closed +15.6% (opened +7.9%),
American Tower +9.3%, SBA +7.3%, while Verizon, AT&T and T-Mobile fell 10-13%, all on 3-5x volume.

Replayed through the rules as they were, the coverage scored 15-37 (never pushed) and tagged
TSLA, RKLB and ASTS instead of the towers and carriers; the radar never pushed AMT. Every link
must now fire. Prices are real (TradingView daily bars); headlines are the published ones."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news247.analysis.scorer import Scorer
from news247.config import build_config
from news247.market.radar import MoversRadar, Quote
from news247.market.universe import Universe
from news247.models import NewsItem
from news247.models import SourceTier as T

ET = ZoneInfo("America/New_York")
UNI = {
    "CCI": {"name": "Crown Castle Inc. Common Stock", "mcap": 33.9e9, "price": 68.89},
    "AMT": {"name": "American Tower Corporation (REIT) Common Stock", "mcap": 84.9e9, "price": 166.7},
    "SBAC": {"name": "SBA Communications Corporation Class A Common Stock", "mcap": 19.4e9, "price": 170.0},
    "VZ": {"name": "Verizon Communications Inc. Common Stock", "mcap": 173e9, "price": 46.35},
    "T": {"name": "AT&T Inc.", "mcap": 152e9, "price": 24.87},
    "TMUS": {"name": "T-Mobile US, Inc. Common Stock", "mcap": 159e9, "price": 171.3},
    "SATS": {"name": "EchoStar Corporation Class A Common Stock", "mcap": 20e9, "price": 70.0},
    "DAL": {"name": "Delta Air Lines, Inc. Common Stock", "mcap": 40e9, "price": 60.0},
    "HUM": {"name": "Humana Inc. Common Stock", "mcap": 30e9, "price": 250.0},
}


def et(*a: int) -> float:
    return datetime(*a, tzinfo=ET).timestamp()


@pytest.fixture(scope="module")
def scorer() -> Scorer:
    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    s.attach_universe(Universe.stub(UNI), **cfg.smallcap.filters())
    return s


def score(s: Scorer, title: str, tier: T = T.MEDIA):
    return s.score(NewsItem(source="t", title=title, url="u", tier=tier, published=time.time()))


# --------------------------------------------------------------------------- the news


@pytest.mark.parametrize(
    "title,severity,direction",
    [
        # the first reports, Oct 8 after the close (Reuters, WSJ)
        ("SpaceX to acquire spectrum that enables Starlink Mobile services", "CRITICAL", "mixed"),
        ("SpaceX to Pay About $8 Billion for Grain Management's 800 MHz Spectrum", "HIGH", "mixed"),
        ("SpaceX takes aim at US wireless carriers with spectrum acquisition", "HIGH", "down"),
        # Oct 9 coverage (Benzinga)
        ("Crown Castle Stock Jumping as SpaceX Acquires 800 MHz Spectrum", "HIGH", "up"),
        # the Sep 2025 precedent: SpaceX buys EchoStar's AWS-4 / H-block licenses
        ("EchoStar to sell spectrum licenses to SpaceX for about $17 billion", "HIGH", "mixed"),
    ],
)
def test_the_spectrum_deal_pushes_with_the_right_tickers(scorer, title, severity, direction):
    a = score(scorer, title)
    assert a.severity.name == severity, a.reasons
    assert a.direction == direction
    assert "satellite_carrier_threat" in a.themes
    assert {"VZ", "T", "TMUS"} <= set(a.tickers)


def test_tower_read_through_is_tagged(scorer):
    a = score(scorer, "SpaceX to acquire spectrum that enables Starlink Mobile services")
    assert {"CCI", "AMT", "SBAC"} <= set(a.tickers)
    assert "satellite_tower_readthrough" in a.themes


@pytest.mark.parametrize(
    "title",
    [
        "SpaceX launches 24 more Starlink satellites from Cape Canaveral",
        "Starlink launches direct-to-cell texting in Chile with Entel",
        "T-Mobile expands T-Satellite with Starlink to more phones",
        "Starlink adds 1 million subscribers in three months, SpaceX says",
    ],
)
def test_routine_spacex_news_stays_quiet(scorer, title):
    a = score(scorer, title)
    assert a.severity.name in ("LOW", "MEDIUM") and "satellite_carrier_threat" not in a.themes


@pytest.mark.parametrize(
    "title,want",
    [
        ("Crown Castle Stock Jumping as SpaceX Acquires 800 MHz Spectrum", {"CCI"}),
        (
            "SpaceX, AT&T, Crown Castle, Delta, Humana, and More Stocks That Explain Today's Market",
            {"T", "CCI", "HUM"},
        ),
        ("Verizon, AT&T, T-Mobile shares slide as SpaceX buys spectrum", {"VZ", "T", "TMUS"}),
        ("Why American Tower Stock Jumped 8.6% Today", {"AMT"}),  # "(REIT)" in the listed name
    ],
)
def test_big_companies_named_in_a_headline_are_tagged(scorer, title, want):
    assert want <= set(score(scorer, title).tickers)


def test_name_index_reads_reit_suffixes_and_hyphens():
    u = Universe.stub(UNI)
    assert [li.symbol for li in u.find_named("American Tower rallies")] == ["AMT"]
    assert [li.symbol for li in u.find_named("T-Mobile falls")] == ["TMUS"]


# --------------------------------------------------------------------------- the radar


def sc_cfg():
    return build_config({}).smallcap


def q(sym: str, pct: float, session: str = "regular") -> Quote:
    prev = UNI[sym]["price"]
    return Quote(
        sym,
        price=prev * (1 + pct / 100),
        prev_close=prev,
        change_pct=pct,
        market_cap=UNI[sym]["mcap"],
        session=session,
        source="sweep",
    )


def test_cci_flags_pre_market_and_pushes_when_it_crosses_9_percent():
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    (pre,) = r.scan([q("CCI", 7.9, "pre")], et(2026, 10, 9, 8, 5), provider="sweep")
    assert pre.severity == "MEDIUM"  # +7.9% pre-market: on the board, in the app
    (cross,) = r.scan([q("CCI", 9.6)], et(2026, 10, 9, 10, 2), provider="sweep")
    assert cross.severity == "HIGH" and cross.level == pytest.approx(9.0)
    (later,) = r.scan([q("CCI", 15.6)], et(2026, 10, 9, 15, 30), provider="sweep")
    assert later.level == pytest.approx(15.0)


def test_amt_pushes_at_9_3_percent_between_ladder_rungs():
    """+9.3% is past the 9% push line but short of the 10.5% rung: it used to never push."""
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    (first,) = r.scan([q("AMT", 6.4)], et(2026, 10, 9, 9, 50), provider="sweep")
    assert first.severity == "MEDIUM"
    (push,) = r.scan([q("AMT", 9.3)], et(2026, 10, 9, 14, 0), provider="sweep")
    assert push.severity == "HIGH"
    assert r.scan([q("AMT", 9.1)], et(2026, 10, 9, 14, 5), provider="sweep") == []  # no repeat


def test_carriers_after_hours_on_the_announcement():
    """Oct 8 after the close: the carriers fell ~6% in extended trading, then 10-13% on Oct 9."""
    r = MoversRadar(sc_cfg(), Universe.stub(UNI))
    hits = r.scan(
        [q("VZ", -6.1, "post"), q("T", -6.3, "post"), q("TMUS", -6.0, "post")], et(2026, 10, 8, 17, 30)
    )
    assert {h.symbol for h in hits} == {"VZ", "T", "TMUS"} and all(h.direction == "down" for h in hits)
    next_day = r.scan([q("TMUS", -13.3)], et(2026, 10, 9, 11, 0), provider="sweep")
    assert next_day and next_day[0].severity == "HIGH"


# --------------------------------------------------------------------------- the options tape


def cci_data() -> dict:
    import json

    from .conftest import FIXTURES

    return json.loads((FIXTURES / "options_cci_2026-10-09.json").read_text())


def replay_chain(sym: str, data: dict, *, with_oi: bool = False):
    """Friday's closing prints as a chain: last = Friday's close, previous close = the contract's
    last trade before Friday, volume = Friday's. Daily bars carry no bid, so none is invented."""
    import math
    import statistics
    from datetime import date

    from news247.market.options import Chain, Contract, parse_occ

    closes = data["stocks"][sym]["closes"]
    days = sorted(closes)
    hist = [closes[k] for k in days if k <= "2026-10-08"][-21:]
    rets = [math.log(hist[i] / hist[i - 1]) for i in range(1, len(hist))]
    rv = statistics.pstdev(rets) * math.sqrt(252)
    prev, now = closes["2026-10-08"], closes["2026-10-09"]
    chain = Chain(sym, price=now, prev_close=prev, change_pct=(now / prev - 1) * 100, iv30=rv + 0.10)
    ratios = data["vol_oi_2026-10-09"]
    for occ, bars in data["options"].items():
        root, exp, cp, strike = parse_occ(occ)
        if root != sym:
            continue
        fri = bars[-1]
        before = [b for b in bars if b["date"] < "2026-10-09"][-1]
        oi = round(fri["volume"] / ratios[occ]) if with_oi and occ in ratios else None
        chain.contracts.append(
            Contract(
                occ,
                sym,
                exp,
                cp,
                strike,
                last=fri["close"],
                prev_close=before["close"],
                volume=fri["volume"],
                open_interest=oi,
                last_trade=datetime(2026, 10, 9, 15, 50, tzinfo=ET),
            )  # fmt: skip
        )
    assert date(2026, 10, 9) > exp.replace(day=1)  # sanity: Oct 16 expiry, live on Oct 9
    return chain


def options_radar():
    from news247.market.options import OptionsRadar

    return OptionsRadar(build_config({}).options)


@pytest.mark.parametrize("sym,expected", [("CCI", {80.0, 77.5, 75.0}), ("AMT", {185.0, 190.0})])
def test_friday_tower_calls_are_real_thousand_percent_spikes(sym, expected):
    """CCI $80 call: $0.05 -> $1.25 on 1,666 contracts (+2,400%); $77.5: $0.10 -> $2.95;
    $75: $0.05 -> $4.90. AMT $185: $0.10 -> $2.00; $190: $0.03 -> $0.87. All real bases."""
    data = cci_data()
    r = options_radar()
    chain = replay_chain(sym, data)
    rows = [x for x in (r.spike_row(c, chain, datetime(2026, 10, 9).date()) for c in chain.contracts) if x]
    assert {x.contract.strike for x in rows} == expected  # CCI $85 (+540%) stays out
    for x in rows:
        assert x.big_enough and not x.stale_base and x.move_pct >= 1000
    (hit,) = [h for h in r.scan(chain, None, et(2026, 10, 9, 15, 50)) if h.kind == "spike"]
    # daily bars have no bid, so the replay cannot confirm; the live feed carries the bid
    assert hit.severity == "MEDIUM" and "no bid in the data" in hit.reasons[0]


def test_friday_spike_pushes_once_the_bid_is_there():
    data = cci_data()
    chain = replay_chain("CCI", data)
    for c in chain.contracts:
        c.bid, c.ask = round(c.last * 0.92, 2), round(c.last * 1.08, 2)  # a normal 16% spread
    r = options_radar()
    (hit,) = [h for h in r.scan(chain, None, et(2026, 10, 9, 15, 50)) if h.kind == "spike"]
    assert hit.severity == "HIGH" and hit.spikes[0].contract.strike in (75.0, 77.5, 80.0)


def test_friday_unusual_volume_on_the_80_call():
    """Alpha Vantage: the $80 call traded 3.0x its open interest (1,666 vs ~554 open), $208K."""
    data = cci_data()
    r = options_radar()
    chain = replay_chain("CCI", data, with_oi=True)
    rows = {c.strike: r.flow_row(c) for c in chain.contracts}
    assert rows[80.0] is not None and rows[80.0].vol_oi == pytest.approx(3.0, abs=0.05)
    assert rows[77.5] is None and rows[75.0] is None  # those strikes already had open positions


def test_a_contract_with_no_previous_close_is_measured_from_its_model_value():
    data = cci_data()
    chain = replay_chain("CCI", data)
    c = next(x for x in chain.contracts if x.strike == 80.0)
    c.prev_close = None  # never traded before Friday
    row = options_radar().spike_row(c, chain, datetime(2026, 10, 9).date())
    assert row is not None and row.no_prior and c.prev_close == 0.01 and row.move_pct > 10000


# --------------------------------------------------------------------------- the links


def cci_engine(tmp_path):
    from news247.engine import Engine
    from news247.notify import Dispatcher
    from news247.storage import Storage

    from .conftest import CaptureNotifier

    cfg = build_config({"general": {"data_dir": str(tmp_path)}, "notify": {"console": {"enabled": False}}})
    Universe.stub(UNI).save(tmp_path / "universe.json.gz")
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    disp.channels = [CaptureNotifier()]
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), dispatcher=disp, sources=[])
    eng.scorer.attach_universe(eng.universe, **cfg.smallcap.filters())
    return eng


def test_morning_options_alert_cites_last_nights_deal(tmp_path):
    eng = cci_engine(tmp_path)
    t_news = et(2026, 10, 8, 18, 5)
    item = NewsItem(
        source="reuters", title="SpaceX to acquire spectrum that enables Starlink Mobile services",
        url="https://example.com/r", tier=T.MEDIA, published=t_news, detected=t_news,
    )  # fmt: skip
    an = eng.scorer.score(item)
    assert "CCI" in an.tickers
    eng._remember(item, an)
    chain = replay_chain("CCI", cci_data())
    for c in chain.contracts:
        c.bid, c.ask = round(c.last * 0.92, 2), round(c.last * 1.08, 2)
    (hit,) = [
        h
        for h in eng.options_radar.scan(chain, eng.universe.get("CCI"), et(2026, 10, 9, 9, 50))
        if h.kind == "spike"
    ]
    alert = eng._options_alert(hit)
    assert alert.related and alert.related[0]["title"].startswith("SpaceX to acquire spectrum")
    assert "No headline yet" not in alert.body
    assert eng.find_catalysts(["CCI"], et(2026, 10, 9, 9, 50)) == []  # fast price moves keep 45 minutes


def test_overnight_news_keeps_its_names_hot_into_the_next_session(tmp_path):
    import asyncio

    from news247.models import Alert, Severity

    eng = cci_engine(tmp_path)
    eng.calendar.market_status = lambda now=None: {"open": False, "phase": "closed", "until_s": 15.5 * 3600}
    asyncio.run(
        eng.publish(Alert(kind="news", severity=Severity.HIGH, title="t", body="", tickers=["CCI", "VZ"]))
    )
    left = eng.options_feed.priority["CCI"] - time.time()
    assert 17 * 3600 < left <= 17.5 * 3600 + 5  # until 2 hours after the next open
