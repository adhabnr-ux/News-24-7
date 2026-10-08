"""Cluster the same story arriving from many sources.

Five outlets reporting "OpenAI unveils GPT-6" should produce one alert — and the fact that
five outlets confirm it is itself a signal (it raises the score). Titles are reduced to
normalized content tokens; two items join the same story when they share enough of them
(overlap coefficient) and do not name disjoint sets of companies.
"""

from __future__ import annotations

import itertools
import re
import time
from dataclasses import dataclass, field

from ..models import NewsItem, Severity

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.\-+']*[a-z0-9+]|[a-z0-9]")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "than",
        "then",
        "so",
        "such",
        "into",
        "over",
        "under",
        "after",
        "before",
        "about",
        "against",
        "between",
        "through",
        "during",
        "new",
        "says",
        "said",
        "say",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "just",
        "more",
        "most",
        "up",
        "down",
        "out",
        "off",
        "via",
        "amid",
        "per",
        "vs",
        "not",
        "no",
        "yes",
        "how",
        "why",
        "what",
        "when",
        "who",
        "whom",
        "which",
        "where",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "he",
        "his",
        "she",
        "her",
        "them",
        "us",
        "report",
        "reports",
        "reportedly",
        "update",
        "updates",
        "live",
        "latest",
        "today",
        "week",
        "year",
        "ceo",
        "inc",
        "corp",
        "co",
        "ltd",
        "plc",
        "company",
        "companies",
        "stock",
        "stocks",
        "shares",
        "share",
    ]
)
# words different outlets use for the same action
SYNONYMS = {
    "unveils": "launch",
    "unveil": "launch",
    "unveiled": "launch",
    "launches": "launch",
    "launched": "launch",
    "introduces": "launch",
    "introducing": "launch",
    "introduce": "launch",
    "releases": "launch",
    "released": "launch",
    "release": "launch",
    "debuts": "launch",
    "debut": "launch",
    "rolls": "launch",
    "announces": "announce",
    "announced": "announce",
    "announcing": "announce",
    "acquire": "acquire",
    "acquires": "acquire",
    "acquiring": "acquire",
    "acquisition": "acquire",
    "buy": "acquire",
    "buys": "acquire",
    "purchase": "acquire",
    "plunge": "fall",
    "plunges": "fall",
    "tumble": "fall",
    "tumbles": "fall",
    "sink": "fall",
    "sinks": "fall",
    "slump": "fall",
    "slumps": "fall",
    "drop": "fall",
    "drops": "fall",
    "falls": "fall",
    "crash": "fall",
    "soar": "rise",
    "soars": "rise",
    "surge": "rise",
    "surges": "rise",
    "jump": "rise",
    "jumps": "rise",
    "rises": "rise",
    "rally": "rise",
    "rallies": "rise",
}


_MONEY_RE = re.compile(r"\$?(\d+(?:\.\d+)?)\s*(billion|bln|bn|b|million|mln|mn|m|trillion|tn|t)\b")
_UNIT = {
    "billion": "b",
    "bln": "b",
    "bn": "b",
    "b": "b",
    "million": "m",
    "mln": "m",
    "mn": "m",
    "m": "m",
    "trillion": "t",
    "tn": "t",
    "t": "t",
}


def tokens(title: str) -> frozenset[str]:
    title = re.sub(r"^@[\w.\-]+:\s*", "", title.lower())
    # "$50B", "$50 billion" and "50bn" are the same number in different outlets' styles
    title = _MONEY_RE.sub(lambda m: f" {m.group(1)}{_UNIT[m.group(2)]} ", title)
    out = set()
    for tok in _TOKEN_RE.findall(title):
        tok = tok.strip("'")
        if tok.endswith("'s"):
            tok = tok[:-2]
        if tok in STOPWORDS or (len(tok) < 2 and not tok.isdigit()):
            continue
        tok = SYNONYMS.get(tok, tok)
        if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return frozenset(out)


@dataclass
class Story:
    id: int
    title: str
    tokens: frozenset[str]
    entities: frozenset[str]
    created: float
    updated: float
    sources: set[str] = field(default_factory=set)
    items: int = 0
    max_score: float = 0.0
    alerted: Severity | None = None  # highest severity already pushed for this story
    alert_id: str = ""
    unconfirmed: bool = False  # alerted on a single social/squawk relay, awaiting a real source

    @property
    def confirmations(self) -> int:
        return len(self.sources)


class StoryClusterer:
    def __init__(self, window_s: float = 3 * 3600, min_overlap: float = 0.6, min_shared: int = 3) -> None:
        self.window_s = window_s
        self.min_overlap = min_overlap
        self.min_shared = min_shared
        self._stories: dict[int, Story] = {}
        self._index: dict[str, set[int]] = {}
        # ids unique across restarts (they're stored with items to compare sources later)
        self._ids = itertools.count(int(time.time() * 1000))

    def __len__(self) -> int:
        return len(self._stories)

    def _expire(self, now: float) -> None:
        cutoff = now - self.window_s
        dead = [sid for sid, s in self._stories.items() if s.updated < cutoff]
        for sid in dead:
            story = self._stories.pop(sid)
            for tok in story.tokens:
                ids = self._index.get(tok)
                if ids:
                    ids.discard(sid)
                    if not ids:
                        del self._index[tok]

    def similar(self, a: frozenset[str], b: frozenset[str]) -> bool:
        if not a or not b:
            return False
        shared = len(a & b)
        smaller = min(len(a), len(b))
        need = min(self.min_shared, max(2, smaller))
        return shared >= need and shared / smaller >= self.min_overlap

    def assign(
        self, item: NewsItem, entities: list[str] | None = None, now: float | None = None
    ) -> tuple[Story, bool]:
        """Attach ``item`` to an existing story or start a new one. Returns (story, is_new)."""
        now = now or time.time()
        self._expire(now)
        toks = tokens(item.title)
        ents = frozenset(entities or [])
        candidates: set[int] = set()
        for tok in toks:
            candidates |= self._index.get(tok, set())
        best: Story | None = None
        best_overlap = 0.0
        for sid in candidates:
            story = self._stories[sid]
            if ents and story.entities and not (ents & story.entities):
                continue
            if self.similar(toks, story.tokens):
                overlap = len(toks & story.tokens) / max(1, min(len(toks), len(story.tokens)))
                if overlap > best_overlap:
                    best, best_overlap = story, overlap
        if best is not None:
            best.updated = now
            best.items += 1
            best.sources.add(item.source)
            best.entities = best.entities | ents
            return best, False
        story = Story(
            id=next(self._ids),
            title=item.title,
            tokens=toks,
            entities=ents,
            created=now,
            updated=now,
            sources={item.source},
            items=1,
        )
        self._stories[story.id] = story
        for tok in toks:
            self._index.setdefault(tok, set()).add(story.id)
        return story, True
