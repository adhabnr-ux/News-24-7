"""Find companies and tickers in text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Uppercase words that look like tickers but are usually just words/acronyms.
TICKER_STOPLIST = {
    "A",
    "I",
    "AI",
    "IT",
    "ON",
    "NOW",
    "ALL",
    "ARE",
    "CAN",
    "FOR",
    "HAS",
    "NEW",
    "ONE",
    "OUT",
    "SO",
    "BE",
    "GO",
    "AN",
    "AT",
    "BY",
    "DO",
    "IF",
    "IN",
    "IS",
    "OF",
    "OR",
    "TO",
    "UP",
    "US",
    "WE",
    "CEO",
    "CFO",
    "IPO",
    "SEC",
    "FDA",
    "FED",
    "GDP",
    "CPI",
    "PCE",
    "USA",
    "EU",
    "UK",
    "AM",
    "PM",
    "ET",
    "PT",
    "EST",
    "PDT",
    "API",
    "LLM",
    "AGI",
    "GPU",
    "CPU",
    "TV",
    "PC",
    "EV",
    "AR",
    "VR",
    "UN",
    "OK",
    "ARM",
    "U",
    "BIG",
    "KEY",
    "LOW",
    "TECH",
    "NEWS",
    "FAST",
    "OPEN",
    "REAL",
    "BEST",
    "GOOD",
    "PLAY",
    "LIVE",
    "LOVE",
    "RUN",
    "SNOW",
    "TEAM",
}

_CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Z]{1,5}(?:[.-][A-Z])?)\b")
_EXCHANGE_RE = re.compile(
    r"\((?:NASDAQ|Nasdaq|NYSE|NYSE American|NYSE Arca|AMEX|Cboe|CBOE|TSX|OTCQX|OTCQB|OTC|NasdaqGS|NasdaqGM|NasdaqCM)"
    r"\s*:\s*([A-Z]{1,5}(?:[.-][A-Z])?)\s*[;,)]"
)


@dataclass
class Company:
    name: str
    ticker: str | None = None
    aliases: list[str] = field(default_factory=list)
    cased: list[str] = field(default_factory=list)
    exposure: list[str] = field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        """The company's own ticker if public, otherwise the stocks exposed to it."""
        return [self.ticker] if self.ticker else list(self.exposure)


def _alias_pattern(aliases: list[str]) -> str:
    # longest first so "google deepmind" wins over "google"
    escaped = [re.escape(a) for a in sorted(set(aliases), key=len, reverse=True) if a]
    return r"(?<![\w$])(" + "|".join(escaped) + r")(?![\w])"


class EntityMatcher:
    def __init__(self, companies: list[dict[str, Any]], watchlist: list[str] | None = None) -> None:
        self.companies: list[Company] = []
        by_name: dict[str, Company] = {}
        for c in companies:
            comp = Company(
                name=c["name"],
                ticker=(c.get("ticker") or None),
                aliases=[a.lower() for a in c.get("aliases", [])],
                cased=list(c.get("cased", [])),
                exposure=[t.upper() for t in c.get("exposure", [])],
            )
            if comp.ticker:
                comp.ticker = comp.ticker.upper()
            # the canonical name is always an alias
            if comp.name.lower() not in comp.aliases and comp.name not in comp.cased:
                comp.aliases.append(comp.name.lower())
            if comp.name in by_name:  # user override replaces built-in entry
                self.companies.remove(by_name[comp.name])
            by_name[comp.name] = comp
            self.companies.append(comp)
        self.by_name = by_name
        self.by_ticker = {c.ticker: c for c in self.companies if c.ticker}
        self._ci_map: dict[str, Company] = {}
        self._cs_map: dict[str, Company] = {}
        for comp in self.companies:
            for a in comp.aliases:
                self._ci_map[a] = comp
            for a in comp.cased:
                self._cs_map[a] = comp
        self._ci_re = re.compile(_alias_pattern(list(self._ci_map)), re.I) if self._ci_map else None
        self._cs_re = re.compile(_alias_pattern(list(self._cs_map))) if self._cs_map else None
        self.watchlist = {t.upper() for t in (watchlist or [])}
        bare = sorted((self.watchlist | set(self.by_ticker)) - TICKER_STOPLIST, key=len, reverse=True)
        bare = [t for t in bare if len(t) >= 3]
        self._bare_re = (
            re.compile(r"(?<![\w$])(" + "|".join(map(re.escape, bare)) + r")(?![\w])") if bare else None
        )

    def find_companies(self, text: str) -> list[Company]:
        found: dict[str, Company] = {}
        if self._ci_re:
            for m in self._ci_re.finditer(text):
                comp = self._ci_map.get(m.group(1).lower())
                if comp:
                    found.setdefault(comp.name, comp)
        if self._cs_re:
            for m in self._cs_re.finditer(text):
                comp = self._cs_map.get(m.group(1))
                if comp:
                    found.setdefault(comp.name, comp)
        return list(found.values())

    def find_tickers(self, text: str, title: str = "") -> list[str]:
        """Explicit tickers: $CASHTAGS, "(NASDAQ: XYZ)" and known tickers written in the title."""
        out: dict[str, None] = {}
        for m in _CASHTAG_RE.finditer(text):
            out[m.group(1)] = None
        for m in _EXCHANGE_RE.finditer(text):
            out[m.group(1)] = None
        if self._bare_re and title:
            for m in self._bare_re.finditer(title):
                out[m.group(1)] = None
        return [t.replace(".", "-") if "." in t else t for t in out]

    def company(self, name: str) -> Company | None:
        return self.by_name.get(name)

    def is_known_ticker(self, ticker: str) -> bool:
        return ticker in self.watchlist or ticker in self.by_ticker
