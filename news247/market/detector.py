"""Abnormal price-move detection — the "all the stocks are crashing" alarm.

Each price tick goes into a per-symbol rolling history. A move fires when the change over a
rule's window (e.g. 3% in 5 minutes) is reached; basket ("group") moves fire when a whole
sector moves together. Cooldowns stop repeats, but a move that keeps getting worse
(≥1.5× the last alerted size) alerts again.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass

from ..config import MarketConfig, MoveRule
from ..models import PriceMove, Severity

ESCALATION = 1.5


@dataclass
class _Last:
    ts: float
    pct: float


class MoveDetector:
    def __init__(self, cfg: MarketConfig) -> None:
        self.cfg = cfg
        self.max_window = max(
            [r.window for r in cfg.rules]
            + [g.window for g in cfg.groups]
            + [r.window for rs in cfg.overrides.values() for r in rs]
            + [900]
        )
        self.history: dict[str, deque[tuple[float, float]]] = {}
        self.prev_close: dict[str, float] = {}
        self.last_price: dict[str, tuple[float, float]] = {}
        self._last_alert: dict[str, _Last] = {}
        self._day_alerted: dict[str, str] = {}
        self._last_eval: dict[str, float] = {}

    # ------------------------------------------------------------------ data in

    def set_prev_close(self, symbol: str, price: float | None) -> None:
        if price and price > 0:
            self.prev_close[symbol] = float(price)

    def seed(self, symbol: str, points: list[tuple[float, float]]) -> None:
        """Pre-load history (e.g. today's 1-minute bars) without generating alerts."""
        dq = self.history.setdefault(symbol, deque())
        for ts, price in sorted(points):
            if price and price > 0 and (not dq or ts > dq[-1][0]):
                dq.append((ts, float(price)))
        if dq:
            self.last_price[symbol] = (dq[-1][0], dq[-1][1])
            self._trim(symbol, dq[-1][0])

    def update(self, symbol: str, price: float, ts: float | None = None) -> list[PriceMove]:
        """Record a tick and return any single-symbol moves it triggers."""
        if not price or price <= 0:
            return []
        ts = ts or time.time()
        dq = self.history.setdefault(symbol, deque())
        if dq and ts < dq[-1][0]:
            return []  # out-of-order tick
        if dq and ts - dq[-1][0] < 1.0:
            # keep at most ~1 point/second: fold bursts of trades into the latest sample
            dq[-1] = (dq[-1][0], float(price))
        else:
            dq.append((ts, float(price)))
        self.last_price[symbol] = (ts, float(price))
        self._trim(symbol, ts)
        if ts - self._last_eval.get(symbol, 0.0) < 1.0:
            return []
        self._last_eval[symbol] = ts
        moves = []
        mv = self._check_rules(symbol, ts)
        if mv:
            moves.append(mv)
        day = self._check_day(symbol, ts)
        if day:
            moves.append(day)
        return moves

    def _trim(self, symbol: str, now: float) -> None:
        dq = self.history[symbol]
        cutoff = now - self.max_window - 120
        while len(dq) > 2 and dq[1][0] < cutoff:
            dq.popleft()

    # ------------------------------------------------------------------ queries

    def price_at(self, symbol: str, ts: float, tolerance: float) -> float | None:
        """Price at (or just before) ``ts``; None if history doesn't reach back that far."""
        dq = self.history.get(symbol)
        if not dq or dq[0][0] > ts + tolerance:
            return None
        times = [p[0] for p in dq]
        i = bisect_left(times, ts)
        if i < len(dq) and dq[i][0] == ts:
            return dq[i][1]
        if i == 0:
            return dq[0][1]
        return dq[i - 1][1]

    def change(self, symbol: str, window: int, now: float | None = None) -> float | None:
        last = self.last_price.get(symbol)
        if not last:
            return None
        now = now or last[0]
        ref = self.price_at(symbol, now - window, tolerance=window * 0.25)
        if not ref:
            return None
        return (last[1] / ref - 1.0) * 100.0

    def day_change(self, symbol: str) -> float | None:
        last, pc = self.last_price.get(symbol), self.prev_close.get(symbol)
        if not last or not pc:
            return None
        return (last[1] / pc - 1.0) * 100.0

    def snapshot(self) -> dict[str, dict[str, float | None]]:
        out = {}
        for sym, (ts, price) in sorted(self.last_price.items()):
            out[sym] = {
                "price": price,
                "ts": ts,
                "chg_1m": self.change(sym, 60),
                "chg_5m": self.change(sym, 300),
                "chg_15m": self.change(sym, 900),
                "chg_day": self.day_change(sym),
            }
        return out

    # ------------------------------------------------------------------ rules

    def rules_for(self, symbol: str) -> list[MoveRule]:
        return self.cfg.overrides.get(symbol) or self.cfg.rules

    def _cooldown_ok(self, key: str, pct: float, now: float) -> bool:
        last = self._last_alert.get(key)
        if last is None or now - last.ts >= self.cfg.cooldown_minutes * 60:
            return True
        # same direction and much bigger -> escalate
        return (pct > 0) == (last.pct > 0) and abs(pct) >= abs(last.pct) * ESCALATION

    def _check_rules(self, symbol: str, now: float) -> PriceMove | None:
        best: tuple[float, MoveRule, float, float] | None = None  # (ratio, rule, pct, ref)
        last_price = self.last_price[symbol][1]
        for rule in self.rules_for(symbol):
            ref = self.price_at(symbol, now - rule.window, tolerance=rule.window * 0.25)
            if not ref:
                continue
            pct = (last_price / ref - 1.0) * 100.0
            ratio = abs(pct) / rule.pct
            if ratio >= 1.0 and (best is None or ratio > best[0]):
                best = (ratio, rule, pct, ref)
        if best is None:
            return None
        ratio, rule, pct, ref = best
        if not self._cooldown_ok(symbol, pct, now):
            return None
        self._last_alert[symbol] = _Last(now, pct)
        rel = None
        bench = self.cfg.benchmark
        if symbol != bench:
            bpct = self.change(bench, rule.window, now)
            if bpct is not None:
                rel = pct - bpct
        return PriceMove(symbol, round(pct, 3), rule.window, last_price, ref, detected=now, relative_pct=rel)

    def _check_day(self, symbol: str, now: float) -> PriceMove | None:
        pct = self.day_change(symbol)
        if pct is None or abs(pct) < self.cfg.day_move_pct:
            return None
        day = time.strftime("%Y-%m-%d", time.gmtime(now - 4 * 3600))  # roughly the US trading date
        key = f"{symbol}:{'up' if pct > 0 else 'down'}"
        if self._day_alerted.get(key) == day:
            return None
        self._day_alerted[key] = day
        price = self.last_price[symbol][1]
        return PriceMove(symbol, round(pct, 3), 86400, price, self.prev_close[symbol], detected=now)

    def check_groups(self, now: float | None = None) -> list[PriceMove]:
        now = now or time.time()
        moves = []
        for g in self.cfg.groups:
            changes: dict[str, float] = {}
            for sym in g.symbols:
                last = self.last_price.get(sym)
                if not last or now - last[0] > g.window:  # stale quote
                    continue
                c = self.change(sym, g.window, now)
                if c is not None:
                    changes[sym] = c
            if len(changes) < max(g.min_members, (len(g.symbols) + 1) // 2):
                continue
            avg = sum(changes.values()) / len(changes)
            if abs(avg) < g.pct:
                continue
            same_dir = [s for s, c in changes.items() if (c > 0) == (avg > 0) and abs(c) >= g.pct / 2]
            if len(same_dir) < g.min_members:
                continue
            key = f"GROUP:{g.name}"
            if not self._cooldown_ok(key, avg, now):
                continue
            self._last_alert[key] = _Last(now, avg)
            ordered = dict(sorted(changes.items(), key=lambda kv: kv[1], reverse=avg > 0))
            moves.append(PriceMove(key, round(avg, 3), g.window, 0.0, 0.0, detected=now, members=ordered))
        return moves

    # ------------------------------------------------------------------ severity

    def severity(self, move: PriceMove) -> Severity:
        if move.is_group:
            g = next((g for g in self.cfg.groups if f"GROUP:{g.name}" == move.symbol), None)
            ratio = abs(move.change_pct) / (g.pct if g else 2.0)
        elif move.window_s >= 86400:
            ratio = abs(move.change_pct) / self.cfg.day_move_pct
            return Severity.HIGH if ratio >= 1.5 else Severity.MEDIUM
        else:
            rule = next((r for r in self.rules_for(move.symbol) if r.window == move.window_s), None)
            ratio = abs(move.change_pct) / (rule.pct if rule else 3.0)
        return Severity.CRITICAL if ratio >= 2.0 or (move.is_group and ratio >= 1.5) else Severity.HIGH
