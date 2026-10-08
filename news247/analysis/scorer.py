"""Rule-based impact scoring — deterministic, explainable, and fast (~50 µs per item).

The score (0-100) estimates how likely an item is to move stocks *right now*:

    source tier base
  + event keywords (diminishing: best + ½·second + ¼·third)
  + themes (multi-condition patterns like "AI lab launches enterprise agent")
  + company relevance (watchlist / known names / exposed tickers)
  + urgency markers ("BREAKING", "*" bulletins) and source boosts (8-K items, halt codes)
  − penalties (law-firm spam, listicles, "here's why", event schedules)

then scaled down for tickers nobody is tracking. ``explain`` returns every contribution, so
tuning is a matter of reading `news247 score "<headline>"`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import ScoringConfig
from ..models import Analysis, NewsItem, SourceTier
from .entities import EntityMatcher

TIER_BASE = {SourceTier.PRIMARY: 20.0, SourceTier.WIRE: 12.0, SourceTier.MEDIA: 10.0, SourceTier.SOCIAL: 5.0}
UNKNOWN_TICKER_FACTOR = 0.45
THEME_CAP = 35.0
PENALTY_CAP = 60.0

_HANDLE_PREFIX = re.compile(r"^@[\w.\-]+:\s*")
_DOWN_WORDS = re.compile(
    r"\b(plunge\w*|crash\w*|tumble\w*|sink\w*|slump\w*|plummet\w*|fall\w*|drop\w*|sell-?off|bankrupt\w*|halt\w*|"
    r"cuts? guidance|lowers? guidance|withdraws?|miss(es|ed)?|probe|bans?|banned|downgrade\w*|recall\w*|fraud|"
    r"restat\w*|delist\w*|lawsuit|investigation|warning|layoffs?|resign\w*|ousted|reject\w*|clinical hold)\b",
    re.I,
)
_UP_WORDS = re.compile(
    r"\b(soar\w*|surge\w*|jump\w*|rall(y|ies|ied)|rocket\w*|beats?|raises? guidance|record|approv\w*|"
    r"upgrade\w*|buyback|repurchase|to acquire|takeover|buyout|wins?|awarded|partnership)\b",
    re.I,
)


def term_regex(term: str) -> str:
    """'acqui*' -> \\bacqui\\w*  ;  'chapter 11' -> \\bchapter\\s+11\\b"""
    parts = []
    for word in term.strip().split():
        if word.endswith("*"):
            parts.append(re.escape(word[:-1]) + r"[\w-]*")
        else:
            parts.append(re.escape(word))
    body = r"\s+".join(parts)
    end = "" if term.endswith("*") else r"(?![\w])"
    return r"(?<![\w])" + body + end


@dataclass
class _Theme:
    name: str
    groups: list[re.Pattern[str]]
    weight: float
    direction: str
    tickers: list[str]
    description: str


class Scorer:
    def __init__(
        self, cfg: ScoringConfig, knowledge: dict[str, Any], tracked: list[str] | None = None
    ) -> None:
        self.cfg = cfg
        companies = list(knowledge.get("companies", [])) + list(cfg.companies)
        self.watchlist = {t.upper() for t in cfg.watchlist} or {
            t.upper() for t in knowledge.get("default_watchlist", [])
        }
        self.tracked = self.watchlist | {t.upper() for t in (tracked or [])}
        self.entities = EntityMatcher(companies, sorted(self.tracked))

        kw: dict[str, float] = {**knowledge.get("keywords", {}), **cfg.keywords}
        self.keywords = [
            (k, float(w), re.compile(term_regex(k), re.I)) for k, w in kw.items() if float(w) != 0
        ]
        pen = knowledge.get("penalties", {})
        self.penalties = [(k, float(w), re.compile(term_regex(k), re.I)) for k, w in pen.items()]
        self.urgency = [re.compile(p, re.I) for p in knowledge.get("urgency", [])]
        self.mute = [re.compile(p, re.I) for p in cfg.mute]

        themes_raw = {**knowledge.get("themes", {}), **cfg.themes}
        self.themes: list[_Theme] = []
        for name, t in themes_raw.items():
            if not t or t.get("enabled", True) is False:
                continue
            groups = [
                re.compile("|".join(term_regex(x) for x in grp), re.I) for grp in t.get("all", []) if grp
            ]
            if groups:
                self.themes.append(
                    _Theme(
                        name=name,
                        groups=groups,
                        weight=float(t.get("weight", 10)),
                        direction=t.get("direction", "mixed"),
                        tickers=[x.upper() for x in t.get("tickers", [])],
                        description=t.get("description", name),
                    )
                )

    # ------------------------------------------------------------------ public

    def score(self, item: NewsItem) -> Analysis:
        return self.explain(item)

    def explain(self, item: NewsItem) -> Analysis:
        reasons: list[str] = []
        title, summary = item.title, item.summary

        for rx in self.mute:
            if rx.search(item.text):
                return Analysis(0.0, self.cfg.severity_for(0), reasons=[f"muted by /{rx.pattern}/"])

        score = TIER_BASE.get(item.tier, 8.0)
        reasons.append(f"+{score:.0f} {item.tier.name.lower()} source")

        # --- event keywords (title full weight, summary-only half weight), diminishing returns
        hits: list[tuple[float, str]] = []
        for term, w, rx in self.keywords:
            if rx.search(title):
                hits.append((w, term))
            elif summary and rx.search(summary):
                hits.append((w * 0.5, f"{term} (summary)"))
        hits.sort(reverse=True)
        for factor, (w, term) in zip((1.0, 0.5, 0.25), hits[:3]):
            contrib = w * factor if w > 0 else w
            score += contrib
            reasons.append(f"{contrib:+.0f} keyword '{term}'")

        # --- penalties
        pen_total = 0.0
        for term, w, rx in self.penalties:
            if rx.search(title):
                pen_total += w
                reasons.append(f"-{w:.0f} penalty '{term}'")
            elif summary and rx.search(summary):
                pen_total += w * 0.3
        if title.rstrip().endswith("?"):
            pen_total += 10
            reasons.append("-10 question headline")
        score -= min(PENALTY_CAP, pen_total)

        # --- urgency / shape (ignore the "@handle: " prefix of social posts)
        body = _HANDLE_PREFIX.sub("", title)
        if any(rx.search(body) for rx in self.urgency):
            score += 12
            reasons.append("+12 urgency marker")
        letters = [c for c in title if c.isalpha()]
        if len(letters) > 20 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
            score += 5
            reasons.append("+5 all-caps bulletin")

        # --- source-provided boost (8-K items, halt reason codes, configured boosts)
        boost = float(item.extra.get("boost", 0) or 0)
        if boost:
            score += boost
            reasons.append(f"{boost:+.0f} source signal")

        # --- entities & tickers
        text = item.text
        companies = self.entities.find_companies(text)
        for name in item.extra.get("entities", []):
            comp = self.entities.company(name)
            if comp and comp not in companies:
                companies.append(comp)
        explicit = list(dict.fromkeys([*item.tickers, *self.entities.find_tickers(text, title)]))
        entity_tickers: list[str] = []
        for comp in companies:
            entity_tickers.extend(comp.tickers)
        if any(t in self.watchlist for t in explicit + [c.ticker for c in companies if c.ticker]):
            score += 10
            reasons.append("+10 watchlist company")
        elif companies:
            score += 5
            reasons.append(f"+5 known entity ({', '.join(c.name for c in companies[:3])})")

        # --- themes (matched over text + entity names so source hints count). A post on a
        # company's own newsroom *is* an announcement even if the headline has no verb.
        theme_text = text + " " + " ".join(c.name for c in companies)
        if item.tier == SourceTier.PRIMARY and item.extra.get("entities"):
            theme_text += " announces"
        themes: list[_Theme] = [t for t in self.themes if all(g.search(theme_text) for g in t.groups)]
        theme_total = 0.0
        theme_tickers: list[str] = []
        for t in themes:
            theme_total += t.weight
            theme_tickers.extend(t.tickers)
            reasons.append(f"+{t.weight:.0f} theme {t.name}")
        if theme_total > THEME_CAP:
            reasons.append(f"(themes capped at {THEME_CAP:.0f})")
        score += min(THEME_CAP, theme_total)

        # --- relevance: news about tickers nobody tracks is scaled down
        if explicit and not companies and not themes and not any(t in self.tracked for t in explicit):
            score *= UNKNOWN_TICKER_FACTOR
            reasons.append(f"×{UNKNOWN_TICKER_FACTOR} untracked ticker(s) {', '.join(explicit[:3])}")

        score = max(0.0, min(100.0, score))
        tickers = list(dict.fromkeys(explicit + theme_tickers + entity_tickers))[:20]
        return Analysis(
            score=round(score, 1),
            severity=self.cfg.severity_for(score),
            tickers=tickers,
            entities=[c.name for c in companies],
            themes=[t.name for t in themes],
            reasons=reasons,
            direction=self._direction(title, themes),
        )

    def rescore(self, analysis: Analysis, delta: float, reason: str) -> Analysis:
        """Apply a later adjustment (multi-source confirmation, LLM) and recompute severity."""
        analysis.score = round(max(0.0, min(100.0, analysis.score + delta)), 1)
        analysis.severity = self.cfg.severity_for(analysis.score)
        analysis.reasons.append(f"{delta:+.0f} {reason}")
        return analysis

    @staticmethod
    def _direction(title: str, themes: list[_Theme]) -> str:
        down = len(_DOWN_WORDS.findall(title))
        up = len(_UP_WORDS.findall(title))
        if down > up:
            return "down"
        if up > down:
            return "up"
        dirs = {t.direction for t in themes if t.direction in ("up", "down")}
        if len(dirs) == 1:
            return dirs.pop()
        if dirs:
            return "mixed"
        return "unknown"
