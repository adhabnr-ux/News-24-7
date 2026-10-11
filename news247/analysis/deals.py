"""Who is being bought? Takeover headlines for listed companies of any size.

The most common first headline of a takeover names the buyer first: "C.H. Robinson to buy RXO
for $5.8 billion", "Schneider Electric to buy PTC for $22.6bn in cash", "McKesson, CD&R near
$5 billion-plus deal to buy Option Care, FT reports". The target is the stock that jumps 20-50%;
the buyer usually falls. In the week of Oct 5 2026 those three headlines came 2-17 hours before
the targets' moves, and the rules scored all three LOW because the target was never tagged.

``read_deal`` finds the target (and the buyer, when it is listed) in four shapes:

* ``<buyer> to buy / to acquire / agrees to buy / near deal to buy / in talks to buy <target>``
* ``<target> to be acquired by / to be bought by / agrees to be acquired by <buyer>``
* ``<buyer> and <partner> sign agreement to acquire <target>``
* ``<a> to merge with <b>``: the smaller company is treated as the target

The target is resolved against the listed universe by ticker, by full name, by a company whose
name is its ticker ("PTC", "RXO", "XP"), or by the first words of its name ("Option Care" ->
Option Care Health). Questions, buybacks, stakes and "reasons to buy" lists are not deals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..market.universe import name_key

_VERB = (
    r"(?:to buy|to acquire|agrees? to (?:buy|acquire)|agreed to (?:buy|acquire)|will (?:buy|acquire)|"
    r"buys|acquires|acquiring|is acquiring|"
    r"nears? (?:a )?(?:[$\w.,-]+ )?deal (?:to buy|to acquire|for)|near (?:a )?(?:[$\w.,+-]+ )?deal (?:to buy|to acquire|for)|"
    r"(?:in|enters?) (?:advanced |exclusive )?talks to (?:buy|acquire)|in talks for (?:a )?(?:[$\w.,+-]+ )?deal for|"
    r"explores? (?:a )?(?:bid|offer|deal) for|makes? (?:a )?(?:[$\w.,-]+ )?(?:takeover |buyout )?(?:offer|bid|approach) for|"
    r"bids? for|sign(?:s)? (?:an |a definitive )?(?:merger )?agreement to acquire|to take [\w .,&'-]{2,40}? private)"
)
_BUYER_FIRST = re.compile(rf"^(?P<buyer>.+?)\s+{_VERB}\s+(?P<target>.+)$", re.I)
_TARGET_FIRST = re.compile(
    r"^(?P<target>.+?)\s+(?:to be (?:acquired|bought|taken private)|agrees? to be (?:acquired|bought)|"
    r"(?:is )?being (?:acquired|bought)|to sell itself)\s+(?:(?:for|at) [^;]{1,48}?\s+)?(?:by|to)\s+(?P<buyer>.+)$",
    re.I,
)
_MERGE = re.compile(r"^(?P<a>.+?)\s+(?:to|will|agrees? to) merge with\s+(?P<b>.+)$", re.I)
_TALKS = re.compile(
    r"\b(?:near|nears|talks|explor\w*|consider\w*|weigh\w*|approach\w*|bid|bids|offer|proposal|"
    r"interest|said to|reportedly|people familiar|sources)\b",
    re.I,
)
_PRICE = re.compile(
    r"\$\s?(\d{1,5}(?:\.\d{1,2})?)(?:\s*|-)(?:per[ -]share|a share|/share|/shr|/sh\b|each|cash per share|in cash per share)",
    re.I,
)
_CASH_ELECT = re.compile(r"\$\s?(\d{1,5}(?:\.\d{1,2})?)\s+cash\b", re.I)
_SPAN_END = re.compile(
    r"\s+(?:for|in|at|from|valued|worth|with|after|amid|as|to|that|which|—|–|-)\s+|[;:,(|]|\s-\s|\s—\s",
)
# words a "buyer" never starts with: these are opinions and lists, not deals
_NOT_BUYER = re.compile(
    r"^(?:\d|why|how|should|is|are|was|were|it'?s|time|investors?|traders?|analysts?|funds?|hedge funds?|"
    r"retail|people|buyers|bulls|bears|wall street|reasons?|ways|here'?s|what|when|where|top|best|"
    r"stocks?|shares|the market|markets|buffett says|cramer|motley)\b",
    re.I,
)
# what follows "to buy" when it is not a company being bought
_NOT_TARGET = re.compile(
    r"^(?:\$[\d.,]+\s*(?:[kmb]|bn|mn|million|billion)?\s+(?:of|worth|in)\b|[\d.,]+\s*(?:[km]|million)?\s*shares|"
    r"back|its own|own|shares|stock|more|a stake|stake|stakes|minority|majority|up to|bitcoin|"
    r"ether|gold|bonds?|treasur|assets|the dip|calls|puts|options|land|real estate|homes?|"
    r"rights|spectrum|licen[cs]es?|patents?|a portfolio|portfolio|fleet|aircraft|jets|plants?|"
    r"mines?|properties|stores|a factory|factory)\b",
    re.I,
)
_PART_OF = re.compile(
    r"'s\s+(?:unit|business|division|assets|stake|brand|operations|portfolio|plant|segment)\b", re.I
)
# all-caps words that are listed tickers but are almost always something else in a headline
_STOP = frozenset(
    """
    A AI AM AN AP ARE AS AT BE BY CEO CFO CO COO CORP CTO DO EU ETF EV FDA FT GO HE IN INC IPO IS IT
    LLC LP ME MY NO NOW OF OK ON ONE OR SEC SO TO TV UK UP US USA WE WSJ CNBC DOJ FTC NYSE NASDAQ
    OUT ALL ANY BIG CAN FOR HAS NEW OLD ONE OUR SEE TWO WAS WHO WHY YOU AND THE NOT BUT GET HAS
    """.split()  # noqa: SIM905 - a word list reads better as prose
)
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9.&-]{0,5}\b")
_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'.-]*")


@dataclass
class Deal:
    target: Any  # market.universe.Listing
    buyer: Any | None  # Listing, when the buyer is listed
    buyer_name: str
    stage: str  # "definitive" | "talks"
    price: float | None = None  # offer per share, when the headline gives it

    @property
    def premium(self) -> float | None:
        last = getattr(self.target, "price", None)
        if self.price and last:
            return (self.price / last - 1.0) * 100.0
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.symbol,
            "buyer": self.buyer.symbol if self.buyer is not None else None,
            "buyer_name": self.buyer_name,
            "stage": self.stage,
            "price": self.price,
            "premium": round(self.premium, 1) if self.premium is not None else None,
        }


def _clean(span: str) -> str:
    span = span.strip().strip("\"'“”‘’")
    m = _SPAN_END.search(span)
    return (span[: m.start()] if m else span).strip()


def resolve(span: str, universe: Any) -> Any | None:
    """The listed company a short span of a headline names, or None."""
    if not span or universe is None or not len(universe):
        return None
    words = _WORDS.findall(span)[:8]
    if not words:
        return None
    # 1. the longest run of leading words that is a full listed name ("C.H. Robinson Worldwide")
    for n in range(len(words), 0, -1):
        key = name_key(" ".join(words[:n]))
        sym = universe._by_name.get(key) if key else None
        if sym:
            if n == 1 and (len(key) < 5 and not words[0].isupper()):
                continue
            return universe.by_symbol[sym]
    # 2. a company whose name is its ticker ("PTC", "RXO", "XP"), written in capitals
    first = words[0]
    if first.isupper() and len(first) >= 2:
        sym = universe._short.get(first.lower())
        if sym and sym == first.upper().replace(".", "-"):
            return universe.by_symbol[sym]
    # 3. the first words of exactly one listed name ("Option Care" -> Option Care Health)
    raw_lead = []
    for w in words:
        if not (w[0].isupper() or w[0].isdigit() or w in ("&", "and")):
            break
        raw_lead.append(w.removesuffix("'s"))
    # the index's own alphabet: "C.H. Robinson" -> ("c", "h", "robinson")
    lead = name_key(" ".join(raw_lead)).split() if raw_lead else []
    if len(lead) >= 2:
        cands = {
            sym
            for full, sym in universe._first.get(lead[0], [])
            if full[: len(lead)] == tuple(lead)
            or (len(lead) > len(full) and tuple(lead[: len(full)]) == full)
        }
        if len(cands) == 1:
            return universe.by_symbol[next(iter(cands))]
    # 4. a listed ticker written in capitals inside the span ("... to buy RXO")
    for tok in _TOKEN.findall(span):
        sym = tok.replace(".", "-")
        if sym in _STOP or len(sym) < 2:
            continue
        li = universe.get(sym)
        if li is not None and li.common:
            return li
    # 5. a name anywhere in the span ("Infusion Services Provider Option Care Health")
    found = universe.find_named(span, limit=2)
    return found[0] if len(found) == 1 else None


def _price(title: str) -> float | None:
    m = _PRICE.search(title) or _CASH_ELECT.search(title)
    return float(m.group(1)) if m else None


def read_deal(title: str, universe: Any) -> Deal | None:
    """The takeover a headline announces or reports, or None."""
    if not title or "?" in title or universe is None or not len(universe):
        return None
    t = " ".join(title.split())
    stage = "talks" if _TALKS.search(t) else "definitive"
    m = _TARGET_FIRST.match(t)
    if m:
        target = resolve(_clean(m.group("target")), universe)
        if target is None:
            return None
        buyer_span = _clean(m.group("buyer"))
        buyer = resolve(buyer_span, universe)
        if buyer is not None and buyer.symbol == target.symbol:
            return None
        return Deal(target, buyer, buyer_span, stage, _price(t))
    m = _MERGE.match(t)
    if m:
        a, b = resolve(_clean(m.group("a")), universe), resolve(_clean(m.group("b")), universe)
        if a is None or (b is not None and a.symbol == b.symbol):
            return None
        if b is not None and (b.market_cap or 0) < (a.market_cap or 0):
            a, b = b, a
        return Deal(a, b, b.name if b is not None else _clean(m.group("b")), stage, _price(t))
    m = _BUYER_FIRST.match(t)
    if not m:
        return None
    buyer_span = m.group("buyer").strip()
    raw_target = m.group("target").strip()
    if _NOT_BUYER.match(buyer_span) or _NOT_TARGET.match(raw_target) or _PART_OF.search(raw_target[:60]):
        return None
    if len(buyer_span.split()) > 10 or not (buyer_span[0].isupper() or buyer_span[0].isdigit()):
        return None
    # "to take X private": the target sits inside the verb phrase
    tp = re.search(r"to take (.+?) private", t, re.I)
    target = resolve(_clean(tp.group(1)), universe) if tp else resolve(_clean(raw_target), universe)
    if target is None:
        return None
    # "McKesson, CD&R near deal": the first listed name is the buyer
    buyer = resolve(re.split(r",|\s+and\s+|\s+&\s+", buyer_span)[0], universe)
    if buyer is not None and buyer.symbol == target.symbol:
        return None
    return Deal(target, buyer, buyer_span, stage, _price(t))


_MONEY = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(trillion|tn|billion|bn|b|million|mn|m)\b", re.I)
_SCALE = {
    "trillion": 1e12,
    "tn": 1e12,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mn": 1e6,
    "m": 1e6,
}


def acquirer_side(title: str, universe: Any) -> tuple[Any, float] | None:
    """(the listed buyer, the deal's dollar value) when a headline is about a listed company
    buying something with a stated price, e.g. "Microsoft to acquire gaming studio for $1 billion".
    A deal worth a sliver of the buyer's value doesn't move the buyer."""
    if not title or universe is None or not len(universe):
        return None
    m = _BUYER_FIRST.match(" ".join(title.split()))
    money = _MONEY.search(title)
    if not m or not money:
        return None
    buyer = resolve(re.split(r",|\s+and\s+|\s+&\s+", m.group("buyer").strip())[0], universe)
    if buyer is None:
        return None
    value = float(money.group(1)) * _SCALE[money.group(2).lower()]
    return buyer, value
