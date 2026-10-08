"""Nasdaq Trader trading-halts feed (covers halts on all US exchanges).

A "T1 – news pending" halt means a company is about to release material news; an "MWC"
halt is a market-wide circuit breaker. Both are among the fastest public signals there are.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..models import NewsItem, SourceTier
from ..util import struct_to_ts
from .base import PollingSource
from .rss import parse_feed

HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
ET = ZoneInfo("America/New_York")

REASONS: dict[str, tuple[str, float]] = {
    "T1": ("News Pending", 30),
    "T2": ("News Released", 12),
    "T3": ("News and Resumption Times", 8),
    "T5": ("Single Stock Trading Pause", 15),
    "T6": ("Extraordinary Market Activity", 30),
    "T7": ("Quotation Only Period", 5),
    "T8": ("ETF Halt", 15),
    "T12": ("Additional Information Requested", 25),
    "H4": ("Non-compliance", 15),
    "H9": ("Not Current in Filings", 15),
    "H10": ("SEC Trading Suspension", 35),
    "H11": ("Regulatory Concern", 25),
    "O1": ("Operations Halt", 15),
    "M": ("Volatility Trading Pause", 15),
    "M1": ("Corporate Action", 10),
    "M2": ("Quotation Not Available", 5),
    "LUDP": ("Volatility Trading Pause (LULD)", 15),
    "LUDS": ("Volatility Pause – Straddle", 12),
    "MWC1": ("MARKET-WIDE CIRCUIT BREAKER Level 1", 90),
    "MWC2": ("MARKET-WIDE CIRCUIT BREAKER Level 2", 95),
    "MWC3": ("MARKET-WIDE CIRCUIT BREAKER Level 3", 100),
    "MWC0": ("Market-wide circuit breaker carry-over", 60),
    "IPO1": ("IPO Not Yet Trading", 0),
    "IPOQ": ("IPO Quotation Period", 0),
    "IPOE": ("IPO Positioning Window Extension", 0),
    "D": ("Security Deletion", 10),
}


def _halt_ts(date_s: str, time_s: str) -> float | None:
    """Nasdaq publishes halt times in US/Eastern as MM/DD/YYYY + HH:MM:SS(.fff)."""
    if not date_s or not time_s:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(f"{date_s.strip()} {time_s.strip()}", fmt).replace(tzinfo=ET).timestamp()
        except ValueError:
            continue
    return None


class TradingHaltsSource(PollingSource):
    type_name = "halts"
    default_interval = 15.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        cfg = {"browser_headers": True, **cfg}
        super().__init__(cfg, ctx)
        self.url = cfg.get("url", HALTS_URL)
        skip = cfg.get("skip_reasons", ["IPO1", "IPOQ", "IPOE"])
        self.skip_reasons = {r.upper() for r in skip}

    async def fetch(self) -> list[NewsItem]:
        resp = await self.ctx.http.get(self.url, headers=self.headers, conditional=True)
        if resp.not_modified:
            return []
        return self.parse(resp.body)

    def parse(self, body: bytes) -> list[NewsItem]:
        parsed = parse_feed(body)
        out = []
        for e in parsed.entries:
            item = self.entry_to_item(e)
            if item:
                out.append(item)
        return out

    def entry_to_item(self, e: Any) -> NewsItem | None:
        sym = (e.get("ndaq_issuesymbol") or e.get("title") or "").strip().upper()
        if not sym:
            return None
        code = (e.get("ndaq_reasoncode") or "").strip().upper()
        if code in self.skip_reasons:
            return None
        name = (e.get("ndaq_issuename") or "").strip()
        market = (e.get("ndaq_market") or "").strip()
        hdate, htime = e.get("ndaq_haltdate", ""), e.get("ndaq_halttime", "")
        resume = (e.get("ndaq_resumptiontradetime") or "").strip()
        label, boost = REASONS.get(code, (code or "Unknown reason", 10))
        published = _halt_ts(hdate, htime) or struct_to_ts(e.get("published_parsed"))
        if code.startswith("MWC"):
            title = f"🚨 {label} — US equity trading halted market-wide"
        else:
            title = (
                f"TRADING HALT {sym} ({name}) — {label} [{code}]"
                if name
                else f"TRADING HALT {sym} — {label} [{code}]"
            )
        summary = f"Market: {market}. Halted {hdate} {htime} ET." + (
            f" Resumes {resume} ET." if resume else ""
        )
        item = self.make_item(
            title=title,
            url=f"https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts#{sym}",
            summary=summary,
            published=published,
            uid=f"halt:{sym}:{hdate}:{htime}:{code}",
            tickers=[] if code.startswith("MWC") else [sym],
        )
        item.extra.update({"halt_code": code, "halt_reason": label, "market": market})
        item.extra["boost"] = item.extra.get("boost", 0) + boost
        return item
