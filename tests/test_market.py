from __future__ import annotations

import json

import pytest

from news247.config import GroupRule, MoveRule, build_config
from news247.market.detector import MoveDetector
from news247.market.prices import PriceMonitor, parse_chart, parse_spark
from news247.models import Severity

T0 = 1_791_490_000.0


def detector(**market) -> MoveDetector:
    base = {
        "rules": [{"window": 60, "pct": 1.5}, {"window": 300, "pct": 3.0}],
        "groups": {},
        "cooldown_minutes": 20,
    }
    cfg = build_config({"market": {**base, **market}})
    return MoveDetector(cfg.market)


def flat(det: MoveDetector, sym: str, price: float, seconds: int = 600, step: int = 15) -> None:
    det.seed(sym, [(T0 - seconds + k, price) for k in range(0, seconds + 1, step)])


def test_single_stock_drop_fires_once_with_cooldown():
    det = detector()
    flat(det, "NVDA", 100.0)
    assert det.update("NVDA", 99.5, T0 + 15) == []
    moves = det.update("NVDA", 98.0, T0 + 30)  # -2% within 60s
    assert len(moves) == 1
    mv = moves[0]
    assert mv.symbol == "NVDA" and mv.window_s == 60 and mv.change_pct == pytest.approx(-2.0)
    assert mv.direction == "down" and not mv.is_group
    assert det.severity(mv) is Severity.HIGH
    # a little worse: still cooling down
    assert det.update("NVDA", 97.6, T0 + 45) == []
    # 1.5x worse than the alerted move -> escalation
    esc = det.update("NVDA", 96.5, T0 + 60)
    assert esc and abs(esc[0].change_pct) >= 3.0


def test_largest_ratio_rule_wins_and_critical():
    det = detector()
    flat(det, "AMD", 100.0)
    moves = det.update("AMD", 93.0, T0 + 30)
    assert len(moves) == 1
    # -7% in 60s is 4.7x the 60s rule vs 2.3x the 300s rule
    assert moves[0].window_s == 60 and det.severity(moves[0]) is Severity.CRITICAL


def test_needs_enough_history():
    det = detector()
    det.update("NEW", 100.0, T0)
    assert det.update("NEW", 80.0, T0 + 5) == []  # 5s of history can't satisfy a 60s window


def test_out_of_order_and_bad_ticks_ignored():
    det = detector()
    flat(det, "X", 50.0)
    assert det.update("X", 0, T0 + 1) == []
    assert det.update("X", 10.0, T0 - 1000) == []
    assert det.last_price["X"][1] == 50.0


def test_relative_to_benchmark():
    det = detector()
    flat(det, "SPY", 500.0)
    flat(det, "CRM", 300.0)
    det.update("SPY", 497.5, T0 + 30)  # -0.5%
    mv = det.update("CRM", 291.0, T0 + 31)[0]  # -3%
    assert mv.relative_pct == pytest.approx(-2.5, abs=0.05)


def test_index_override_thresholds():
    det = detector()
    flat(det, "SPY", 500.0)
    moves = det.update("SPY", 497.0, T0 + 30)  # -0.6% in 60s: big for the index
    assert moves and moves[0].symbol == "SPY"


def test_day_move_once_per_day():
    det = detector(day_move_pct=7.0)
    det.set_prev_close("TSLA", 200.0)
    flat(det, "TSLA", 186.0)
    moves = det.update("TSLA", 185.0, T0 + 100)
    day = [m for m in moves if m.window_s == 86400]
    assert day and day[0].change_pct == pytest.approx(-7.5)
    assert det.severity(day[0]) is Severity.MEDIUM
    assert [m for m in det.update("TSLA", 184.0, T0 + 200) if m.window_s == 86400] == []


def test_group_move_detection():
    det = detector()
    det.cfg.groups = [GroupRule("software", ["A", "B", "C", "D", "E"], pct=2.0, window=600, min_members=3)]
    for s in "ABCDE":
        flat(det, s, 100.0, seconds=900)
    for s, p in zip("ABCDE", [97.0, 97.5, 96.0, 99.5, 97.2]):
        det.update(s, p, T0 + 10)
    moves = det.check_groups(T0 + 10)
    assert len(moves) == 1
    g = moves[0]
    assert g.symbol == "GROUP:software" and g.is_group and g.change_pct == pytest.approx(-2.56, abs=0.01)
    assert list(g.members)[0] == "C"  # biggest loser first
    assert det.severity(g) is Severity.HIGH
    assert det.check_groups(T0 + 20) == []  # cooldown


def test_group_requires_breadth():
    det = detector()
    det.cfg.groups = [GroupRule("g", ["A", "B", "C", "D"], pct=2.0, window=600, min_members=3)]
    for s in "ABCD":
        flat(det, s, 100.0, seconds=900)
    det.update("A", 80.0, T0 + 10)  # one stock crashing drags the average but isn't a sector move
    for s in "BCD":
        det.update(s, 100.0, T0 + 10)
    assert det.check_groups(T0 + 10) == []


def test_stale_quotes_excluded_from_groups():
    det = detector()
    det.cfg.groups = [GroupRule("g", ["A", "B", "C"], pct=2.0, window=600, min_members=3)]
    for s in "ABC":
        flat(det, s, 100.0, seconds=900)
        det.update(s, 95.0, T0 + 10)
    assert det.check_groups(T0 + 5000) == []


def test_snapshot_and_rules_for():
    det = detector()
    flat(det, "AAPL", 200.0)
    det.update("AAPL", 202.0, T0 + 30)
    snap = det.snapshot()["AAPL"]
    assert snap["price"] == 202.0 and snap["chg_1m"] == pytest.approx(1.0)
    assert det.rules_for("AAPL") == [MoveRule(60, 1.5), MoveRule(300, 3.0)]


def test_tick_bursts_are_downsampled():
    det = detector()
    for k in range(1000):
        det.update("NVDA", 100.0 + (k % 3) * 0.01, T0 + k * 0.01)  # 1000 trades in 10s
    assert len(det.history["NVDA"]) <= 12


# --------------------------------------------------------------------------- feeds


def test_parse_spark_v8_and_v7():
    v8 = {
        "NVDA": {
            "symbol": "NVDA",
            "timestamp": [1, 2, 3],
            "close": [10.0, None, 11.0],
            "chartPreviousClose": 9.5,
        }
    }
    out = parse_spark(v8)
    assert out["NVDA"]["points"] == [(1.0, 10.0), (3.0, 11.0)] and out["NVDA"]["prev_close"] == 9.5
    v7 = {
        "spark": {
            "result": [
                {
                    "symbol": "AAPL",
                    "response": [
                        {
                            "meta": {"regularMarketPrice": 5.0, "chartPreviousClose": 4.0},
                            "timestamp": [1],
                            "indicators": {"quote": [{"close": [5.0]}]},
                        }
                    ],
                }
            ]
        }
    }
    out = parse_spark(v7)
    assert out["AAPL"]["price"] == 5.0 and out["AAPL"]["points"] == [(1.0, 5.0)]


def test_parse_chart():
    data = {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": 7.0, "previousClose": 6.0, "regularMarketTime": 100},
                    "timestamp": [50, 60],
                    "indicators": {"quote": [{"close": [6.5, 7.0]}]},
                }
            ]
        }
    }
    snap = parse_chart(data)
    assert snap["points"][-1] == (60.0, 7.0) and snap["prev_close"] == 6.0
    assert parse_chart({"chart": {"result": None}}) is None


def test_price_monitor_ingest_seeds_then_detects():
    cfg = build_config({"market": {"rules": [{"window": 60, "pct": 1.5}], "groups": {}}})
    det = MoveDetector(cfg.market)
    pm = PriceMonitor(cfg.market, None, det)  # type: ignore[arg-type]
    pts = [(T0 - 600 + 60 * k, 100.0) for k in range(10)] + [(T0 - 30, 100.0)]
    assert pm.ingest("CRM", {"points": pts, "prev_close": 101.0}, T0) == []
    assert det.prev_close["CRM"] == 101.0
    moves = pm.ingest("CRM", {"points": pts[:-1] + [(T0 - 30, 97.0)], "prev_close": 101.0}, T0 + 15)
    assert moves and moves[0].change_pct < -2.5


def test_price_monitor_closed_market_does_not_repeat():
    cfg = build_config({"market": {"groups": {}}})
    det = MoveDetector(cfg.market)
    pm = PriceMonitor(cfg.market, None, det)  # type: ignore[arg-type]
    pts = [(T0 - 7200 + 60 * k, 100.0) for k in range(5)]
    pm.ingest("X", {"points": pts, "prev_close": 100.0}, T0)
    n = len(det.history["X"])
    pm.ingest("X", {"points": pts, "prev_close": 100.0}, T0 + 15)
    assert len(det.history["X"]) == n  # stale bar not re-added with a fresh timestamp


def test_finnhub_message_handling():
    cfg = build_config({"market": {"rules": [{"window": 60, "pct": 1.5}], "groups": {}}})
    det = MoveDetector(cfg.market)
    pm = PriceMonitor(cfg.market, None, det)  # type: ignore[arg-type]
    det.seed("NVDA", [(T0 - 120 + k, 100.0) for k in range(0, 121, 10)])
    msg = {
        "type": "trade",
        "data": [
            {"s": "NVDA", "p": 97.5, "t": (T0 + 30) * 1000, "v": 10},
            {"s": "NVDA", "p": 97.0, "t": (T0 + 31) * 1000, "v": 5},
        ],
    }
    moves = pm.handle_finnhub_message(json.dumps(msg))
    assert moves and moves[0].price == 97.0
    assert pm.handle_finnhub_message('{"type":"ping"}') == []


async def test_yahoo_poll_with_fallback(server, http, monkeypatch):
    import news247.market.prices as mod

    server.on("/spark", (500, "nope"))
    server.on(
        "/chart/NVDA",
        {
            "chart": {
                "result": [
                    {
                        "meta": {"previousClose": 100},
                        "timestamp": [int(T0)],
                        "indicators": {"quote": [{"close": [101.0]}]},
                    }
                ]
            }
        },
    )
    server.on(
        "/chart/SPY",
        {
            "chart": {
                "result": [
                    {
                        "meta": {"previousClose": 500},
                        "timestamp": [int(T0)],
                        "indicators": {"quote": [{"close": [501.0]}]},
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(mod, "SPARK_URL", server.url("/spark"))
    monkeypatch.setattr(mod, "CHART_URL", server.url("/chart/") + "{symbol}")
    cfg = build_config({"market": {"symbols": ["NVDA"], "groups": {}}})
    pm = PriceMonitor(cfg.market, http, MoveDetector(cfg.market))
    await pm.poll_yahoo_once()
    assert pm.mode == "chart"
    assert set(pm.detector.last_price) == {"NVDA", "SPY"}
    assert pm.detector.prev_close["NVDA"] == 100
