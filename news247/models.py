"""Core data types that flow through the pipeline."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any

from .util import normalize_url


class Severity(IntEnum):
    """Alert severity. Ordered so comparisons (>=) work naturally."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @classmethod
    def parse(cls, value: str | int | Severity) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[str(value).strip().upper()]

    @property
    def emoji(self) -> str:
        return {0: "⚪", 1: "🟡", 2: "🟠", 3: "🔴"}[int(self)]


class SourceTier(IntEnum):
    """How authoritative / early a source is. Primary sources move markets first."""

    SOCIAL = 0  # Reddit, HN, aggregators: fast but noisy
    MEDIA = 1  # Newsrooms (CNBC, MarketWatch, ...): reliable, slightly later
    WIRE = 2  # Press-release wires: the company's own words, distributed
    PRIMARY = 3  # Company blogs, regulators, exchanges, executives' own accounts


@dataclass
class NewsItem:
    """A single piece of news from any source, normalized."""

    source: str  # source name from config, e.g. "openai-blog"
    title: str
    url: str = ""
    summary: str = ""
    published: float | None = None  # unix seconds, as claimed by the publisher
    detected: float = field(default_factory=time.time)  # unix seconds, when we saw it
    tier: SourceTier = SourceTier.MEDIA
    author: str = ""
    uid: str = ""  # stable id used for de-duplication; derived if empty
    tickers: list[str] = field(default_factory=list)  # tickers the source itself attached
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = " ".join((self.title or "").split())
        self.summary = " ".join((self.summary or "").split())
        if not self.uid:
            basis = normalize_url(self.url) if self.url else f"{self.source}|{self.title}"
            self.uid = hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}" if self.summary else self.title

    @property
    def latency(self) -> float | None:
        """Seconds between publication and detection (None if unknown or clock-skewed)."""
        if self.published is None:
            return None
        lag = self.detected - self.published
        return lag if lag >= 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.name
        return d


@dataclass
class Analysis:
    """The result of scoring a NewsItem."""

    score: float
    severity: Severity
    tickers: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    direction: str = "unknown"  # "up" | "down" | "mixed" | "unknown"
    summary: str = ""  # optional one-liner (LLM)
    llm_used: bool = False
    smallcap: dict[str, Any] = field(default_factory=dict)  # small/mid-cap catalyst read, if any

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.name
        return d


@dataclass
class PriceMove:
    """A detected abnormal price move for one symbol or a group of symbols."""

    symbol: str  # ticker, or "GROUP:<name>" for basket moves
    change_pct: float
    window_s: int
    price: float
    ref_price: float
    detected: float = field(default_factory=time.time)
    members: dict[str, float] = field(default_factory=dict)  # for group moves: symbol -> change %
    relative_pct: float | None = None  # move vs. benchmark (SPY) over the same window

    @property
    def is_group(self) -> bool:
        return self.symbol.startswith("GROUP:")

    @property
    def direction(self) -> str:
        return "up" if self.change_pct > 0 else "down"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Alert:
    """What gets pushed to notification channels."""

    kind: str  # "news" | "price" | "system"
    severity: Severity
    title: str
    body: str
    url: str = ""
    tickers: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    item: NewsItem | None = None
    analysis: Analysis | None = None
    move: PriceMove | None = None
    related: list[dict[str, Any]] = field(default_factory=list)  # correlated news <-> price context
    id: str = ""
    edge: dict[str, Any] = field(default_factory=dict)  # the play, precedents, lead, refs (edge.py)

    def __post_init__(self) -> None:
        if not self.id:
            basis = f"{self.kind}|{self.title}|{self.created}"
            self.id = hashlib.sha1(basis.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity.name,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "tickers": self.tickers,
            "created": self.created,
            "item": self.item.to_dict() if self.item else None,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "move": self.move.to_dict() if self.move else None,
            "related": self.related,
            "edge": self.edge,
        }
