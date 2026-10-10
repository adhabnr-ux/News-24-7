"""Binary events announced in advance: know the date before the move.

Small biotechs tell the market *when* the coin will be flipped, weeks ahead:

* "Kodiak Sciences to Present Topline Results on September 28, 2026 from DAYBREAK Pivotal
  Phase 3 Study…" (published Sep 25; the stock went +178% on the 28th)
* "FDA Accepts NDA for Priority Review; PDUFA Target Action Date of June 30, 2027"
* "FDA Advisory Committee Meeting Scheduled for July 29, 2026 to Review Deramiocel"

``extract_event`` turns such a headline (and its summary, where the date often sits) into a
calendar entry, so the Calendar tab and the morning Brief show "Tomorrow 08:30 ET · KOD Phase 3
topline" and you are positioned before the print instead of reading about it after.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_DATE_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(20\d\d))?",
    re.I,
)
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\s*(?:ET|EDT|EST|Eastern)\b", re.I)

KINDS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "pdufa",
        re.compile(r"\b(?:pdufa|target\s+action\s+date|prescription\s+drug\s+user\s+fee)\b", re.I),
        "FDA decision (PDUFA)",
    ),
    (
        "adcom",
        re.compile(
            r"\badvisory\s+committee\b.{0,40}\b(?:meeting|scheduled|to\s+review|will\s+(?:meet|review|convene)|date)\b",
            re.I,
        ),
        "FDA advisory committee",
    ),
    (
        # index entry/exit: funds that track it must trade at the close before the effective date
        "index",
        re.compile(
            r"\b(?:to\s+join|joins?|will\s+join|set\s+to\s+join|to\s+be\s+added\s+to|will\s+be\s+added\s+to|added\s+to|"
            r"to\s+replace\b.{0,60}\b(?:on|in)|replac\w+\b.{0,60}\bin|to\s+be\s+removed\s+from|removed\s+from)\s+(?:the\s+)?"
            r"(?:s&p\s*(?:500|midcap\s*400|smallcap\s*600)|nasdaq[-\s]?100|russell\s*(?:1000|2000|3000)|dow\s+jones\s+industrial)",
            re.I,
        ),
        "index change",
    ),
    (
        # medical meetings: "data at ESMO 2026 Presidential Symposium", "late-breaking oral presentation"
        "conference",
        re.compile(
            r"\b(?:presidential\s+symposium|late[-\s]breaking|plenary\s+session|oral\s+presentation)\b.{0,80}"
            r"|\bto\s+present\b.{0,60}\b(?:phase\s*(?:2b?|3|iii)|pivotal)\b.{0,40}\b(?:at|during)\s+(?:the\s+)?[A-Z]",
            re.I,
        ),
        "data at a medical meeting",
    ),
    (
        "readout",
        re.compile(
            r"\bto\s+(?:present|report|announce|host|release|share|unveil)\b.{0,60}\b(?:topline|top-line|"
            r"pivotal|phase\s*(?:2b?|3|iii|ii)|registrational|primary\s+endpoint)",
            re.I,
        ),
        "trial readout",
    ),
)
_PHASE_RE = re.compile(r"\b(phase\s*(?:1/2|i/ii|2b|2|3|iii|ii))\b", re.I)
_PIVOTAL_RE = re.compile(r"\b(pivotal|registrational)\b", re.I)


_INDEX_NAME_RE = re.compile(
    r"\b(s&p\s*(?:500|midcap\s*400|smallcap\s*600)|nasdaq[-\s]?100|russell\s*(?:1000|2000|3000)|dow\s+jones\s+industrial)",
    re.I,
)
_INDEX_NAMES = {
    "s&p500": "S&P 500",
    "s&pmidcap400": "S&P MidCap 400",
    "s&psmallcap600": "S&P SmallCap 600",
    "nasdaq100": "Nasdaq-100",
    "russell1000": "Russell 1000",
    "russell2000": "Russell 2000",
    "russell3000": "Russell 3000",
    "dowjonesindustrial": "Dow Jones Industrial Average",
}
_AT_OPEN_RE = re.compile(r"\b(?:prior\s+to|before)\s+(?:the\s+)?(?:market\s+open|open(?:ing)?\b)", re.I)


def _parse_dates(text: str, today: date) -> list[date]:
    out = []
    for mon, day, year in _DATE_RE.findall(text):
        m = _MONTHS.get(mon.lower().rstrip("."))
        if not m:
            continue
        try:
            if year:
                d = date(int(year), m, int(day))
            else:  # "on September 28": the next one
                d = date(today.year, m, int(day))
                if d < today - timedelta(days=1):
                    d = date(today.year + 1, m, int(day))
        except ValueError:
            continue
        out.append(d)
    return out


def _parse_time(text: str) -> str:
    m = _TIME_RE.search(text)
    if not m:
        return ""
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if h > 12 or mins > 59:
        return ""
    if ap.startswith("p") and h != 12:
        h += 12
    if ap.startswith("a") and h == 12:
        h = 0
    return f"{h:02d}:{mins:02d}"


def extract_event(
    title: str, summary: str = "", symbol: str = "", now: float | None = None, max_days: int = 400
) -> dict[str, Any] | None:
    """A scheduled binary event in this headline, as a calendar entry, or None."""
    text = f"{title} — {summary[:700]}" if summary else title
    kind = label = ""
    for k, rx, lbl in KINDS:
        if rx.search(title) or (summary and rx.search(summary[:700]) and k != "readout"):
            kind, label = k, lbl
            break
    if not kind or not symbol:
        return None
    today = datetime.fromtimestamp(now, ET).date() if now else datetime.now(ET).date()
    # the date nearest the keyword wins: "…on September 28, 2026 from DAYBREAK…"
    dates = [d for d in _parse_dates(title, today) or _parse_dates(text, today) if d >= today]
    dates = [d for d in dates if (d - today).days <= max_days]
    if not dates:
        return None
    when = min(dates)
    phase = _PHASE_RE.search(title) or _PIVOTAL_RE.search(title)  # "Phase 3" says more than "pivotal"
    what = label
    if kind in ("readout", "conference") and phase:
        what = f"{phase.group(1).title()} {'trial readout' if kind == 'readout' else 'data at a medical meeting'}"
    what = re.sub(r"\s+", " ", what.replace("Phase", "Phase "))
    clock = _parse_time(text)
    if kind == "index":
        idx = _INDEX_NAME_RE.search(text)
        name = _INDEX_NAMES.get(re.sub(r"[\s-]+", "", idx.group(1).lower()), idx.group(1)) if idx else "index"
        leaving = bool(re.search(r"\bremov|\bdelet|\bdrop", title, re.I))
        what = f"{'leaves' if leaving else 'joins'} the {name}" + (
            " (effective at the open)" if _AT_OPEN_RE.search(text) else ""
        )
        clock = clock or ("09:30" if _AT_OPEN_RE.search(text) else "")
    return {
        "id": f"{symbol}:{when.isoformat()}:{kind}",
        "date": when.isoformat(),
        "time": clock,
        "title": f"{symbol} {what}" if kind == "index" else f"{symbol} · {what}",
        "kind": "binary",
        "event": kind,
        "symbol": symbol,
        "headline": title[:200],
    }
