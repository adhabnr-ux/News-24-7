"""The little things: the market-cap universe, small-cap catalyst sizing, and the movers radar."""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.analysis.scorer import Scorer
from news247.analysis.smallcap import SmallCapDesk, amounts, floor_for, points_for, relative_move
from news247.backtest import history_universe, load_history, run_backtest
from news247.config import ConfigError, build_config
from news247.edge import EdgeDesk
from news247.engine import Engine
from news247.market import radar as radar_mod
from news247.market.radar import MoversRadar, Quote, SmallCapFeed, parse_yahoo_screener, quotes_from_listings
from news247.market.universe import (
    Listing,
    Universe,
    band,
    display_name,
    fmt_cap,
    money,
    name_key,
    parse_screener,
    pct,
)
from news247.models import Alert, NewsItem, Severity
from news247.models import SourceTier as T
from news247.notify.format import plain_body, sms_text
from news247.sources.rss import RSSSource
from news247.storage import Storage
from news247.web.server import WebServer

from .conftest import CaptureNotifier

BIO = "Biotechnology: Pharmaceutical Preparations"

STUB = {
    "ACMB": {"name": "Acme Biotech, Inc.", "mcap": 180e6, "price": 3.2, "industry": BIO},
    "DRNZ": {"name": "Dronez Systems Inc.", "mcap": 70e6, "price": 2.1, "industry": "Aerospace"},
    "VRNA": {"name": "Verona Pharma plc", "mcap": 5.5e9, "price": 88.0, "industry": BIO},
    "MRK": {"name": "Merck & Company, Inc.", "mcap": 210e9, "price": 80.0, "industry": BIO},
    "TINY": {"name": "Tiny Pump Corp", "mcap": 8e6, "price": 2.0},
    "LAC": {"name": "Lithium Americas Corp.", "mcap": 700e6, "price": 3.0, "industry": "Metal Mining"},
    "SERV": {"name": "Serve Robotics Inc.", "mcap": 100e6, "price": 2.8},
    "CAPR": {"name": "Capricor Therapeutics, Inc.", "mcap": 290e6, "price": 7.0, "industry": BIO},
    "AILE": {"name": "iLearningEngines, Inc.", "mcap": 430e6, "price": 3.6},
    "PENNY": {"name": "Pennyworth Mining Corp", "mcap": 90e6, "price": 0.4},
    "HKIP": {
        "name": "Harbour Kite Ltd",
        "mcap": 60e6,
        "price": 4.0,
        "country": "Hong Kong",
        "ipo_year": 2025,
    },
    "GRID": {"name": "Gridline Power Inc.", "mcap": 400e6, "price": 9.0},
    "GRDX": {"name": "Gridline Biosciences Inc.", "mcap": 300e6, "price": 5.0, "industry": BIO},
    "CHMP": {"name": "Champion Industries Inc.", "mcap": 200e6, "price": 4.0},
}


@pytest.fixture(scope="module")
def uni() -> Universe:
    return Universe.stub(STUB)


@pytest.fixture(scope="module")
def desk(uni: Universe) -> SmallCapDesk:
    return SmallCapDesk(uni)


@pytest.fixture(scope="module")
def scorer(uni: Universe) -> Scorer:
    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    s.attach_universe(uni, **cfg.smallcap.filters())
    return s


def item(title: str, tier: T = T.WIRE, tickers: list[str] | None = None, **extra) -> NewsItem:
    return NewsItem(source="t", title=title, tier=tier, tickers=tickers or [], extra=dict(extra))


# --------------------------------------------------------------------------- universe


def screener_rows(n: int = 3) -> list[dict]:
    rows = [
        {
            "symbol": "ACCS",
            "name": "ACCESS Newswire Inc. Common Stock",
            "lastsale": "$10.50",
            "netchange": "1.24",
            "pctchange": "13.391%",
            "volume": "6099",
            "marketCap": "40401302.00",
            "country": "United States",
            "ipoyear": "",
            "industry": "Publishing",
            "sector": "Consumer Discretionary",
            "url": "/market-activity/stocks/accs",
        },
        {
            "symbol": "AAPL",
            "name": "Apple Inc. Common Stock",
            "lastsale": "$227.52",
            "netchange": "-1.03",
            "pctchange": "-0.451%",
            "volume": "36211762",
            "marketCap": "3459074880000.00",
            "country": "United States",
            "ipoyear": "1980",
            "industry": "Computer Manufacturing",
            "sector": "Technology",
            "url": "/x",
        },
        {
            "symbol": "BRK/A",
            "name": "Berkshire Hathaway Inc.",
            "lastsale": "$700000",
            "pctchange": "NA",
            "marketCap": "",
            "volume": "1000",
        },
        {
            "symbol": "ABCDW",
            "name": "Abcd Corp. Warrant",
            "lastsale": "$0.10",
            "pctchange": "5%",
            "marketCap": "0.00",
        },
    ]
    return rows[:n] + rows[3:] if n < 3 else rows


def test_parse_screener_both_shapes():
    dl = parse_screener({"data": {"asOf": None, "headers": {}, "rows": screener_rows()}})
    paged = parse_screener({"data": {"table": {"rows": screener_rows()}, "totalrecords": 4}})
    assert [li.symbol for li in dl] == [li.symbol for li in paged] == ["ACCS", "AAPL", "BRK-A", "ABCDW"]
    accs, aapl, brk, warrant = dl
    assert accs.name == "ACCESS Newswire Inc." and accs.market_cap == pytest.approx(40401302.0)
    assert accs.price == 10.5 and accs.change_pct == pytest.approx(13.391) and accs.volume == 6099
    assert aapl.ipo_year == 1980 and aapl.band == "mega cap" and aapl.cap_label == "$3.5T"
    assert brk.market_cap is None and brk.change_pct is None and brk.band == "unknown size"
    assert warrant.common is False and warrant.market_cap is None


@pytest.mark.parametrize(
    "raw,want",
    [
        ("Acme Biotech, Inc. Common Stock", "Acme Biotech, Inc."),
        ("Verona Pharma plc American Depositary Shares", "Verona Pharma plc"),
        ("Alphabet Inc. Class A Common Stock", "Alphabet Inc."),
        ("Brookfield Corp Class A Limited Voting Shares", "Brookfield Corp Class A Limited Voting Shares"),
    ],
)
def test_display_name(raw, want):
    assert display_name(raw) == want


def test_name_key_and_numbers():
    assert name_key("Merck & Company, Inc.") == "merck and"  # suffixes peeled: & -> and, company, inc
    assert name_key("Serve Robotics Inc.") == "serve robotics"
    assert name_key("The Trade Desk, Inc.") == "the trade desk"
    assert money("$1,234.50") == 1234.5 and money("NA") is None and money("0.00") is None and money(5) == 5
    assert pct("-0.451%") == -0.451 and pct("NA") is None and pct(3) == 3.0
    assert fmt_cap(180e6) == "$180M" and fmt_cap(5.5e9) == "$5.5B" and fmt_cap(None) == "?"
    assert band(40e6) == "nano cap" and band(250e6) == "micro cap" and band(1e9) == "small cap"
    assert band(5e9) == "mid cap" and band(50e9) == "large cap" and band(500e9) == "mega cap"


def test_find_issuer_from_the_headline_lead(uni: Universe):
    assert uni.find_issuer("Acme Biotech Announces FDA Approval of ACM-101").symbol == "ACMB"
    assert uni.find_issuer("Serve Robotics Expands Fleet").symbol == "SERV"
    assert uni.find_issuer("FDA approves first gene therapy") is None  # acronym alone is never a company
    assert uni.find_issuer("Champion league results") is None  # one common word is not a company


def test_find_named_anywhere(uni: Universe):
    syms = lambda t: [li.symbol for li in uni.find_named(t)]  # noqa: E731
    assert syms("Nvidia discloses stake in Serve Robotics") == ["SERV"]
    assert syms("FDA advisory committee votes against Capricor's deramiocel") == ["CAPR"]  # distinctive word
    assert syms("We are short iLearningEngines") == ["AILE"]  # CamelCase name
    assert syms("Gridline outage hits Texas") == []  # first word shared by two companies: ambiguous
    assert syms("the champion of small caps") == []  # lower-case, common word
    assert syms("Lithium Americas surges on DOE stake") == ["LAC"]


def test_ambiguous_names_match_neither():
    u = Universe.stub({"AAA": {"name": "Acme Corp", "mcap": 1e8}, "BBB": {"name": "Acme Inc", "mcap": 2e8}})
    assert u.lookup_name("Acme Corp") is None and u.find_issuer("Acme Corp wins contract") is None


def test_save_and_load_roundtrip(tmp_path: Path, uni: Universe):
    path = tmp_path / "universe.json.gz"
    uni.save(path)
    back = Universe.load(path)
    assert back is not None and len(back) == len(uni) and back.get("ACMB") == uni.get("ACMB")
    assert back.find_issuer("Acme Biotech wins") is not None
    path.write_bytes(b"not gzip")
    assert Universe.load(path) is None
    assert Universe.load(tmp_path / "missing.gz") is None


def test_pump_profile():
    li = Universe.stub(STUB).get("HKIP")
    assert li is not None and "Hong Kong micro-cap IPO" in li.pump_profile
    assert Universe.stub(STUB).get("ACMB").pump_profile == ""


# --------------------------------------------------------------------------- catalyst sizing


def test_money_helpers():
    assert amounts("a $45 million order and $1.2B more, plus US$300,000") == [45e6, 1.2e9, 300000.0]
    assert amounts("$425,000,000 Private Placement") == [425e6]
    assert (
        relative_move(0.25) == pytest.approx(24.7, abs=0.2)
        and relative_move(1) == 70.0
        and relative_move(5) == 80.0
    )
    assert points_for(70) == 32 and points_for(30) == 24 and points_for(5) == 0
    assert floor_for(45) == 70 and floor_for(26) == 62 and floor_for(16) == 50 and floor_for(9) == 0


@pytest.mark.parametrize(
    "title,tickers,kind,direction",
    [
        ("Acme Biotech (NASDAQ: ACMB) Announces FDA Approval of Lead Drug", ["ACMB"], "fda_approval", "up"),
        ("Acme Biotech Receives Complete Response Letter from FDA", [], "fda_negative", "down"),
        ("Acme Biotech Announces Positive Topline Results from Pivotal Phase 3 Study", [], "trial_win", "up"),
        ("Acme Biotech Phase 3 trial did not meet its primary endpoint", [], "trial_fail", "down"),
        ("Acme Biotech Announces Topline Results from Phase 3 ACME-2 Study", [], "trial_readout", "mixed"),
        ("Acme Biotech Phase 3 Study Stopped Early for Efficacy", [], "trial_win", "up"),
        (
            "Acme Biotech to Present Topline Results on October 20 from Pivotal Phase 3 Study",
            [],
            "readout_scheduled",
            "mixed",
        ),
        ("Acme Biotech Announces Alignment with FDA on Accelerated Approval Pathway", [], "fda_path", "up"),
        (
            "FDA advisory committee votes 9-3 against Acme Biotech's ACM-101",
            ["ACMB"],
            "adcom_negative",
            "down",
        ),
        ("Acme Biotech to be acquired by Pfizer for $6.40 per share in cash", [], "acquired", "up"),
        ("Dronez Systems (NASDAQ: DRNZ) Awarded $45 Million U.S. Army Contract", ["DRNZ"], "contract", "up"),
        ("Lithium Americas says Department of Energy to take 5% equity stake", [], "gov_stake", "up"),
        ("SCHEDULE 13G: Nvidia Corp discloses stake in Serve Robotics Inc. (SERV)", [], "mega_partner", "up"),
        (
            "Acme Biotech (NASDAQ: ACMB) Announces Pricing of $60 Million Public Offering",
            ["ACMB"],
            "offering",
            "down",
        ),
        ("@HindenburgRes: We are short iLearningEngines", [], "short_report", "down"),
        (
            "Dronez Systems Announces $100 Million Private Placement to Initiate Solana Treasury Strategy",
            [],
            "crypto_treasury",
            "up",
        ),
        ("Dronez Systems Files for Chapter 11 Bankruptcy Protection", [], "bankruptcy", "down"),
        (
            "Acme Biotech Receives Nasdaq Notification Regarding Minimum Bid Price Deficiency",
            [],
            "routine",
            "mixed",
        ),
    ],
)
def test_classify(desk: SmallCapDesk, title, tickers, kind, direction):
    read = desk.read(title, tickers=tickers)
    assert read is not None and read.catalyst is not None, title
    assert (read.catalyst.kind, read.catalyst.direction) == (kind, direction), read.to_dict()


def test_buyout_premium_from_offer_or_stated(desk: SmallCapDesk):
    r = desk.read("Acme Biotech to be acquired by Pfizer for $6.40 per share in cash")
    assert (
        r.catalyst.expected == pytest.approx(100.0) and r.catalyst.move_text == "≈+100% (to the offer price)"
    )
    r = desk.read(
        "Acme Biotech enters definitive agreement to be acquired, representing a premium of approximately 62%"
    )
    assert r.catalyst.expected == 62 and "62% premium" in r.catalyst.label


def test_target_vs_acquirer(desk: SmallCapDesk):
    r = desk.read(
        "Merck (NYSE: MRK) to Acquire Verona Pharma (NASDAQ: VRNA) for $107 per ADS", tickers=["MRK", "VRNA"]
    )
    assert r.listing.symbol == "VRNA" and r.catalyst.kind == "acquired"  # MRK is mega: never a small-cap read
    assert r.catalyst.expected == pytest.approx((107 / 88 - 1) * 100, abs=0.1)
    r = desk.read("Dronez Systems to Acquire Skyhawk Labs for $70 Million", tickers=["DRNZ"])
    assert r.catalyst.kind == "acquirer" and r.catalyst.direction == "mixed"


def test_contract_is_sized_against_the_company(desk: SmallCapDesk):
    big = desk.read("Dronez Systems Awarded $45 Million U.S. Army Contract")
    small = desk.read("Dronez Systems Awarded $1.5 Million Navy Contract")
    capped = desk.read("Dronez Systems Awarded IDIQ Contract With Ceiling of Up to $45 Million")
    assert big.catalyst.relative == pytest.approx(45 / 70) and big.material
    assert small.catalyst.expected < 5 and not small.material
    assert capped.catalyst.expected < big.catalyst.expected  # ceilings are discounted


def test_fluff_and_pharma_supply_deals_are_not_catalysts(desk: SmallCapDesk):
    assert desk.read("Dronez Systems Joins NVIDIA Inception Program").material is False
    assert desk.read("Dronez Systems AI Now Available on AWS Marketplace with Microsoft").material is False
    r = desk.read("Acme Biotech Announces Clinical Trial Collaboration and Supply Agreement with Merck")
    assert not r.material


def test_pump_guards(desk: SmallCapDesk):
    tiny = desk.read("Tiny Pump Corp Announces Partnership with NVIDIA")
    assert tiny.blocked.startswith("under $30M") and tiny.points == 0
    penny = desk.read("Pennyworth Mining Awarded $50 Million Contract")
    assert "under $1" in penny.blocked
    hk = desk.read("Harbour Kite Ltd Announces Strategic Partnership with NVIDIA")
    assert "manipulation" in hk.blocked
    # a deal bigger than the whole company is a capital event even for a nano cap
    pipe = desk.read(
        "Tiny Pump Corp Announces $425,000,000 Private Placement to Initiate Ethereum Treasury Strategy"
    )
    assert pipe.catalyst.kind == "crypto_treasury" and pipe.material and not pipe.blocked
    assert any("nano cap" in n for n in pipe.notes)


def test_halt_on_a_small_cap(desk: SmallCapDesk):
    r = desk.read(
        "TRADING HALT ACMB (Acme Biotech) — News Pending [T1]",
        tickers=["ACMB"],
        extra={"halt_code": "T1", "halt_reason": "News Pending"},
    )
    assert r.catalyst.kind == "halt" and r.material


# --------------------------------------------------------------------------- scorer integration


def test_same_headline_scored_by_company_size(scorer: Scorer):
    plain = build_config({})
    no_uni = Scorer(plain.scoring, plain.knowledge, plain.market.symbols)
    title = "Acme Biotech (NASDAQ: ACMB) Announces FDA Approval of Lead Drug"
    without, with_ = no_uni.score(item(title)), scorer.score(item(title))
    assert without.severity <= Severity.MEDIUM  # the calibration case: unknown company
    assert with_.severity >= Severity.HIGH and with_.direction == "up"
    assert with_.smallcap["symbol"] == "ACMB" and with_.smallcap["material"]
    assert any("micro cap" in r for r in with_.reasons) and not any("untracked" in r for r in with_.reasons)


def test_floor_makes_a_quiet_release_loud(scorer: Scorer):
    a = scorer.score(item("Dronez Systems (NASDAQ: DRNZ) Awarded $45 Million U.S. Army Contract"))
    assert a.severity >= Severity.HIGH and any("small-cap floor" in r for r in a.reasons)
    assert a.tickers[0] == "DRNZ"
    rumor = scorer.score(item("Dronez Systems Awarded $45 Million U.S. Army Contract", T.SOCIAL))
    assert rumor.score < a.score  # a social post earns less trust than the company's release


def test_routine_and_blocked_stay_quiet(scorer: Scorer):
    assert (
        scorer.score(
            item("Acme Biotech (NASDAQ: ACMB) to Present at the H.C. Wainwright Conference")
        ).severity
        is Severity.LOW
    )
    a = scorer.score(item("Tiny Pump Corp (NASDAQ: TINY) Announces Partnership with NVIDIA"))
    assert a.severity < Severity.HIGH and any("not credited" in r for r in a.reasons)


def test_direction_comes_from_the_catalyst(scorer: Scorer):
    # "plunge" would read as down; being bought at a premium is up
    a = scorer.score(
        item("Acme Biotech to be acquired by Pfizer for $6.40 per share after shares plunge 40% this year")
    )
    assert a.direction == "up"


def test_big_caps_are_untouched(scorer: Scorer):
    a = scorer.score(
        item("Merck (NYSE: MRK) Announces FDA Approval of Keytruda Subcutaneous", tickers=["MRK"])
    )
    assert not a.smallcap


# --------------------------------------------------------------------------- backtest


def test_backtest_small_cap_lane():
    cfg = build_config({})
    history = load_history()
    assert history_universe(history) is not None
    rep = run_backtest(cfg, history)
    small = [e for e in rep.events if e.event.get("category", "").startswith("smallcap_")]
    assert len(small) >= 30
    missed = [e.event["id"] for e in small if not e.caught]
    assert set(missed) <= {"xos-registered-direct"}, missed  # a 12% dilution is shown, not pushed
    noise = {
        h["text"] for h in history["noise"] if isinstance(h, dict) and (h.get("smallcap") or h.get("tickers"))
    }
    flagged = [t for t, a in rep.noise if t in noise and a.severity >= Severity.HIGH]
    assert not flagged, flagged


# --------------------------------------------------------------------------- the radar


def sc_cfg(**kw):
    return build_config({"smallcap": kw}).smallcap


def q(sym="KTRA", pct_=25.0, price=5.0, vol=2e6, cap=300e6, **kw) -> Quote:
    return Quote(
        sym,
        name=sym + " Inc",
        price=price,
        prev_close=price / (1 + pct_ / 100),
        change_pct=pct_,
        volume=vol,
        market_cap=cap,
        **kw,
    )


def test_parse_yahoo_sessions():
    data = {
        "finance": {
            "result": [
                {
                    "quotes": [
                        {
                            "symbol": "AAA",
                            "quoteType": "EQUITY",
                            "marketState": "REGULAR",
                            "regularMarketPrice": 5.0,
                            "regularMarketChangePercent": 25.0,
                            "regularMarketPreviousClose": 4.0,
                            "regularMarketVolume": 1e6,
                            "averageDailyVolume3Month": 1e5,
                            "marketCap": 3e8,
                            "shortName": "Aaa",
                        },
                        {
                            "symbol": "BBB",
                            "quoteType": "EQUITY",
                            "marketState": "PRE",
                            "regularMarketPrice": 4.0,
                            "preMarketPrice": 6.0,
                            "preMarketChangePercent": 50.0,
                            "regularMarketVolume": 0,
                            "marketCap": 2e8,
                        },
                        {
                            "symbol": "CCC",
                            "quoteType": "EQUITY",
                            "marketState": "POST",
                            "regularMarketPrice": 11.0,
                            "regularMarketChangePercent": 10.0,
                            "postMarketPrice": 13.2,
                            "postMarketChangePercent": 20.0,
                        },
                        {"symbol": "DDD", "quoteType": "ETF", "regularMarketPrice": 1.0},
                        {
                            "symbol": "EEE",
                            "regularMarketPrice": {"raw": 2.0, "fmt": "2.00"},
                            "regularMarketChangePercent": {"raw": 30},
                        },
                    ]
                }
            ]
        }
    }
    qs = {x.symbol: x for x in parse_yahoo_screener(data)}
    assert set(qs) == {"AAA", "BBB", "CCC", "EEE"}
    assert qs["AAA"].rvol == 10 and qs["AAA"].dollar_volume == 5e6 and qs["AAA"].session == "regular"
    assert (qs["BBB"].price, qs["BBB"].change_pct, qs["BBB"].prev_close, qs["BBB"].session) == (
        6.0,
        50.0,
        4.0,
        "pre",
    )
    assert qs["CCC"].session == "post" and qs["CCC"].change_pct == pytest.approx(32.0)  # 1.1 × 1.2
    assert qs["EEE"].price == 2.0 and qs["EEE"].change_pct == 30


def test_radar_baselines_then_fires_once_per_level():
    r = MoversRadar(sc_cfg())
    t = 1_800_000_000.0
    assert r.scan([q(pct_=25)], t, provider="yahoo") == []  # already moving at start: baseline only
    assert r.snapshot()[0]["symbol"] == "KTRA"  # ...but it is on the board
    assert r.scan([q("NEW1", 22)], t + 60, provider="yahoo")[0].level == 20
    assert r.scan([q("NEW1", 30)], t + 120, provider="yahoo") == []  # same level: no repeat
    hits = r.scan([q("NEW1", 52)], t + 180, provider="yahoo")
    assert [h.level for h in hits] == [50]  # escalation to the next rung
    down = r.scan([q("NEW2", -36)], t + 240, provider="yahoo")
    assert down[0].direction == "down" and down[0].level == 35


def test_radar_filters_pumps_and_ghost_volume():
    r = MoversRadar(sc_cfg())
    t = 1_800_000_000.0
    r.scan([], t, provider="yahoo")
    out = r.scan(
        [
            q("NANO", 60, cap=10e6),  # under the market-cap floor
            q("PENY", 60, price=0.5),  # sub-$1
            q("THIN", 60, vol=10_000),  # $50K traded: nobody is trading it
            q("HUGE", 60, cap=5e9),  # above the radar's range
            q("REAL", 60),
        ],
        t + 60,
        provider="yahoo",
    )
    assert [h.symbol for h in out] == ["REAL"]


def test_radar_jump_between_scans():
    r = MoversRadar(sc_cfg())
    t = 1_800_000_000.0
    r.scan([q("JMP", 5, price=10.0)], t, provider="yahoo")
    r.scan([q("JMP", 6, price=10.1)], t + 60, provider="yahoo")
    hits = r.scan([q("JMP", 18, price=11.2)], t + 120, provider="yahoo")
    assert len(hits) == 1 and hits[0].kind == "jump" and hits[0].jump_pct == pytest.approx(12.0)
    assert hits[0].jump_window_s == 120 and hits[0].ref_price == 10.0
    assert r.scan([q("JMP", 30, price=12.5)], t + 180, provider="yahoo")[0].kind == "day"  # jump cooled down


def test_radar_severity_and_daily_cap():
    r = MoversRadar(sc_cfg(radar_daily_pushes=1))
    t = 1_800_000_000.0
    r.scan([], t, provider="yahoo")
    big = r.scan([q("BIG1", 40, vol=3e6)], t + 60, provider="yahoo")[0]  # $15M traded, +40%
    big2 = r.scan([q("BIG2", 40, vol=3e6)], t + 120, provider="yahoo")[0]
    small = r.scan([q("SML", 22)], t + 180, provider="yahoo")[0]
    assert (big.severity, big2.severity, small.severity) == ("HIGH", "MEDIUM", "MEDIUM")


def test_radar_new_day_resets():
    r = MoversRadar(sc_cfg())
    t = 1_800_000_000.0
    r.scan([], t, provider="yahoo")
    assert r.scan([q("DAY", 25)], t + 60, provider="yahoo")
    assert r.scan([q("DAY", 25)], t + 86400, provider="yahoo")


def test_quotes_from_listings():
    li = Listing("ABC", "Abc Inc.", market_cap=2e8, price=12.0, change_pct=20.0, volume=1e6)
    (qq,) = quotes_from_listings([li, Listing("NOP", "Nop", price=None, change_pct=1.0)])
    assert qq.prev_close == pytest.approx(10.0) and qq.source == "nasdaq"


# --------------------------------------------------------------------------- the feed (HTTP)


async def test_universe_refresh_and_cache(server, http, tmp_path: Path):
    rows = [
        {
            "symbol": f"S{i:04d}".replace("0", "A"),
            "name": f"Company {i} Inc. Common Stock",
            "lastsale": "$5.00",
            "pctchange": "1%",
            "volume": "1000",
            "marketCap": "200000000.00",
        }
        for i in range(1200)
    ]
    server.on("/api/screener/stocks", {"data": {"rows": rows}})
    cfg = sc_cfg(universe_url=server.url("/api/screener/stocks?tableonly=true&download=true"))
    uni = Universe()
    feed = SmallCapFeed(cfg, http, uni, tmp_path)
    got = await feed.refresh_universe()
    assert got and len(uni) == 1200 and feed.health.last_ok
    assert server.requests[0]["headers"]["Origin"] == "https://www.nasdaq.com"
    assert (tmp_path / "universe.json.gz").exists() and len(SmallCapFeed.load_cached(tmp_path)) == 1200
    # a truncated answer must never wipe a good universe
    server.on("/api/screener/stocks", {"data": {"rows": rows[:5]}})
    assert await feed.refresh_universe() is None and len(uni) == 1200
    server.on("/api/screener/stocks", (503, "busy"))
    assert await feed.refresh_universe() is None and feed.health.consecutive_errors == 2


async def test_yahoo_crumb_dance(server, http, monkeypatch):
    state = {"n": 0}

    def screener(request):
        from aiohttp import web

        state["n"] += 1
        if request.query.get("crumb") != "abc123":
            return web.Response(status=401, text="Invalid Crumb")
        return web.json_response(
            {
                "finance": {
                    "result": [
                        {
                            "quotes": [
                                {
                                    "symbol": "ZZZ",
                                    "regularMarketPrice": 3.0,
                                    "regularMarketChangePercent": 40.0,
                                    "regularMarketVolume": 2e6,
                                    "marketCap": 1e8,
                                }
                            ]
                        }
                    ]
                }
            }
        )

    server.on("/v1/finance/screener/predefined/saved", screener)
    server.on("/", (404, "", {"Set-Cookie": "A3=d=1; Path=/"}))
    server.on("/v1/test/getcrumb", (200, "abc123"))
    monkeypatch.setattr(radar_mod, "YAHOO_SCREENER", server.url("/v1/finance/screener/predefined/saved"))
    monkeypatch.setattr(radar_mod, "YAHOO_COOKIE_URL", server.url("/"))
    monkeypatch.setattr(radar_mod, "YAHOO_CRUMB_URL", server.url("/v1/test/getcrumb"))
    feed = SmallCapFeed(sc_cfg(), http, Universe(), None, MoversRadar(sc_cfg()))
    quotes = await feed.fetch_yahoo()
    assert [x.symbol for x in quotes] == ["ZZZ"] and state["n"] == 2
    assert server.requests[-1]["query"]["scrIds"] == "small_cap_gainers"


async def test_feed_loop_scans_in_session(server, http, monkeypatch):
    import asyncio

    server.on(
        "/v1/finance/screener/predefined/saved",
        {
            "finance": {
                "result": [
                    {
                        "quotes": [
                            {
                                "symbol": "RUN",
                                "regularMarketPrice": 3.0,
                                "regularMarketChangePercent": 30.0,
                                "regularMarketVolume": 2e6,
                                "marketCap": 1e8,
                            }
                        ]
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(radar_mod, "YAHOO_SCREENER", server.url("/v1/finance/screener/predefined/saved"))
    cfg = sc_cfg(radar_seconds=20)
    uni = Universe.stub({"X": {"name": "X", "mcap": 1e8}})  # fresh: no universe download
    radar = MoversRadar(cfg, uni)
    radar.baselined.add("yahoo")  # pretend the start-up baseline happened
    got: list = []
    stop = asyncio.Event()

    async def on_hits(hits):
        got.extend(hits)
        stop.set()

    feed = SmallCapFeed(cfg, http, uni, None, radar, market_phase=lambda now: "pre-market")
    await asyncio.wait_for(feed.run(on_hits, stop), 5)
    assert [h.symbol for h in got] == ["RUN"]
    assert feed.status()["radar"]["scans"] == 1 and feed.status()["listings"] == 1


# --------------------------------------------------------------------------- engine, edge, notify, API


def engine(tmp_path: Path, uni: Universe | None = None) -> tuple[Engine, CaptureNotifier]:
    from news247.notify import Dispatcher

    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {"console": {"enabled": False}},
            "web": {"token": "tok"},
        }
    )
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    cap = CaptureNotifier()
    disp.channels = [cap]
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), dispatcher=disp, sources=[])
    eng.coalesce_s = 0
    if uni is not None:
        eng.universe = uni
        eng.scorer.attach_universe(uni, **cfg.smallcap.filters())
        eng.radar = MoversRadar(cfg.smallcap, uni)
    return eng, cap


async def test_engine_loads_cached_universe(tmp_path: Path):
    Universe.stub(STUB).save(tmp_path / "universe.json.gz")
    eng, _ = engine(tmp_path)
    assert len(eng.universe) == len(STUB) and eng.scorer.smallcap is not None
    assert eng.smallcap_feed is None  # market off: no network feed
    assert eng.status()["smallcap"]["listings"] == len(STUB)


async def test_small_cap_news_alert_carries_the_read(tmp_path: Path, uni: Universe):
    eng, cap = engine(tmp_path, uni)
    it = NewsItem(
        source="globenewswire",
        title="Dronez Systems (NASDAQ: DRNZ) Awarded $45 Million U.S. Army Contract",
        tier=T.WIRE,
        tickers=["DRNZ"],
        url="https://example.com/drnz",
        published=time.time() - 3,
    )
    await eng.on_item(it)
    await eng.drain(2)
    (alert,) = cap.alerts
    sc = alert.edge["smallcap"]
    assert sc["symbol"] == "DRNZ" and sc["material"] and alert.edge["play"]["direct"][0] == "DRNZ"
    line = EdgeDesk.push_line(alert)
    assert line.startswith("DRNZ · $70M micro cap · Contract from U.S. Army")
    assert "Small cap: DRNZ $70M micro cap" in plain_body(alert) and "Small cap: DRNZ" in sms_text(alert)


async def test_radar_alert_then_news_says_radar_was_first(tmp_path: Path, uni: Universe):
    eng, cap = engine(tmp_path, uni)
    t = time.time()
    eng.radar.scan([], t - 120, provider="yahoo")
    hits = eng.radar.scan(
        [q("ACMB", 45, price=4.64, vol=8e6, cap=180e6, avg_volume=4e5, session="pre")],
        t - 60,
        provider="yahoo",
    )
    await eng.on_radar(hits)
    await eng.drain(2)
    (ra,) = cap.alerts
    assert ra.kind == "price" and ra.severity is Severity.HIGH and ra.tickers == ["ACMB"]
    assert ra.title.startswith("▲ ACMB +45.00% pre-market · ") and "$180M micro cap" in ra.title
    assert "20× normal volume" in ra.body and "No headline yet" in ra.body
    assert ra.edge["radar"]["rvol"] == 20 and ra.edge["smallcap"]["radar"] is True
    assert EdgeDesk.push_line(ra) == "ACMB · $180M micro cap · Radar: moving before the news"
    board = eng.radar_board()
    assert board["enabled"] and board["rows"][0]["symbol"] == "ACMB" and board["rows"][0]["flagged"]
    assert board["rows"][0]["alert_id"] == ra.id  # the row opens the radar alert
    # the headline lands afterwards: the alert says the radar had it first
    await eng.on_item(
        NewsItem(
            source="globenewswire",
            title="Acme Biotech (NASDAQ: ACMB) Announces FDA Approval of ACM-101",
            tier=T.WIRE,
            tickers=["ACMB"],
            url="https://example.com/acmb",
            published=time.time() - 2,
        )
    )
    await eng.drain(2)
    news = cap.alerts[-1]
    assert news.kind == "news" and "📡 Radar flagged ACMB +45.00%" in news.body


async def test_radar_alert_links_an_existing_headline(tmp_path: Path, uni: Universe):
    eng, cap = engine(tmp_path, uni)
    await eng.on_item(
        NewsItem(
            source="globenewswire",
            title="Acme Biotech (NASDAQ: ACMB) Announces FDA Approval of ACM-101",
            tier=T.WIRE,
            tickers=["ACMB"],
            url="https://example.com/acmb",
            published=time.time() - 2,
        )
    )
    t = time.time()
    eng.radar.scan([], t - 1, provider="yahoo")
    await eng.on_radar(eng.radar.scan([q("ACMB", 45, price=4.64, vol=8e6, cap=180e6)], t, provider="yahoo"))
    await eng.drain(2)
    ra = cap.alerts[-1]
    assert (
        ra.related
        and ra.related[0]["url"] == "https://example.com/acmb"
        and ra.url == "https://example.com/acmb"
    )
    assert "No headline yet" not in ra.body


async def test_api_radar_and_webpush_payload(tmp_path: Path, uni: Universe):
    eng, _ = engine(tmp_path, uni)
    t = time.time()
    eng.radar.scan([], t - 1, provider="yahoo")
    eng.radar.scan([q("SERV", 33)], t, provider="yahoo")
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/radar")) as r:
                assert r.status == 401
            async with s.get(srv.make_url("/api/radar?token=tok&limit=5")) as r:
                body = await r.json()
            assert body["enabled"] and body["rows"][0]["symbol"] == "SERV" and "market" in body
            async with s.get(srv.make_url("/api/app?token=tok")) as r:
                app = await r.json()
            assert (
                app["smallcap"]["listings"] == len(STUB) and app["smallcap"]["radar"] is False
            )  # no feed: market off
    finally:
        await srv.close()
        await eng.http.close()
    from news247.notify.channels import WebPushNotifier

    a = Alert(kind="news", severity=Severity.HIGH, title="x", body="")
    a.edge["smallcap"] = {
        "symbol": "SERV",
        "cap": "$100M",
        "band": "micro cap",
        "label": "NVIDIA stake",
        "move_text": "+26–70% typical",
        "material": True,
    }
    payload = WebPushNotifier.payload(WebPushNotifier.__new__(WebPushNotifier), a)
    assert payload["smallcap"] is True and payload["body"].count("+26–70% typical") == 1


# --------------------------------------------------------------------------- config, CLI, RSS


def test_config_section():
    cfg = build_config({"smallcap": {"min_market_cap": "50e6", "radar": "false", "radar_provider": "nasdaq"}})
    assert cfg.smallcap.min_market_cap == 50e6 and cfg.smallcap.radar is False
    assert cfg.smallcap.filters()["min_market_cap"] == 50e6
    with pytest.raises(ConfigError):
        build_config({"smallcap": {"radar_provider": "finviz"}})
    with pytest.raises(ConfigError):
        build_config({"smallcap": {"radar_seconds": 5}})
    with pytest.raises(ConfigError):
        build_config({"smallcap": {"bogus": 1}})


def test_cli_score_and_universe_use_the_cache(tmp_path: Path, capsys):
    from news247.cli import main

    Universe.stub(STUB).save(tmp_path / "universe.json.gz")
    conf = tmp_path / "config.yaml"
    conf.write_text(json.dumps({"general": {"data_dir": str(tmp_path)}}))
    assert (
        main(
            [
                "-c",
                str(conf),
                "score",
                "--tier",
                "wire",
                "--ticker",
                "ACMB",
                "Acme Biotech (NASDAQ: ACMB) Announces FDA Approval of Lead Drug",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "small cap: ACMB $180M micro cap · FDA approval" in out and ("HIGH" in out or "CRITICAL" in out)
    assert main(["-c", str(conf), "universe", "ACMB", "Serve Robotics"]) == 0
    out = capsys.readouterr().out
    assert "Acme Biotech, Inc." in out and "SERV  Serve Robotics Inc." in out and "small-cap lane" in out


def test_rss_reads_nyse_american_category():
    from news247.sources import SourceContext

    src = RSSSource({"name": "gnw", "url": "https://x"}, SourceContext(http=None, user_agent="t"))  # type: ignore[arg-type]
    it = src.entry_to_item(
        {
            "title": "Uranium Co Wins Contract",
            "link": "https://x/1",
            "tags": [{"term": "NYSE American:UUUU"}, {"term": "Nasdaq:ACMB"}, {"term": "Energy"}],
        }
    )
    assert it.tickers == ["UUUU", "ACMB"]
