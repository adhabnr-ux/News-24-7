"""The options tape: pricing, the Cboe parser, spike and unusual-volume rules, the feed, the engine.

Chains below use the field names Cboe's delayed-quotes JSON serves (``data.options[]`` with
``option``, ``bid``, ``ask``, ``last_trade_price``, ``prev_day_close``, ``volume``,
``open_interest``, ``iv``, ``last_trade_time``; ``data.current_price``, ``prev_day_close``,
``percent_change``, ``iv30``), as read by OpenBB's Cboe provider. Their numbers are made up to
exercise each rule, except in the NWE replay, which uses real bars.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.config import ConfigError, build_config
from news247.edge import ET
from news247.engine import Engine
from news247.http import HttpClient
from news247.market.options import (
    Chain,
    Contract,
    OptionsFeed,
    OptionsRadar,
    as_vol,
    bs_price,
    parse_chain,
    parse_occ,
    prev_session,
)
from news247.market.universe import Listing, Universe
from news247.models import Alert, Severity
from news247.storage import Storage
from news247.web.server import WebServer

from .conftest import FIXTURES, CaptureNotifier, Recorder

NOW = datetime(2026, 10, 7, 11, 30, tzinfo=ET).timestamp()  # a Wednesday, mid-session
TODAY = date(2026, 10, 7)
EXP = "261016"  # the Oct 16 monthly


def ocfg(**kw: Any):
    return build_config({"options": kw}).options


def row(occ: str, **kw: Any) -> dict[str, Any]:
    base = {
        "option": occ,
        "bid": 0.0,
        "ask": 0.0,
        "last_trade_price": 0.0,
        "prev_day_close": 0.0,
        "volume": 0,
        "open_interest": 0,
        "iv": 0.3,
        "last_trade_time": "2026-10-07T11:20:00",
    }
    base.update(kw)
    return base


def doc(
    sym: str, price: float, prev: float, rows: list[dict[str, Any]], iv30: float = 24.0, **kw: Any
) -> dict:
    data = {
        "symbol": sym,
        "current_price": price,
        "prev_day_close": prev,
        "percent_change": (price / prev - 1) * 100,
        "iv30": iv30,
        "last_trade_time": "2026-10-07T11:29:00",
        "options": rows,
    }
    data.update(kw)
    return {"timestamp": "2026-10-07 11:29:30", "data": data}


def li(sym: str, cap: float, change: float | None = None, name: str = "") -> Listing:
    return Listing(sym, name or f"{sym} Corp", market_cap=cap, price=50.0, change_pct=change)


# --------------------------------------------------------------------------- pricing


def test_black_scholes_textbook_values_and_parity():
    c = bs_price(100, 100, 1, 0.2, "C", r=0.05)
    p = bs_price(100, 100, 1, 0.2, "P", r=0.05)
    assert c == pytest.approx(10.4506, abs=1e-4) and p == pytest.approx(5.5735, abs=1e-4)
    assert c - p == pytest.approx(100 - 100 * math.exp(-0.05), abs=1e-9)  # put-call parity


def test_black_scholes_edges():
    assert bs_price(110, 100, 0, 0.3, "C") == 10 and bs_price(90, 100, 0, 0.3, "P") == 10  # expiry
    assert bs_price(90, 100, 0.1, 0.0, "C") == 0  # no vol, out of the money
    assert bs_price(0, 100, 0.1, 0.3) == 0
    vols = [bs_price(50, 55, 0.05, s) for s in (0.1, 0.2, 0.4, 0.8)]
    assert vols == sorted(vols) and vols[0] < vols[-1]  # more vol, more value


def test_black_scholes_reproduces_the_study():
    """The lottery-ticket alert study: NWE 75C the day before, spot 68.92, 10 days left: model fair value
    $0.00 at its 16% realized vol and $0.03 at RV + 10 points."""
    assert bs_price(68.92, 75, 10 / 365, 0.16) == pytest.approx(0.0, abs=0.005)
    assert bs_price(68.92, 75, 10 / 365, 0.26) == pytest.approx(0.03, abs=0.005)


@pytest.mark.parametrize(
    "raw,want",
    [(0.25, 0.25), (25.0, 0.25), ("31.5", 0.315), (2.4, 2.4), (0, None), (None, None), ("x", None)],
)
def test_as_vol_reads_fractions_and_percentages(raw, want):
    assert as_vol(raw) == (pytest.approx(want) if want is not None else None)


def test_occ_symbols():
    assert parse_occ("NWE261016C00075000") == ("NWE", date(2026, 10, 16), "C", 75.0)
    assert parse_occ("BRKB261016P00492500") == ("BRKB", date(2026, 10, 16), "P", 492.5)
    assert parse_occ("AAPL1261016C00100000") is None  # adjusted contract (non-standard deliverable)
    assert parse_occ("junk") is None and parse_occ("ABC991340C00010000") is None  # bad date


def test_prev_session_skips_weekends():
    assert prev_session(date(2026, 10, 7)) == date(2026, 10, 6)
    assert prev_session(date(2026, 10, 12)) == date(2026, 10, 9)  # Monday -> Friday


# --------------------------------------------------------------------------- parsing


def test_parse_chain_reads_every_field_and_skips_adjusted_contracts():
    d = doc(
        "XYZ",
        52.0,
        50.0,
        [
            row(
                f"XYZ{EXP}C00055000",
                bid=1.2,
                ask=1.4,
                last_trade_price=1.3,
                prev_day_close=0.1,
                volume=900,
                open_interest=120,
            ),
            row(f"XYZ1{EXP}C00055000", last_trade_price=9.0),  # adjusted
            row(f"ABC{EXP}C00055000"),  # another root
            "not a row",
        ],
    )
    ch = parse_chain(d, "XYZ")
    assert (ch.price, ch.prev_close, round(ch.change_pct, 2), ch.iv30) == (52.0, 50.0, 4.0, 0.24)
    assert ch.quote_time == datetime(2026, 10, 7, 11, 29, tzinfo=ET) and ch.skipped == 2
    (c,) = ch.contracts
    assert (c.cp, c.strike, c.expiry, c.bid, c.ask, c.last, c.prev_close) == (
        "C",
        55.0,
        date(2026, 10, 16),
        1.2,
        1.4,
        1.3,
        0.1,
    )
    assert (c.volume, c.open_interest, c.iv, c.kind) == (900, 120, 0.3, "call")
    assert c.move_pct == pytest.approx(1200.0) and c.premium == pytest.approx(900 * 1.3 * 100)
    assert c.label() == "XYZ $55 call (Oct 16)"


def test_parse_chain_class_shares_and_missing_numbers():
    d = doc(
        "BRK-B", 480.0, 470.0, [row(f"BRKB{EXP}C00500000", bid=None, ask="", volume=None, open_interest="12")]
    )
    (c,) = parse_chain(d, "BRK-B").contracts
    assert c.bid is None and c.ask is None and c.volume == 0 and c.open_interest == 12  # never invented


def test_parse_chain_rejects_garbage():
    with pytest.raises(ValueError):
        parse_chain({"error": "nope"}, "XYZ")


# --------------------------------------------------------------------------- spikes


def chain_with(*rows: dict[str, Any], price: float = 52.0, prev: float = 50.0, iv30: float = 24.0) -> Chain:
    return parse_chain(doc("XYZ", price, prev, list(rows), iv30=iv30), "XYZ")


SPIKE = dict(bid=1.9, ask=2.1, last_trade_price=2.0, prev_day_close=0.15, volume=400, open_interest=900)


def test_real_spike_pushes():
    r = OptionsRadar(ocfg())
    (hit,) = r.scan(chain_with(row(f"XYZ{EXP}C00060000", **SPIKE)), li("XYZ", 5e9), NOW)
    top = hit.spikes[0]
    assert hit.kind == "spike" and hit.severity == "HIGH" and hit.rung == 1000.0
    assert top.confirmed and not top.stale_base and top.move_pct == pytest.approx(1233.3, abs=0.1)
    assert hit.reasons == []
    d = hit.to_dict()
    assert d["spikes"][0]["honest_move_pct"] == pytest.approx(1233.3, abs=0.1) and d["cap"] == "$5.0B"
    assert d["source"].startswith("Cboe delayed")


def test_one_print_with_no_bid_shows_but_does_not_push():
    r = OptionsRadar(ocfg())
    (hit,) = r.scan(
        chain_with(row(f"XYZ{EXP}C00060000", **{**SPIKE, "bid": 0.0, "ask": 2.5})), li("XYZ", 5e9), NOW
    )
    assert hit.severity == "MEDIUM" and "one-sided: the bid is empty" in hit.reasons[0]
    (hit,) = OptionsRadar(ocfg()).scan(
        chain_with(row(f"XYZ{EXP}C00060000", **{**SPIKE, "bid": None})), None, NOW
    )
    assert "no bid in the data" in hit.reasons[0]


def test_stale_base_is_remeasured_from_model_value():
    """The study's MSM case: a $0.01 'previous close' on a contract the model valued at ~$0.32."""
    rows = row(
        f"XYZ{EXP}C00052000",
        bid=1.4,
        ask=1.6,
        last_trade_price=1.5,
        prev_day_close=0.01,
        volume=300,
        open_interest=500,
    )
    (hit,) = OptionsRadar(ocfg()).scan(chain_with(rows, price=51.0, prev=50.0, iv30=30), li("XYZ", 5e9), NOW)
    top = hit.spikes[0]
    assert top.move_pct == pytest.approx(14900.0)  # what a screenshot badge would show
    assert top.stale_base and top.fair_prev == pytest.approx(bs_price(50.0, 52.0, 10 / 365, 0.30), abs=1e-9)
    assert top.honest_move_pct == pytest.approx(1.5 / top.fair_prev * 100 - 100, abs=0.5)
    assert top.honest_move_pct < 1000 and hit.severity == "MEDIUM"
    assert "stale base" in hit.reasons[0] and "$0.01" in hit.reasons[0] and "+366%" in hit.reasons[0]
    assert "the bid ($1.40) is only +335% above the base" in hit.reasons[1]


def test_stale_base_that_still_moved_thousands_pushes():
    """Same stale $0.01 base, but the stock ran 18%: from the $0.32 model value it is still
    +2,300%, so it is real and it pushes, quoted from the model value."""
    rows = row(
        f"XYZ{EXP}C00052000",
        bid=7.6,
        ask=8.0,
        last_trade_price=7.8,
        prev_day_close=0.01,
        volume=300,
        open_interest=500,
    )
    (hit,) = OptionsRadar(ocfg()).scan(chain_with(rows, price=59.0, prev=50.0, iv30=30), li("XYZ", 5e9), NOW)
    top = hit.spikes[0]
    assert top.stale_base and top.honest_move_pct == pytest.approx(7.8 / top.fair_prev * 100 - 100)
    assert top.honest_move_pct > 2000 and hit.severity == "HIGH" and hit.reasons == []


@pytest.mark.parametrize(
    "change",
    [
        {"last_trade_price": 1.4},  # +833%: below 1,000%
        {"volume": 10},  # too few contracts
        {"last_trade_price": 0.09, "prev_day_close": 0.005, "bid": 0.08},  # sub-dime print
        {"volume": 30, "last_trade_price": 1.6, "prev_day_close": 0.1},  # $4,800 traded < $5,000
        {"last_trade_time": "2026-10-06T15:59:00"},  # the "last" is yesterday's print
    ],
)
def test_spike_filters(change):
    assert (
        OptionsRadar(ocfg()).scan(chain_with(row(f"XYZ{EXP}C00060000", **{**SPIKE, **change})), None, NOW)
        == []
    )


def test_expired_contracts_are_ignored():
    assert OptionsRadar(ocfg()).scan(chain_with(row("XYZ261002C00060000", **SPIKE)), None, NOW) == []


def test_spike_ladder_fires_once_per_rung_and_survives_restart(tmp_path: Path):
    path = tmp_path / "options_state.json"
    r = OptionsRadar(ocfg(), state_path=path)
    occ = f"XYZ{EXP}C00060000"
    assert len(r.scan(chain_with(row(occ, **SPIKE)), None, NOW)) == 1
    assert (
        r.scan(chain_with(row(occ, **{**SPIKE, "last_trade_price": 2.2, "bid": 2.1})), None, NOW + 60) == []
    )
    r2 = OptionsRadar(ocfg(), state_path=path)  # a restart remembers the rung
    assert r2.scan(chain_with(row(occ, **SPIKE)), None, NOW + 120) == []
    (hit,) = r2.scan(
        chain_with(row(occ, **{**SPIKE, "last_trade_price": 3.5, "bid": 3.3, "ask": 3.7})), None, NOW + 180
    )
    assert hit.rung == 2000.0 and hit.severity == "HIGH"
    nxt = datetime(2026, 10, 8, 10, 0, tzinfo=ET).timestamp()  # a new day starts over
    assert (
        len(r2.scan(chain_with(row(occ, **{**SPIKE, "last_trade_time": "2026-10-08T09:59:00"})), None, nxt))
        == 1
    )


def test_unconfirmed_then_confirmed_upgrades_to_a_push():
    r = OptionsRadar(ocfg())
    occ = f"XYZ{EXP}C00060000"
    (a,) = r.scan(chain_with(row(occ, **{**SPIKE, "bid": 0.05})), None, NOW)
    assert a.severity == "MEDIUM"
    (b,) = r.scan(chain_with(row(occ, **SPIKE)), None, NOW + 60)
    assert b.severity == "HIGH"


def test_daily_push_cap():
    r = OptionsRadar(ocfg(daily_pushes=1))
    (a,) = r.scan(chain_with(row(f"XYZ{EXP}C00060000", **SPIKE)), None, NOW)
    ch = parse_chain(doc("QQQX", 52.0, 50.0, [row(f"QQQX{EXP}C00060000", **SPIKE)]), "QQQX")
    (b,) = r.scan(ch, None, NOW + 1)
    assert (a.severity, b.severity) == ("HIGH", "MEDIUM") and "push limit" in b.reasons[0]


def test_puts_spike_too():
    rows = row(
        f"XYZ{EXP}P00040000",
        bid=2.9,
        ask=3.1,
        last_trade_price=3.0,
        prev_day_close=0.2,
        volume=500,
        open_interest=50,
    )
    (hit,) = OptionsRadar(ocfg()).scan(chain_with(rows, price=37.0, prev=45.0), None, NOW)
    assert hit.spikes[0].contract.kind == "put" and hit.severity == "HIGH"


def test_threshold_is_configurable():
    cfg = ocfg(spike_min_pct=500)
    (hit,) = OptionsRadar(cfg).scan(
        chain_with(row(f"XYZ{EXP}C00060000", **{**SPIKE, "last_trade_price": 1.0, "bid": 0.95, "ask": 1.05})),
        None,
        NOW,
    )
    assert hit.rung == 500.0


# --------------------------------------------------------------------------- unusual volume


FLOW = dict(bid=1.95, ask=2.05, last_trade_price=2.05, prev_day_close=1.8, volume=6000, open_interest=500)


def test_unusual_volume_pushes_with_premium_mix_and_side():
    rows = [
        row(f"XYZ{EXP}C00055000", **FLOW),
        row(f"XYZ{EXP}P00045000", **{**FLOW, "volume": 1000, "open_interest": 100, "last_trade_price": 1.95}),
    ]
    (hit,) = OptionsRadar(ocfg()).scan(chain_with(*rows), li("XYZ", 3e9), NOW)
    assert hit.kind == "flow" and hit.severity == "HIGH"
    assert hit.flow_premium == pytest.approx(6000 * 2.05 * 100 + 1000 * 1.95 * 100)
    assert hit.call_premium == pytest.approx(6000 * 2.05 * 100) and hit.put_premium == pytest.approx(
        1000 * 1.95 * 100
    )
    top = hit.flow[0]
    assert top.vol_oi == pytest.approx(12.0) and top.contract.side() == "near the ask (likely bought)"
    assert hit.flow[1].contract.side() == "near the bid (likely sold)"


@pytest.mark.parametrize(
    "change",
    [
        {"volume": 400},  # under 500 contracts
        {"open_interest": 2500},  # 6,000 vs 2,500 open: only 2.4x
        {
            "volume": 600,
            "last_trade_price": 1.5,
            "bid": 1.45,
            "ask": 1.55,
            "open_interest": 0,
        },  # $90K < $100K
    ],
)
def test_flow_filters(change):
    assert (
        OptionsRadar(ocfg()).scan(chain_with(row(f"XYZ{EXP}C00055000", **{**FLOW, **change})), None, NOW)
        == []
    )


def test_flow_below_company_minimum_or_push_bar():
    small = {**FLOW, "volume": 1000}  # $205K < $250K across the company
    assert OptionsRadar(ocfg()).scan(chain_with(row(f"XYZ{EXP}C00055000", **small)), None, NOW) == []
    mid = {**FLOW, "volume": 2000}  # $410K: an alert, but under the $1M push bar
    (hit,) = OptionsRadar(ocfg()).scan(chain_with(row(f"XYZ{EXP}C00055000", **mid)), None, NOW)
    assert hit.severity == "MEDIUM"


def test_flow_speaks_again_only_when_it_doubles():
    r = OptionsRadar(ocfg())
    occ = f"XYZ{EXP}C00055000"
    assert len(r.scan(chain_with(row(occ, **FLOW)), None, NOW)) == 1
    assert r.scan(chain_with(row(occ, **{**FLOW, "volume": 9000})), None, NOW + 60) == []
    (again,) = r.scan(chain_with(row(occ, **{**FLOW, "volume": 13000})), None, NOW + 120)
    assert again.severity == "MEDIUM"  # the push already went out


def test_flow_with_no_prior_open_interest():
    (hit,) = OptionsRadar(ocfg()).scan(
        chain_with(row(f"XYZ{EXP}C00055000", **{**FLOW, "open_interest": 0})), None, NOW
    )
    assert hit.flow[0].vol_oi is None and hit.severity == "HIGH"


# --------------------------------------------------------------------------- the real NWE tape


def nwe() -> dict[str, Any]:
    return json.loads((FIXTURES / "options_nwe_2026-10-07.json").read_text())


def test_replay_nwe_oct_7_from_real_bars():
    """NWE 75C (Oct 16), Oct 7 2026: the stock gapped +7.3% on 4x volume. OPRA's previous
    print was $0.15 (Sep 25), the contract traded up to $3.40 and closed at $1.10.

    Daily bars carry no bid/ask, no open interest and no intraday volume, so: the day's
    volume (288) stands in for the volume at the high, the bid is left unknown, and the
    company's implied vol is the study's proxy (20-day realized vol + 10 points)."""
    d = nwe()
    stock = {b["date"]: b for b in d["stock"]}
    opt = {b["date"]: b for b in d["option"]}
    closes = [b["close"] for b in d["stock"] if b["date"] <= "2026-10-06"][-21:]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    rv = statistics.pstdev(rets) * math.sqrt(252)
    assert rv == pytest.approx(0.166, abs=0.002)  # the study: 16%
    prev_trade = [b for b in d["option"] if b["date"] < "2026-10-07"][-1]
    assert (prev_trade["date"], prev_trade["close"]) == ("2026-09-25", 0.15)

    def at(last: float) -> list:
        c = Contract(
            d["contract"], "NWE", date(2026, 10, 16), "C", 75.0,
            bid=None, ask=None, last=last, prev_close=prev_trade["close"],
            volume=opt["2026-10-07"]["volume"], last_trade=datetime(2026, 10, 7, 11, 0, tzinfo=ET),
        )  # fmt: skip
        ch = Chain(
            "NWE", price=stock["2026-10-07"]["close"], prev_close=stock["2026-10-06"]["close"],
            change_pct=(stock["2026-10-07"]["close"] / stock["2026-10-06"]["close"] - 1) * 100,
            iv30=rv + 0.10, contracts=[c],
        )  # fmt: skip
        listing = Listing("NWE", d["name"], market_cap=d["market_cap"])
        return OptionsRadar(ocfg()).scan(ch, listing, NOW)

    (hit,) = at(opt["2026-10-07"]["high"])  # $0.15 -> $3.40
    top = hit.spikes[0]
    assert top.move_pct == pytest.approx(2166.7, abs=0.1) and hit.rung == 2000.0
    assert not top.stale_base and top.fair_prev == pytest.approx(0.03, abs=0.01)  # $0.15 was a real price
    assert hit.severity == "MEDIUM" and "no bid in the data" in hit.reasons[0]  # live data has the bid
    assert hit.to_dict()["band"] == "mid cap"
    assert at(opt["2026-10-07"]["close"]) == []  # at the close: +633%, under the 1,000% line


# --------------------------------------------------------------------------- the feed


def uni(*listings: Listing) -> Universe:
    return Universe(listings, loaded_at=time.time(), source="test")


def test_rotation_is_largest_first_and_skips_small_and_non_common():
    u = uni(
        li("BIG", 900e9),
        li("MID", 5e9),
        li("SMALL", 400e6),
        Listing("WRT", "W", 3e9, common=False),
        li("ONEB", 1e9),
    )
    f = OptionsFeed(ocfg(), None, u, OptionsRadar(ocfg()))  # type: ignore[arg-type]
    assert [x.symbol for x in f.companies()] == ["BIG", "MID", "ONEB"]
    assert f.next_batch(NOW, 2) == ["BIG", "MID"] and f.next_batch(NOW, 2) == ["ONEB"]


def test_hot_names_come_first_and_respect_their_interval():
    u = uni(li("BIG", 900e9, 0.5), li("MOVER", 3e9, -7.0), li("NEWS", 2e9, 0.1), li("QUIET", 2e9, 1.0))
    f = OptionsFeed(ocfg(hot_seconds=180), None, u, OptionsRadar(ocfg()))  # type: ignore[arg-type]
    f.prioritize(["NEWS", "TINY", ""])
    now = time.time()
    assert f.hot(now) == ["NEWS", "MOVER"] and "TINY" not in f.priority
    f.movers = lambda: ["QUIET", "NOPE"]  # the price radar's board
    assert f.hot(now) == ["NEWS", "QUIET", "MOVER"]
    f.movers = lambda: 1 / 0  # a broken radar never stops the feed
    assert f.hot(now) == ["NEWS", "MOVER"]
    f.movers = None
    assert f.next_batch(now, 3) == ["NEWS", "MOVER", "BIG"]  # scheduled counts as read
    assert f.hot(now + 60) == [] and f.hot(now + 200) == ["NEWS", "MOVER"]


async def test_feed_reads_chains_and_handles_missing_and_stale(server: Recorder, http: HttpClient):
    fresh = doc("XYZ", 52.0, 50.0, [row(f"XYZ{EXP}C00060000", **SPIKE)])
    server.on("/XYZ.json", fresh)
    server.on("/OLD.json", doc("OLD", 52.0, 50.0, [], last_trade_time="2026-10-07T09:31:00"))
    server.on("/NONE.json", (403, "<Error>AccessDenied</Error>"))
    server.on("/BRK.B.json", doc("BRK-B", 480.0, 470.0, []))
    cfg = ocfg(chain_url=server.url("/") + "{symbol}.json", request_interval_s=0.2)
    u = uni(li("XYZ", 5e9), li("OLD", 5e9), li("NONE", 5e9), li("BRK-B", 900e9))
    f = OptionsFeed(cfg, http, u, OptionsRadar(cfg))
    (hit,) = await f.read("XYZ", NOW)
    assert hit.severity == "HIGH" and hit.listing is not None and hit.listing.symbol == "XYZ"
    assert await f.read("OLD", NOW) == [] and f.stale_chains == 1 and "09:31" in f.status()["last_stale"]
    assert await f.read("NONE", NOW) == [] and "NONE" in f.no_chain and f.health.errors == 0
    assert "NONE" not in [x.symbol for x in f.companies()]  # not retried until tomorrow
    assert await f.read("BRK-B", NOW) == [] and server.requests[-1]["path"] == "/BRK.B.json"
    st = f.status()
    assert st["chains_scanned"] == 2 and st["companies"] == 3 and st["source"].startswith("Cboe")


def test_freshness_tolerates_a_timezone_shift_but_not_a_frozen_feed():
    f = OptionsFeed(ocfg(), None, uni(), OptionsRadar(ocfg()))  # type: ignore[arg-type]

    def ch(hh: int, mm: int) -> Chain:
        return Chain("X", quote_time=datetime(2026, 10, 7, hh, mm, tzinfo=ET))

    # Cboe stamping Central time: every chain reads an hour behind the Eastern clock
    assert f.fresh(ch(10, 29), NOW) and f.fresh(ch(10, 25), NOW)
    assert not f.fresh(ch(9, 40), NOW)  # 49 min behind the newest chain seen: frozen
    assert not OptionsFeed(ocfg(), None, uni(), OptionsRadar(ocfg())).fresh(ch(9, 0), NOW)  # type: ignore[arg-type]
    assert f.fresh(Chain("X"), NOW)  # no timestamp: judged by the per-contract trade times


def test_feed_problems_are_reported_once_a_day():
    f = OptionsFeed(ocfg(), None, uni(), OptionsRadar(ocfg()))  # type: ignore[arg-type]
    assert f.problem(NOW) == ""
    for _ in range(10):
        f._error(ValueError("bad json"))
    note = f.problem(NOW)
    assert "failed 10 chain reads in a row" in note and "bad json" in note and "Cboe may be blocking" in note
    assert f.problem(NOW + 60) == ""  # once a day
    f.health.consecutive_errors, f._stale_run, f._stale_warned = 0, 25, "XYZ: last quote 09:31 ET"
    assert "skipped 25 chains in a row as stale (XYZ: last quote 09:31 ET)" in f.problem(NOW + 86400)


async def test_feed_backs_off_on_rate_limits(server: Recorder, http: HttpClient):
    server.on("/XYZ.json", (429, "slow down", {"Retry-After": "120"}))
    cfg = ocfg(chain_url=server.url("/") + "{symbol}.json", request_interval_s=0.2)
    f = OptionsFeed(cfg, http, uni(li("XYZ", 5e9)), OptionsRadar(cfg))
    assert await f.read("XYZ", NOW) == []
    assert f.health.errors == 1 and f.status()["backing_off_s"] >= 60


async def test_feed_run_loop_only_in_session(server: Recorder, http: HttpClient):
    server.on("/XYZ.json", doc("XYZ", 52.0, 50.0, [row(f"XYZ{EXP}C00060000", **SPIKE)]))
    cfg = ocfg(chain_url=server.url("/") + "{symbol}.json", request_interval_s=0.2)
    phase = {"now": "closed"}
    f = OptionsFeed(cfg, http, uni(li("XYZ", 5e9)), OptionsRadar(cfg), market_phase=lambda now: phase["now"])
    f.fresh = lambda chain, now: True  # type: ignore[method-assign]  # the fixture's clock is Oct 7
    f.radar.scan = lambda ch, listing, now: OptionsRadar(cfg).scan(ch, listing, NOW)  # type: ignore[method-assign]
    got: list = []

    async def on_hits(hits):
        got.extend(hits)
        stop.set()

    stop = asyncio.Event()
    task = asyncio.create_task(f.run(on_hits, stop))
    await asyncio.sleep(0.2)
    assert server.requests == []  # market closed: nothing read
    phase["now"] = "open"
    stop.set()
    await task
    stop = asyncio.Event()
    await asyncio.wait_for(f.run(on_hits, stop), 5)
    assert len(got) == 1 and got[0].symbol == "XYZ"


# --------------------------------------------------------------------------- config


def test_config_defaults_and_validation():
    c = build_config({}).options
    assert c.enabled and c.min_market_cap == 1e9 and c.spike_min_pct == 1000 and "{symbol}" in c.chain_url
    assert (
        build_config({"options": {"spike_min_pct": "2500", "enabled": "false"}}).options.spike_min_pct == 2500
    )
    for bad in (
        {"request_interval_s": 0.05},
        {"spike_confirm_fraction": 0},
        {"chain_url": "https://x/y.json"},
        {"nope": 1},
    ):
        with pytest.raises(ConfigError):
            build_config({"options": bad})


# --------------------------------------------------------------------------- engine


def engine(tmp_path: Path, **opts: Any) -> tuple[Engine, CaptureNotifier]:
    from news247.notify import Dispatcher

    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "notify": {"console": {"enabled": False}},
            "web": {"token": "tok"},
            "options": opts,
        }
    )
    Universe.stub(
        {"XYZ": {"name": "Xylo Zinc Corp", "mcap": 5e9}, "NWE": {"name": "NorthWestern", "mcap": 4.6e9}}
    ).save(tmp_path / "universe.json.gz")
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    cap = CaptureNotifier()
    disp.channels = [cap]
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), dispatcher=disp, sources=[])
    return eng, cap


async def test_engine_publishes_spikes_and_flow(tmp_path: Path):
    eng, cap = engine(tmp_path)
    assert eng.options_feed is not None and eng.options_radar is not None
    rows = [row(f"XYZ{EXP}C00060000", **SPIKE), row(f"XYZ{EXP}C00055000", **FLOW)]
    hits = eng.options_radar.scan(chain_with(*rows), eng.universe.get("XYZ"), NOW)
    assert [h.kind for h in hits] == ["spike", "flow"]
    await eng.on_options(hits)
    await asyncio.sleep(0.05)  # dispatch runs as a task
    spike, flow = cap.alerts
    assert spike.kind == "options" and spike.severity == Severity.HIGH
    assert spike.title.startswith("🔥 XYZ $60 call (Oct 16) +1,233% today · Xylo Zinc Corp · $5.0B mid cap")
    first = spike.body.split("\n")[0]
    assert (
        "$0.15 → $2.00 (bid $1.90 / ask $2.10) · 400 contracts ($80K)" in first
        and "XYZ $52.00 (+4.00%)" in first
    )
    assert "Cboe delayed quotes (~15 min) · quotes as of 11:29 ET" in spike.body
    assert "No headline yet." in spike.body and spike.url.endswith("/xyz/quote_table")
    assert flow.title.startswith(
        "🐋 Unusual options: XYZ $1.2M · XYZ $55 call (Oct 16) 6,000 contracts, 12× open interest"
    )
    assert "near the ask (likely bought)" in flow.body and flow.edge["options"]["kind"] == "flow"
    assert eng.storage.get_alert(spike.id)["kind"] == "options"


async def test_unconfirmed_spike_is_labelled(tmp_path: Path):
    eng, _ = engine(tmp_path)
    (hit,) = eng.options_radar.scan(chain_with(row(f"XYZ{EXP}C00060000", **{**SPIKE, "bid": 0.0})), None, NOW)
    alert = eng._options_alert(hit)
    assert (
        "(unconfirmed)" in alert.title and "⚠ one-sided" in alert.body and alert.severity == Severity.MEDIUM
    )


async def test_real_spike_from_a_stale_base_says_how_it_was_measured(tmp_path: Path):
    eng, _ = engine(tmp_path)
    rows = row(
        f"XYZ{EXP}C00052000",
        bid=7.6,
        ask=8.0,
        last_trade_price=7.8,
        prev_day_close=0.01,
        volume=300,
        open_interest=500,
    )
    (hit,) = eng.options_radar.scan(
        chain_with(rows, price=59.0, prev=50.0, iv30=30), eng.universe.get("XYZ"), NOW
    )
    alert = eng._options_alert(hit)
    assert alert.severity == Severity.HIGH and "+2,324% today" in alert.title  # not the +77,900% badge
    assert "The $0.01 previous close looks stale (model value that day $0.32)" in alert.body


def test_board_stays_bounded():
    r = OptionsRadar(ocfg(daily_pushes=0))
    for i in range(520):
        sym = (
            f"S{i:03d}".replace("0", "A")
            .replace("1", "B")
            .replace("2", "C")
            .replace("3", "D")
            .replace("4", "E")
        )
        sym = "".join(ch if ch.isalpha() else chr(65 + int(ch)) for ch in sym)
        ch = parse_chain(doc(sym, 52.0, 50.0, [row(f"{sym}{EXP}C00060000", **SPIKE)]), sym)
        assert len(r.scan(ch, None, NOW + i)) == 1
    assert len(r.board) <= 500 and r.snapshot(3)[0]["detected"] == NOW + 519


async def test_feed_problem_becomes_one_system_alert(tmp_path: Path):
    eng, cap = engine(tmp_path)
    await eng.on_options_problem("The options tape has failed 10 chain reads in a row")
    await asyncio.sleep(0.05)
    (alert,) = cap.alerts
    assert alert.kind == "system" and alert.severity == Severity.HIGH and "failed 10" in alert.body


async def test_news_and_radar_alerts_put_tickers_at_the_front(tmp_path: Path):
    eng, _ = engine(tmp_path)
    await eng.publish(Alert(kind="news", severity=Severity.LOW, title="t", body="", tickers=["XYZ", "NOPE"]))
    assert "XYZ" in eng.options_feed.priority and "NOPE" not in eng.options_feed.priority


def test_options_off_or_without_universe_feed(tmp_path: Path):
    eng, _ = engine(tmp_path, enabled=False)
    assert eng.options_feed is None and eng.status()["options"] == {"enabled": False}
    assert eng.options_board()["enabled"] is False


async def test_api_options(tmp_path: Path):
    eng, _ = engine(tmp_path)
    eng.options_radar.scan(chain_with(row(f"XYZ{EXP}C00060000", **SPIKE)), eng.universe.get("XYZ"), NOW)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/options")) as r:
                assert r.status == 401
            async with s.get(srv.make_url("/api/options?token=tok&limit=5")) as r:
                body = await r.json()
            assert body["enabled"] and body["rows"][0]["symbol"] == "XYZ" and body["status"]["companies"] == 2
            async with s.get(srv.make_url("/api/status?token=tok")) as r:
                st = await r.json()
            assert st["options"]["source"].startswith("Cboe")
    finally:
        await srv.close()
        await eng.http.close()


async def test_cli_reads_chains_once(server: Recorder, tmp_path: Path, capsys):
    from news247.cli import _options

    server.on(
        "/XYZ.json", doc("XYZ", 52.0, 50.0, [row("XYZ301220C00060000", **SPIKE)])
    )  # Dec 2030: never expires in CI
    server.on("/NONE.json", (404, "nope"))
    server.on("/BAD.json", (500, "boom"))
    cfg = build_config(
        {"general": {"data_dir": str(tmp_path)}, "options": {"chain_url": server.url("/") + "{symbol}.json"}}
    )
    Universe.stub({"XYZ": {"name": "Xylo Zinc Corp", "mcap": 5e9}}).save(tmp_path / "universe.json.gz")
    assert await _options(cfg, ["xyz", "NONE", "BAD"], 500) == 1  # BAD failed
    out = capsys.readouterr().out
    assert "options tape: 1 companies of $1.0B+" in out
    assert "XYZ $60 call (Dec 20)" in out and "+1,233%" in out and "vol 400" in out
    assert "no option chain listed" in out and "failed: HTTPError" in out
