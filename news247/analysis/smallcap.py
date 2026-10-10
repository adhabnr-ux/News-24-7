"""The little things: catalysts that move small and mid caps 20–300% in a session.

A big-cap desk asks "is this news?". A small-cap desk asks "how big is this news *for this
company*?". The answer is almost always relative:

* a contract worth 60% of the company's market value is a re-rating; the same dollars are
  noise for a large cap
* "to be acquired for $X per share" moves the target to the offer price, so the move IS the
  premium over the last sale
* an FDA decision or a pivotal readout is binary for a one-drug biotech (+30–100% / −40–80%)
  and a footnote for a diversified pharma
* a stake or partnership from NVIDIA, OpenAI, a hyperscaler or the US government re-rates a
  micro cap overnight (2025: MP Materials +50% on the DoD stake, Lithium Americas +95% on the
  DOE stake, Serve Robotics and SoundHound on Nvidia's 13F)
* an offering worth a third of the company is a guaranteed gap down

``SmallCapDesk.read`` finds the listed company a headline is about (``Universe``), classifies
the catalyst, sizes it to an expected percentage move and converts that into score points
— so a $180M biotech's approval is texted to you at the first wire, while the same words
about an untracked large cap stay quiet. Expected moves are rules of thumb from the
2024–2026 record (docs/SMALLCAPS.md), never promises: small caps also fade, and "typical"
means the middle of a wide range.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..market.universe import Listing, Universe, fmt_cap
from .entities import TICKER_STOPLIST

# --------------------------------------------------------------------------- vocab

_AMOUNT_RE = re.compile(
    r"(?:US)?\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(trillion|billion|bn|b|million|mm|mln|m|thousand|k)?\b",
    re.I,
)
_UNITS = {
    "trillion": 1e12,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mm": 1e6,
    "mln": 1e6,
    "m": 1e6,
    "thousand": 1e3,
    "k": 1e3,
}
_PER_SHARE_RE = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s*(?:in\s+cash\s+)?(?:per|a|/)\s*(?:common\s+|ordinary\s+)?(?:share|ads|ordinary share)\b",
    re.I,
)
_PREMIUM_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*premium|premium\s+of\s+(?:approximately\s+|about\s+|roughly\s+)?(\d{1,3}(?:\.\d+)?)\s*%",
    re.I,
)

_PAREN_SYMBOL_RE = re.compile(r"\(([A-Z]{1,5}(?:[.-][A-Z])?)\)")

# Partners whose name alone re-rates a micro cap (AI labs, hyperscalers, mega-cap industrials
# and pharma, sovereign buyers). Lower-case match on word boundaries.
MEGA_PARTNERS = {
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "xai": "xAI",
    "microsoft": "Microsoft",
    "amazon": "Amazon",
    "aws": "Amazon",
    "apple": "Apple",
    "google": "Google",
    "alphabet": "Alphabet",
    "deepmind": "Google DeepMind",
    "meta platforms": "Meta",
    "tesla": "Tesla",
    "spacex": "SpaceX",
    "oracle": "Oracle",
    "palantir": "Palantir",
    "broadcom": "Broadcom",
    "amd": "AMD",
    "intel": "Intel",
    "ibm": "IBM",
    "qualcomm": "Qualcomm",
    "samsung": "Samsung",
    "tsmc": "TSMC",
    "softbank": "SoftBank",
    "walmart": "Walmart",
    "lockheed martin": "Lockheed Martin",
    "boeing": "Boeing",
    "rtx": "RTX",
    "raytheon": "RTX",
    "northrop grumman": "Northrop Grumman",
    "general dynamics": "General Dynamics",
    "anduril": "Anduril",
    "pfizer": "Pfizer",
    "eli lilly": "Eli Lilly",
    "lilly": "Eli Lilly",
    "novo nordisk": "Novo Nordisk",
    "merck": "Merck",
    "johnson & johnson": "Johnson & Johnson",
    "abbvie": "AbbVie",
    "astrazeneca": "AstraZeneca",
    "bristol myers": "Bristol Myers Squibb",
    "novartis": "Novartis",
    "roche": "Roche",
    "sanofi": "Sanofi",
    "gsk": "GSK",
    "amgen": "Amgen",
    "gilead": "Gilead",
    "regeneron": "Regeneron",
    "vertex": "Vertex",
    "coreweave": "CoreWeave",
    "nebius": "Nebius",
    "berkshire hathaway": "Berkshire Hathaway",
}
_PARTNER_RE = re.compile(
    r"(?<![\w])("
    + "|".join(re.escape(k) for k in sorted(MEGA_PARTNERS, key=len, reverse=True))
    + r")(?![\w])",
    re.I,
)
_GOV_RE = re.compile(
    r"(?<![\w])(department of (?:war|defense|energy|commerce)|pentagon|u\.?s\.? government|us government|"
    r"federal government|trump administration|white house|dod|doe|commerce department|"
    r"office of strategic capital|ex-im bank|export-import bank|in-q-tel|defense production act)(?![\w])",
    re.I,
)
_GOV_BUYER_RE = re.compile(
    r"(?<![\w])(u\.?s\.? army|army|navy|air force|space force|marine corps|pentagon|department of (?:war|defense|"
    r"energy|homeland security)|dod|darpa|nasa|missile defense agency|nato|dhs|faa|federal|u\.?s\.? government)(?![\w])",
    re.I,
)
SHORT_SELLERS = (
    "hindenburg",
    "muddy waters",
    "spruce point",
    "culper",
    "fuzzy panda",
    "wolfpack",
    "grizzly research",
    "kerrisdale",
    "citron",
    "iceberg research",
    "blue orca",
    "bonitas",
    "morpheus research",
    "j capital",
    "gotham city",
    "scorpion capital",
    "viceroy",
    "white diamond",
)


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(parts), re.I)


ACQUIRED_RE = _rx(
    r"\b(?:agrees?|agreed|enters?|entered)\s+(?:in)?to\s+(?:a\s+)?(?:definitive\s+)?(?:merger\s+)?(?:agreement\s+)?"
    r"(?:to\s+)?be\s+(?:acquired|bought|purchased)\b",
    r"\bto\s+be\s+(?:acquired|bought|taken\s+private)\b",
    r"\b(?:acquired|bought)\s+by\b",
    r"\b(?:to\s+go|going|taken|take-)\s?private\b",
    r"\bagrees?\s+to\s+sell\s+(?:itself|the\s+company)\b",
    r"\bdefinitive\s+(?:merger\s+)?agreement\s+to\s+be\s+acquired\b",
)
ACQUIRE_VERB_RE = _rx(
    r"\b(?:to\s+acquire|will\s+acquire|acquires|acquiring|to\s+buy|buys|to\s+purchase|tender\s+offer\s+for|"
    r"agrees?\s+to\s+(?:acquire|buy)|offer\s+for)\b"
)
TAKEOVER_INTEREST_RE = _rx(
    r"\bexplor\w*\s+(?:a\s+)?(?:potential\s+)?sale\b",
    r"\bstrategic\s+alternatives\b",
    r"\b(?:takeover|buyout|acquisition)\s+(?:interest|approach|bid|proposal|offer)\b",
    r"\bunsolicited\s+(?:proposal|offer|bid)\b",
    r"\bnon-binding\s+(?:proposal|offer|indication)\b",
    r"\bin\s+(?:advanced\s+)?talks\s+to\s+(?:be\s+acquired|sell|merge)\b",
    r"\b(?:weighs?|considering|considers)\s+(?:a\s+)?sale\b",
)
FDA_APPROVAL_RE = _rx(
    r"\bfda\s+(?:grants?\s+)?(?:full\s+|accelerated\s+|traditional\s+)?approv\w*",
    r"\bapprov\w*\s+by\s+(?:the\s+)?(?:u\.s\.\s+)?(?:fda|food\s+and\s+drug\s+administration)\b",
    r"\b(?:receives?|received|wins?|secures?|announces?)\s+(?:u\.s\.\s+)?fda\s+approval\b",
    r"\bfda\s+approves\b",
    r"\bchmp\s+positive\s+opinion\b",
    r"\b(?:european\s+commission|ec)\s+approv\w*",
)
DEVICE_CLEAR_RE = _rx(
    r"\b510\(k\)\s+clearance\b", r"\bfda\s+clear\w*", r"\bde\s+novo\s+(?:classification|authori[sz]ation)\b"
)
SUPPLEMENTAL_RE = _rx(
    r"\bsupplemental\b",
    r"\bsnda\b",
    r"\bsbla\b",
    r"\blabel\s+expansion\b",
    r"\bexpanded\s+(?:indication|approval|label)\b",
)
FDA_PATH_RE = _rx(
    r"\bfda\s+(?:agree\w*|align\w*)\b",
    r"\balign\w*\s+with\s+(?:the\s+)?fda\b",
    r"\b(?:agreement|alignment)\s+with\s+(?:the\s+)?fda\b",
    r"\baccelerated\s+approval\s+(?:path\w*|route|filing)\b",
    r"\bfda\s+(?:accepts|backs|supports)\s+(?:\w+\s+){0,4}(?:data|basis|path\w*)\b",
)
FDA_SETBACK_RE = _rx(
    r"\bfda\b.{0,60}\brequir\w*\s+(?:an?\s+)?(?:additional|new|another|second|sham|randomized|placebo)",
    r"\bfda\s+(?:reverses|backtracks|no\s+longer)\b",
)
FDA_NEGATIVE_RE = _rx(
    r"\bcomplete\s+response\s+letter\b",
    r"\bcrl\b",
    r"\brefus\w*\s+to\s+file\b",
    r"\bclinical\s+hold\b",
    r"\bfda\s+(?:declines|rejects|rejected|denies|denied)\b",
    r"\bnot\s+approvable\b",
)
_PANEL = r"\b(?:advisory\s+committee|adcomm|panel|advisers|advisors)\b.{0,40}"
ADCOM_YES_RE = _rx(
    _PANEL
    + r"\b(?:votes?|voted)\s+(?:\d+\s*(?:-|–|to)\s*\d+\s+)?(?:in\s+favor|to\s+(?:support|recommend)|for\b)"
)
ADCOM_NO_RE = _rx(
    _PANEL + r"\b(?:votes?|voted)\s+(?:\d+\s*(?:-|–|to)\s*\d+\s+)?against\b",
    _PANEL + r"\b(?:does|do|did)\s+not\s+support\b",
)
TRIAL_WIN_RE = _rx(
    r"\b(?:met|meets|achieved|achieves|hit|hits|reached)\s+(?:its\s+|the\s+|all\s+)?(?:co-)?primary\s+(?:and\s+\w+\s+)?end\s?points?\b",
    r"\bpositive\s+(?:topline|top-line|pivotal|phase\s*(?:2b?|3|iii|ii)|interim|final)\b",
    r"\b(?:topline|top-line|pivotal|phase\s*(?:2b?|3|iii))\s+(?:\w+\s+){0,3}(?:success|positive)\b",
    r"\bstatistically\s+significant\b",
    r"\bstopped\s+early\s+(?:for|due\s+to)\s+(?:efficacy|positive|overwhelming)\b",
)
# "Announces Topline Results from Phase 3 …" with no verdict in the headline: a binary event
TOPLINE_RE = _rx(
    r"\b(?:topline|top-line|headline)\s+(?:\w+\s+){0,2}(?:results?|data)\b",
    r"\bpivotal\s+(?:\w+\s+){0,3}results?\b",
)
READOUT_SCHEDULED_RE = _rx(
    r"\bto\s+(?:present|report|announce|host|release)\b.{0,50}\b(?:topline|top-line|pivotal|phase\s*(?:2b?|3|iii))\b",
    r"\b(?:pdufa|target\s+action)\s+date\b",
    r"\badvisory\s+committee\s+meeting\s+(?:scheduled|to\s+review|date)\b",
)
TRIAL_FAIL_RE = _rx(
    r"\b(?:did\s+not|does\s+not|didn't|failed\s+to|fails\s+to|fail\s+to)\s+(?:meet|achieve|reach|show)\b",
    r"\bnot\s+statistically\s+significant\b",
    r"\bmiss(?:ed|es)?\s+(?:its\s+|the\s+)?primary\s+end\s?point\b",
    r"\b(?:did\s+not|does\s+not|failed\s+to)\s+achieve\s+statistical\s+significance\b",
    r"\bdiscontinu\w*\s+(?:the\s+|its\s+)?(?:development|program|trial|study)\b",
    r"\bfutility\b",
)
DESIGNATION_RE = _rx(
    r"\b(breakthrough\s+therapy|breakthrough\s+device|fast\s+track|orphan\s+drug|rare\s+pediatric\s+disease|rmat|priority\s+review)\s+(?:designation|status|voucher)?",
)
CONTRACT_RE = _rx(
    r"\b(?:awarded|awards?|wins?|won|secures?|secured|receives?|received|lands?|landed|books?|booked|signs?|signed|"
    r"selected|chosen)\b.{0,60}\b(?:contract|order|award|task\s+order|purchase\s+order|agreement|deal|program|leases?)\b",
    r"\b(?:lease|offtake|supply)\s+(?:agreement\s+)?(?:with|from)\b",
    r"\b(?:contract|purchase\s+order|order|award)\s+(?:valued|worth)\s+",
)
CEILING_RE = _rx(
    r"\bup\s+to\b",
    r"\bceiling\b",
    r"\bidiq\b",
    r"\bpotential\s+(?:total\s+)?value\b",
    r"\bmulti-year\b",
    r"\bframework\b",
)
PARTNER_VERB_RE = _rx(
    r"\b(?:partner\w*|collaborat\w*|selects|selected|alliance|joint\s+venture|selected\s+by|chosen\s+by|supplier\s+to|"
    r"supply\s+agreement|integrat\w*\s+(?:with|into)|investment\s+from|invests?\s+in|invested\s+in|"
    r"stake\s+in|takes?\s+(?:a\s+)?(?:\d+(?:\.\d+)?%\s+)?stake|strategic\s+investment|licens\w*\s+(?:agreement|deal)|"
    r"discloses\s+(?:a\s+)?(?:\d+(?:\.\d+)?%\s+)?stake|backed\s+by|agreement\s+with|deal\s+with|"
    r"contract\s+with|order\s+from|signs?\s+(?:a\s+)?(?:\$[\d.,]+\s*\w*\s+)?(?:deal|agreement|contract))\b"
)
# "joins the NVIDIA Inception program", "now available on AWS Marketplace", "built on NVIDIA":
# the giant never signed anything — the most common pump-style small-cap press release
PARTNER_FLUFF_RE = _rx(
    r"\binception\b",
    r"\bmarketplace\b",
    r"\bfor\s+startups\b",
    r"\bpartner\s+(?:network|program|ecosystem)\b",
    r"\b(?:built|runs?|running|powered)\s+on\b",
    r"\bpowered\s+by\b",
    r"\bavailable\s+(?:on|in|through|via)\b",
    r"\b(?:leverag\w+|using|utiliz\w+|adopts?)\s+(?:\w+\s+){0,2}(?:nvidia|microsoft|google|amazon|aws|openai|meta)\b",
    r"\b(?:certified|certification|validated)\b",
    r"\bjoins?\s+(?:the\s+)?\w+\s+(?:program|alliance|ecosystem|consortium)\b",
    r"\b(?:showcase|demonstrate|exhibit)\w*\s+at\b",
)
PHARMA_PARTNERS = {
    "Pfizer",
    "Eli Lilly",
    "Novo Nordisk",
    "Merck",
    "Johnson & Johnson",
    "AbbVie",
    "AstraZeneca",
    "Bristol Myers Squibb",
    "Novartis",
    "Roche",
    "Sanofi",
    "GSK",
    "Amgen",
    "Gilead",
    "Regeneron",
    "Vertex",
}
CLINICAL_SUPPLY_RE = _rx(
    r"\bclinical\s+(?:trial\s+)?(?:collaboration|supply)\b",
    r"\bsupply\s+(?:of|agreement\s+for)\s+\w+\s+for\s+(?:a|its)\s+(?:phase|trial)\b",
)
GOV_STAKE_RE = _rx(
    r"\b(?:strategic\s+investment|investment\s+by|public-private\s+partnership|equity\s+stake|takes?\s+(?:a\s+)?(?:\d+(?:\.\d+)?%\s+)?stake|stake\s+in|warrants?\s+to\s+(?:buy|purchase)|"
    r"become\s+(?:the\s+)?largest\s+shareholder|preferred\s+(?:equity|stock)\s+investment|price\s+floor|offtake)\b"
)
# a giant selling out of a small cap (SOUN −28% when NVIDIA's next 13F showed it had exited)
MEGA_EXIT_RE = _rx(
    r"\b(?:exited|exits|sold\s+(?:all|its\s+entire|its\s+whole)|dumps?|dumped|liquidated|sells\s+(?:all|entire))\b"
)
CRYPTO_TREASURY_RE = _rx(
    r"\b(?:bitcoin|btc|ethereum|ether|eth|solana|sol|xrp|dogecoin|doge|bnb|hype|tron|trx|ton|sui|avax|litecoin|ltc|"
    r"worldcoin|wld|crypto(?:currency)?|digital\s+asset)\s+(?:\w+\s+){0,2}treasury\b",
    r"\btreasury\s+(?:strategy|reserve|company)\b.{0,40}\b(?:bitcoin|ethereum|solana|crypto|digital\s+asset|token)",
)
SHORT_REPORT_RE = _rx(
    r"\bshort[-\s]?(?:seller|report|position|thesis)\b",
    r"\bwe\s+are\s+short\b",
    r"\b(?:revenue|sales|customers?)\s+(?:\w+\s+){0,2}(?:doesn't|does\s+not|don't|do\s+not)\s+exist\b",
    r"\bundisclosed\s+related[-\s]part",
    *(rf"\b{re.escape(n)}\b" for n in SHORT_SELLERS),
)
OFFERING_RE = _rx(
    r"\b(?:pricing\s+of|prices|priced|announces|launch\w*|proposed)\b.{0,50}\b(?:public\s+offering|registered\s+direct|"
    r"private\s+placement|underwritten\s+offering|offering\s+of\s+(?:common|ordinary|shares|\$)|"
    r"convertible\s+(?:senior\s+)?notes\s+offering|follow-on\s+offering)\b",
    r"\bat-the-market\s+(?:offering|program|equity)\b",
    r"\b(?:registered\s+direct|public)\s+offering\s+priced\b",
    r"\bpre-funded\s+warrants\b",
)
CHAPTER11_RE = _rx(
    r"\bchapter\s+(?:11|7)\b", r"\bfiles?\s+for\s+bankruptcy\b", r"\bbankruptcy\s+protection\b"
)
DELIST_RE = _rx(
    r"\bdelisting\s+determination\b",
    r"\b(?:will\s+be|to\s+be)\s+delisted\b",
    r"\bsuspend\w*\s+(?:trading|listing)\b",
    r"\bform\s+25\b",
)
GOING_CONCERN_RE = _rx(r"\bgoing\s+concern\b")
REVERSE_SPLIT_RE = _rx(r"\breverse\s+(?:stock\s+|share\s+)?split\b")
RESTATE_RE = _rx(
    r"\bnon-reliance\b",
    r"\brestat\w+\b",
    r"\bauditor\s+(?:resign|resigns|resigned|dismiss)\w*",
    r"\bmaterial\s+weakness\b",
)
DEFAULT_RE = _rx(r"\b(?:default\w*\s+on|forbearance\s+agreement|missed\s+(?:an?\s+)?interest\s+payment)\b")
INDEX_RE = _rx(
    r"\b(?:to\s+join|set\s+to\s+join|will\s+join|added\s+to|to\s+be\s+added\s+to|joins?)\s+(?:the\s+)?"
    r"(?:s&p\s*(?:500|midcap\s*400|smallcap\s*600)|nasdaq-?100|russell\s*(?:1000|2000|3000))\b"
)
GUIDE_UP_RE = _rx(
    r"\b(?:raises?|raised|increases?|boosts?|lifts?)\s+(?:its\s+)?(?:full[-\s]year\s+|fy\s?\d*\s+|\d{4}\s+)?"
    r"(?:revenue\s+|sales\s+|earnings\s+)?(?:guidance|outlook|forecast)\b",
    r"\bpreliminary\s+(?:\w+\s+){0,3}(?:revenue|results)\s+(?:above|exceed\w*|ahead)\b",
    r"\brecord\s+(?:quarterly\s+)?revenue\b.{0,40}\b(?:\d{2,4}%|doubl\w+|tripl\w+)",
)
GUIDE_DOWN_RE = _rx(
    r"\b(?:cuts?|lowers?|lowered|reduces?|withdraws?|withdrew|suspends?)\s+(?:its\s+)?(?:full[-\s]year\s+|fy\s?\d*\s+|\d{4}\s+)?"
    r"(?:revenue\s+|sales\s+|earnings\s+)?(?:guidance|outlook|forecast)\b",
    r"\bpreliminary\s+(?:\w+\s+){0,3}(?:revenue|results)\s+(?:below|short|miss\w*)\b",
)
UPLIST_RE = _rx(
    r"\buplist\w*\b", r"\bapproved\s+(?:for\s+listing|to\s+list)\s+on\s+(?:the\s+)?(?:nasdaq|nyse)\b"
)
VERDICT_RE = _rx(
    r"\bjury\s+(?:verdict|awards?|finds)\b",
    r"\bawarded\s+\$[\d.,]+\s*\w*\s+in\s+damages\b",
    r"\bwins?\s+(?:\w+\s+){0,3}(?:patent|lawsuit|litigation|arbitration|appeal)\b",
)
# Routine small-cap PR traffic that sounds like news and is not
ROUTINE_RE = _rx(
    r"\breceives?\s+(?:a\s+)?(?:nasdaq|nyse|nyse\s+american)\s+(?:notice|notification|letter|deficiency)\b",
    r"\bminimum\s+bid\s+price\b",
    r"\bregains?\s+compliance\b",
    r"\binducement\s+grants?\b",
    r"\bto\s+(?:present|participate|ring)\b",
    r"\b(?:fireside\s+chat|investor\s+conference|corporate\s+update\s+call|shareholder\s+letter|letter\s+to\s+shareholders)\b",
    r"\bto\s+(?:report|announce|host)\b.{0,40}\b(?:results|call|webcast)\b",
    r"\bannual\s+(?:general\s+)?meeting\b",
    r"\b(?:letter\s+of\s+intent|loi|memorandum\s+of\s+understanding|mou|non-binding\s+term\s+sheet)\b",
)


def amounts(text: str) -> list[float]:
    """Every '$45 million' / '$1.2B' / 'US$300,000' in the text, in USD."""
    out = []
    for num, unit in _AMOUNT_RE.findall(text):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        mult = _UNITS.get(unit.lower(), 1.0) if unit else 1.0
        out.append(v * mult)
    return out


def _deal_amounts(text: str) -> list[float]:
    """Dollar amounts excluding per-share prices ('$12.50 per share' is not a deal size)."""
    stripped = _PER_SHARE_RE.sub(" ", text)
    return [a for a in amounts(stripped) if a >= 1e5]


# --------------------------------------------------------------------------- results


@dataclass
class Catalyst:
    kind: str
    label: str  # human, e.g. "Being acquired at a 62% premium"
    direction: str  # up | down | mixed
    expected: float  # central expected move, % (absolute)
    detail: str = ""
    relative: float | None = None  # deal size / market cap, when the catalyst has one

    @property
    def range(self) -> tuple[int, int]:
        if self.kind == "acquired" and self.detail.startswith("premium"):
            return (round(self.expected * 0.85), round(self.expected * 1.0))
        return (max(1, round(self.expected * 0.6)), round(self.expected * 1.6))

    @property
    def move_text(self) -> str:
        sign = "+" if self.direction == "up" else "−" if self.direction == "down" else "±"
        lo, hi = self.range
        if self.kind == "acquired" and self.detail.startswith("premium"):
            return f"≈{sign}{hi}% (to the offer price)"
        return f"{sign}{lo}–{hi}% typical"


@dataclass
class SmallCapRead:
    listing: Listing
    catalyst: Catalyst | None
    points: float = 0.0
    material: bool = False  # big enough to lift the untracked-ticker discount
    floor: float = 0.0  # least score this read guarantees (see floor_for)
    blocked: str = ""  # why a catalyst was not credited (pump-risk filters)
    via: str = "ticker"  # ticker | name
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        li, c = self.listing, self.catalyst
        d: dict[str, Any] = {
            **li.brief(),
            "via": self.via,
            "material": self.material,
            "points": self.points,
        }
        if c is not None:
            d.update(
                {
                    "catalyst": c.kind,
                    "label": c.label,
                    "direction": c.direction,
                    "expected_pct": round(c.expected, 1),
                    "range": list(c.range),
                    "move_text": c.move_text,
                    "relative": round(c.relative, 3) if c.relative is not None else None,
                }
            )
        if self.blocked:
            d["blocked"] = self.blocked
        return d

    def push_line(self) -> str:
        c = self.catalyst
        if c is None or not self.material:
            return ""
        return f"{self.listing.symbol} · {self.listing.cap_label} {self.listing.band} · {c.label} · {c.move_text}"


# how hard a generic (not deal-size-relative) catalyst hits each size band
BAND_FACTOR = {
    "nano cap": 1.3,
    "micro cap": 1.15,
    "small cap": 1.0,
    "mid cap": 0.5,
    "large cap": 0.2,
    "mega cap": 0.1,
    "unknown size": 0.8,
}


def points_for(expected: float) -> float:
    """Expected % move -> score points (added on top of the keyword score)."""
    for limit, pts in ((60, 32.0), (40, 28.0), (25, 24.0), (15, 18.0), (10, 12.0), (6, 6.0)):
        if expected >= limit:
            return pts
    return 0.0


def floor_for(expected: float) -> float:
    """Expected % move -> the least score the headline gets. A company's own release that is
    worth a quarter of its value must reach your phone even if no keyword fires: +25% and up
    is pushed (HIGH), +15–25% shows in the app (MEDIUM)."""
    for limit, floor in ((40, 70.0), (25, 62.0), (15, 50.0), (10, 40.0)):
        if expected >= limit:
            return floor
    return 0.0


def relative_move(r: float) -> float:
    """Deal size as a share of market cap -> typical % move (5% -> 7, 25% -> 25, 50% -> 42,
    100% -> 70, capped at 80). Concave: the market discounts execution risk on giant deals."""
    return min(80.0, 70.0 * max(r, 0.0) ** 0.75)


# --------------------------------------------------------------------------- desk


class SmallCapDesk:
    """Reads a headline the way a small-cap trader does."""

    def __init__(
        self,
        universe: Universe,
        *,
        min_market_cap: float = 30e6,
        max_market_cap: float = 10e9,
        min_price: float = 1.0,
    ) -> None:
        self.universe = universe
        self.min_market_cap = min_market_cap
        self.max_market_cap = max_market_cap
        self.min_price = min_price

    # ------------------------------------------------------------------ entry point

    def read(
        self,
        title: str,
        summary: str = "",
        tickers: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SmallCapRead | None:
        """The strongest small/mid-cap read of a headline, or None if it is not about one."""
        candidates: dict[str, tuple[Listing, str]] = {}
        symbols = list(tickers or [])
        # "(SRVR)" written without an exchange prefix (SEC titles, newswire digests)
        symbols += [s for s in _PAREN_SYMBOL_RE.findall(title) if s not in TICKER_STOPLIST]
        for t in symbols:
            li = self.universe.get(t)
            if li is not None and li.common:
                candidates.setdefault(li.symbol, (li, "ticker"))
        lead = self.universe.find_issuer(title)
        if lead is not None:
            candidates.setdefault(lead.symbol, (lead, "name"))
        for li in self.universe.find_named(title):
            candidates.setdefault(li.symbol, (li, "name"))
        best: SmallCapRead | None = None
        for li, via in candidates.values():
            if li.market_cap is not None and li.market_cap > self.max_market_cap:
                continue
            read = self._read_one(li, title, summary, extra or {})
            read.via = via
            if best is None or (
                read.catalyst and (best.catalyst is None or read.catalyst.expected > best.catalyst.expected)
            ):
                best = read
        return best

    def _read_one(self, li: Listing, title: str, summary: str, extra: dict[str, Any]) -> SmallCapRead:
        cat = self.classify(li, title, summary, extra)
        read = SmallCapRead(li, cat)
        if cat is None or cat.kind == "routine":
            return read
        # A deal bigger than the whole company (a $425M PIPE into a $10M shell, a buyout) is a real
        # capital event even for a nano cap; everything else under the floor is pump territory.
        transformational = cat.relative is not None and cat.relative >= 1.0
        if li.market_cap is not None and li.market_cap < self.min_market_cap and not transformational:
            read.blocked = f"under {fmt_cap(self.min_market_cap)} market cap (pump risk)"
            return read
        if li.price is not None and li.price < self.min_price:
            read.blocked = f"under ${self.min_price:g} a share (pump risk)"
            return read
        profile = li.pump_profile
        if profile and cat.direction != "down":
            read.blocked = profile
            return read
        if (
            li.market_cap is not None
            and li.market_cap < 50e6
            and cat.kind in ("crypto_treasury", "mega_partner")
        ):
            read.notes.append("⚠ nano cap: extreme volatility, frequent reversals")
        read.points = points_for(cat.expected)
        read.floor = floor_for(cat.expected)
        read.material = read.points >= 12
        return read

    # ------------------------------------------------------------------ classification

    def classify(
        self, li: Listing, title: str, summary: str = "", extra: dict[str, Any] | None = None
    ) -> Catalyst | None:
        extra = extra or {}
        text = title if not summary else f"{title} — {summary[:400]}"
        found: list[Catalyst] = []
        bf = BAND_FACTOR.get(li.band, 0.8)
        bio = 1.0 if li.biotech else 0.6
        cap = li.market_cap

        # --- deals: the target trades to the offer
        role = self._deal_role(li, title)
        if role == "target":
            found.append(self._acquired(li, text))
        elif role == "acquirer" and cap:
            size = max(_deal_amounts(title) or [0.0])
            r = size / cap if size else 0.0
            if r >= 0.3:
                found.append(
                    Catalyst(
                        "acquirer",
                        f"Buying a business worth {r:.0%} of itself",
                        "mixed",
                        min(35.0, 25.0 * r),
                        relative=r,
                    )
                )
        if TAKEOVER_INTEREST_RE.search(title):
            found.append(
                Catalyst("takeover_interest", "Takeover interest / sale process", "up", 22.0 * max(bf, 0.6))
            )

        # --- the government or a giant buys in
        gov = _GOV_RE.search(text)
        if gov and GOV_STAKE_RE.search(text):
            found.append(
                Catalyst("gov_stake", f"US government stake ({gov.group(1)})", "up", 45.0 * max(bf, 0.8))
            )
        partner = self._partner(li, text)
        if partner and MEGA_EXIT_RE.search(title):
            found.append(Catalyst("mega_exit", f"{partner} sold out", "down", 28.0 * max(bf, 0.9)))
        elif partner and PARTNER_VERB_RE.search(title) and not PARTNER_FLUFF_RE.search(title):
            stake = re.search(r"\bstake\b|\binvest\w*\b|\b13[dg]\b|\bwarrants?\b", title, re.I)
            base = 40.0 if stake else 30.0
            if partner in PHARMA_PARTNERS:
                base = 20.0 * (0.4 if CLINICAL_SUPPLY_RE.search(title) else 1.0)
            exp = base * bf
            size = max(_deal_amounts(title) or [0.0])
            r = size / cap if (size and cap) else None
            if r:
                exp = max(exp, relative_move(r))
            label = f"{partner} {'stake' if stake else 'deal'}" + (
                f" worth {r:.0%} of market cap" if r and r >= 0.05 else ""
            )
            found.append(Catalyst("mega_partner", label, "up", exp, relative=r))

        # --- biotech binaries
        if FDA_SETBACK_RE.search(title):
            found.append(
                Catalyst("fda_setback", "FDA demands more (new trial / reversal)", "down", 35.0 * bf * bio)
            )
        elif FDA_PATH_RE.search(title):
            found.append(
                Catalyst(
                    "fda_path", "FDA path cleared (alignment / accelerated route)", "up", 40.0 * bf * bio
                )
            )
        if READOUT_SCHEDULED_RE.search(title):
            found.append(Catalyst("readout_scheduled", "Binary event scheduled — arm a watch", "mixed", 10.0))
        if FDA_NEGATIVE_RE.search(title):
            found.append(
                Catalyst("fda_negative", "FDA setback (CRL / hold / refusal)", "down", 45.0 * bf * bio)
            )
        elif FDA_APPROVAL_RE.search(title):
            sup = 0.45 if SUPPLEMENTAL_RE.search(title) else 1.0
            found.append(
                Catalyst(
                    "fda_approval",
                    "FDA approval" if sup == 1.0 else "Label expansion",
                    "up",
                    30.0 * bf * bio * sup,
                )
            )
        elif DEVICE_CLEAR_RE.search(title):
            found.append(Catalyst("device_clearance", "FDA device clearance", "up", 12.0 * bf))
        if ADCOM_NO_RE.search(title):
            found.append(Catalyst("adcom_negative", "FDA panel votes against", "down", 40.0 * bf * bio))
        elif ADCOM_YES_RE.search(title):
            found.append(Catalyst("adcom_positive", "FDA panel votes in favor", "up", 25.0 * bf * bio))
        if TRIAL_FAIL_RE.search(title):
            found.append(Catalyst("trial_fail", "Trial failure", "down", 55.0 * bf * bio))
        elif TRIAL_WIN_RE.search(title):
            phase = (
                1.2
                if re.search(r"phase\s*(?:3|iii)|pivotal|registrational", title, re.I)
                else (0.6 if re.search(r"phase\s*(?:1|i)\b", title, re.I) else 1.0)
            )
            found.append(Catalyst("trial_win", "Positive trial readout", "up", 35.0 * bf * bio * phase))
        elif TOPLINE_RE.search(title) and not READOUT_SCHEDULED_RE.search(title):
            phase = 1.2 if re.search(r"phase\s*(?:3|iii)|pivotal|registrational", title, re.I) else 1.0
            found.append(
                Catalyst(
                    "trial_readout", "Trial readout — binary, read the data", "mixed", 40.0 * bf * bio * phase
                )
            )
        m = DESIGNATION_RE.search(title)
        if m:
            kind = re.sub(r"\s+", " ", m.group(1).lower())
            base = 14.0 if "breakthrough" in kind else 8.0 if "priority" in kind else 9.0
            found.append(Catalyst("designation", f"FDA {kind} designation", "up", base * bf))

        # --- money relative to the company
        if CONTRACT_RE.search(title) and not OFFERING_RE.search(title):
            found.extend(self._contract(li, title, text))
        if VERDICT_RE.search(title) and cap:
            size = max(_deal_amounts(title) or [0.0])
            r = size / cap if size else 0.0
            found.append(
                Catalyst(
                    "verdict",
                    "Court/jury win" + (f" worth {r:.0%} of market cap" if r else ""),
                    "up",
                    max(10.0 * bf, relative_move(r)),
                    relative=r or None,
                )
            )

        # --- specials of the 2025–26 tape
        crypto = bool(CRYPTO_TREASURY_RE.search(text))
        if crypto:
            size = max(_deal_amounts(title) or [0.0])
            r = size / cap if (size and cap) else None
            found.append(
                Catalyst(
                    "crypto_treasury",
                    "Crypto treasury pivot" + (f" = {r:.0%} of market cap" if r else "") + " (often fades)",
                    "up",
                    max(30.0 * bf, relative_move(r) if r else 0.0),
                    relative=r,
                )
            )
        if SHORT_REPORT_RE.search(title):
            named = next((n for n in SHORT_SELLERS if n in text.lower() or n.split()[0] in text.lower()), "")
            found.append(
                Catalyst(
                    "short_report",
                    f"Short-seller report{f' ({named.title()})' if named else ''}",
                    "down",
                    (30.0 if named else 20.0) * max(bf, 0.6),
                )
            )

        # --- dilution and distress
        if OFFERING_RE.search(title) and not partner and not crypto and not gov:
            size = max(_deal_amounts(text) or [0.0])
            r = size / cap if (size and cap) else None
            exp = min(40.0, 6.0 + 70.0 * r) if r is not None else 10.0
            found.append(
                Catalyst(
                    "offering",
                    "Share offering" + (f" = {r:.0%} of market cap" if r else ""),
                    "down",
                    exp,
                    relative=r,
                )
            )
        if CHAPTER11_RE.search(title):
            found.append(Catalyst("bankruptcy", "Bankruptcy filing", "down", 50.0))
        if DELIST_RE.search(title):
            found.append(Catalyst("delisting", "Delisting / suspension", "down", 25.0))
        if GOING_CONCERN_RE.search(title):
            found.append(Catalyst("going_concern", "Going-concern doubt", "down", 18.0 * max(bf, 0.6)))
        if RESTATE_RE.search(title):
            found.append(
                Catalyst(
                    "accounting", "Accounting problem (restatement / auditor)", "down", 22.0 * max(bf, 0.6)
                )
            )
        if DEFAULT_RE.search(title):
            found.append(Catalyst("default", "Debt default", "down", 25.0 * max(bf, 0.6)))
        if REVERSE_SPLIT_RE.search(title):
            found.append(Catalyst("reverse_split", "Reverse split", "down", 10.0))

        # --- re-ratings
        if INDEX_RE.search(title):
            found.append(
                Catalyst(
                    "index_add",
                    "Index inclusion",
                    "up",
                    9.0 * (1.0 if li.band in ("mid cap", "small cap") else 0.8),
                )
            )
        if GUIDE_DOWN_RE.search(title):
            found.append(Catalyst("guidance_cut", "Guidance cut", "down", 25.0 * bf))
        elif GUIDE_UP_RE.search(title):
            found.append(Catalyst("guidance_raise", "Guidance raised", "up", 16.0 * bf))
        if UPLIST_RE.search(title):
            found.append(Catalyst("uplisting", "Uplisting to a major exchange", "up", 8.0 * bf))

        # --- a halt on a small cap is news about to drop
        code = str(extra.get("halt_code", "")).upper()
        if code in ("T1", "T12", "H10", "H11"):
            found.append(
                Catalyst(
                    "halt",
                    f"Halted: {extra.get('halt_reason', code)}",
                    "mixed" if code in ("T1", "T12") else "down",
                    30.0 * max(bf, 0.6),
                )
            )

        if not found:
            if ROUTINE_RE.search(title):
                return Catalyst("routine", "Routine company update", "mixed", 0.0)
            return None
        return max(found, key=lambda c: c.expected)

    # ------------------------------------------------------------------ helpers

    def _mentions(self, li: Listing, text: str) -> list[int]:
        """Character offsets where the listing is named (ticker in parentheses, cashtag, or name)."""
        pos = [m.start() for m in re.finditer(rf"(?<![\w$])\$?{re.escape(li.symbol)}(?![\w])", text)]
        words = li.name.split()
        for n in (3, 2, 1):
            if len(words) >= n:
                stem = " ".join(words[:n]).rstrip(",.")
                if n == 1 and len(stem) < 5:
                    continue
                i = text.lower().find(stem.lower())
                if i >= 0:
                    pos.append(i)
                    break
        return sorted(pos)

    def _deal_role(self, li: Listing, title: str) -> str | None:
        if ACQUIRED_RE.search(title):
            # "X to be acquired by Y": the listing is the target unless it's named after "by"
            m = re.search(r"\b(?:acquired|bought)\s+by\b", title, re.I)
            if m:
                where = self._mentions(li, title)
                if where and min(where) > m.end():
                    return "acquirer"
            return "target"
        m = ACQUIRE_VERB_RE.search(title)
        if not m:
            return None
        where = self._mentions(li, title)
        if not where:
            return None
        return "target" if max(where) > m.start() and min(where) > m.start() else "acquirer"

    def _acquired(self, li: Listing, text: str) -> Catalyst:
        m = _PREMIUM_RE.search(text)
        if m:
            prem = float(m.group(1) or m.group(2))
            return Catalyst(
                "acquired",
                f"Being acquired at a {prem:.0f}% premium",
                "up",
                max(prem, 5.0),
                detail=f"premium {prem:.0f}% (stated)",
            )
        per = _PER_SHARE_RE.search(text)
        if per and li.price:
            offer = float(per.group(1))
            prem = (offer / li.price - 1.0) * 100.0
            if -5.0 < prem < 400.0:
                return Catalyst(
                    "acquired",
                    f"Being acquired at ${offer:g}/share ({prem:+.0f}% vs ${li.price:g})",
                    "up" if prem >= 0 else "down",
                    max(abs(prem), 3.0),
                    detail=f"premium {prem:.0f}% vs last ${li.price:g}",
                )
        return Catalyst("acquired", "Being acquired", "up", 35.0 * max(BAND_FACTOR.get(li.band, 0.8), 0.6))

    def _partner(self, li: Listing, text: str) -> str | None:
        own = li.name.lower()
        for m in _PARTNER_RE.finditer(text):
            name = MEGA_PARTNERS[m.group(1).lower()]
            if name.lower() in own or m.group(1).lower() in own:
                continue  # NVIDIA's own news is not a partnership for NVIDIA
            return name
        return None

    def _contract(self, li: Listing, title: str, text: str) -> list[Catalyst]:
        sizes = _deal_amounts(title) or _deal_amounts(text)
        gov = _GOV_BUYER_RE.search(text)
        buyer = f" from {gov.group(1)}" if gov else ""
        if not sizes or not li.market_cap:
            if gov:
                return [
                    Catalyst(
                        "contract", f"Government contract{buyer}", "up", 10.0 * BAND_FACTOR.get(li.band, 0.8)
                    )
                ]
            return []
        size = max(sizes)
        r = size / li.market_cap
        mult = (1.2 if gov else 1.0) * (0.6 if CEILING_RE.search(title) else 1.0)
        exp = relative_move(r) * mult
        if exp < 3:
            return []
        return [Catalyst("contract", f"Contract{buyer} worth {r:.0%} of market cap", "up", exp, relative=r)]
