"""Federal Register public inspection: rules and notices become public here hours to a day
before they are published, and agencies post here before they announce.

Examples: Commerce's Section 232 probes of chips and drugs appeared on public inspection
(2025-04-14) before anyone announced them; BIS's October 2023 chip rules sat on inspection two
days before publication. Regular filings post at 08:45 ET for the next day's issue; "special"
filings post at other times (often 11:15 or 16:15 ET).

API (free, no key): https://www.federalregister.gov/developers/documentation/api/v1
Options: ``agencies`` (list of agency slugs to keep, default: all), ``types`` (e.g.
["Rule", "Proposed Rule", "Notice", "Presidential Document"], default: all).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import NewsItem, SourceTier
from .base import PollingSource

CURRENT_URL = "https://www.federalregister.gov/api/v1/public-inspection-documents/current.json"

# Short names for the agencies whose filings move markets (others use the API's own name).
AGENCY_SHORT = {
    "industry-and-security-bureau": "BIS",
    "trade-representative-office-of-united-states": "USTR",
    "foreign-assets-control-office": "OFAC",
    "commerce-department": "Commerce",
    "international-trade-administration": "ITA",
    "food-and-drug-administration": "FDA",
    "centers-for-medicare-medicaid-services": "CMS",
    "federal-communications-commission": "FCC",
    "securities-and-exchange-commission": "SEC",
    "executive-office-of-the-president": "White House",
    "treasury-department": "Treasury",
    "federal-reserve-system": "Fed",
    "energy-department": "DOE",
    "federal-housing-finance-agency": "FHFA",
}


def _ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class FederalRegisterSource(PollingSource):
    type_name = "federal_register"
    default_interval = 60.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        self.url: str = cfg.get("url", CURRENT_URL)
        self.agencies = {str(a).lower() for a in cfg.get("agencies", [])}
        self.types = {str(t).lower() for t in cfg.get("types", [])}

    async def fetch(self) -> list[NewsItem]:
        resp = await self.ctx.http.get(self.url, headers=self.headers, conditional=True)
        if resp.not_modified:
            return []
        return self.parse(resp.json())

    def parse(self, data: dict[str, Any]) -> list[NewsItem]:
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("unexpected Federal Register response (no 'results' list)")
        out: list[NewsItem] = []
        for doc in data["results"]:
            agencies = [a for a in doc.get("agencies") or [] if isinstance(a, dict)]
            slugs = {str(a.get("slug", "")).lower() for a in agencies}
            if self.agencies and not slugs & self.agencies:
                continue
            kind = str(doc.get("type") or "")
            if self.types and kind.lower() not in self.types:
                continue
            names = [
                AGENCY_SHORT.get(str(a.get("slug", "")).lower()) or a.get("name") or a.get("raw_name") or ""
                for a in agencies
            ]
            who = ", ".join(dict.fromkeys(n for n in names if n)) or "Federal Register"
            subject = " ".join(str(doc.get(k) or "") for k in ("toc_subject", "title") if doc.get(k)).strip()
            special = str(doc.get("filing_type") or "").lower() == "special"
            title = f"Public inspection ({who}{', ' + kind if kind else ''}): {subject or doc.get('document_number', '')}"
            item = self.make_item(
                title=title,
                url=doc.get("html_url") or doc.get("pdf_url") or "",
                summary=str(doc.get("editorial_note") or ""),
                published=_ts(doc.get("filed_at")),
                uid=f"fr-pi-{doc.get('document_number') or doc.get('html_url') or title}",
            )
            item.extra.update(
                {
                    "agencies": sorted(slugs),
                    "doc_type": kind,
                    "special_filing": special,
                    "publication_date": doc.get("publication_date"),
                    "pdf_url": doc.get("pdf_url"),
                    "entities": list(dict.fromkeys([*item.extra.get("entities", []), "Federal Register"])),
                }
            )
            out.append(item)
        return out
