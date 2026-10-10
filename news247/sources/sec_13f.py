"""The giants' quarterly holdings (SEC Form 13F-HR): new stakes in small caps, the hour they're filed.

On Feb 14 2024 NVIDIA's first 13F listed 1.73M SoundHound shares; SOUN closed +67% the next
day. A year later the next 13F showed NVIDIA had sold out, and SOUN fell 28%. The filing is
public the moment the SEC accepts it (often after the close), hours before the move. A 13F's
title says nothing about what's inside, so this source reads the holdings table itself:

1. polls each watched filer's EDGAR submissions index for a new 13F-HR
2. reads that filing's information table and the previous quarter's
3. emits one item per **new stake**, **stake raised 2×+** or **exit**, smallest companies
   first: "NVIDIA 13F: new stake in SoundHound AI (SOUN) — 1.73M shares, $3.7M"

The small-cap desk then sizes it (an NVIDIA stake re-rates a micro cap; an exit does the
reverse). Default filers: NVIDIA, Alphabet, Amazon and Berkshire Hathaway; add any CIK.
13F deadlines are 45 days after each quarter (≈Feb 14, May 15, Aug 14, Nov 14): around them
the source polls every 2 minutes.
"""

from __future__ import annotations

import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..http import HTTPError
from ..models import NewsItem, SourceTier
from .base import PollingSource

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
DEFAULT_FILERS = {
    "1045810": "NVIDIA",
    "1652044": "Alphabet",
    "1018724": "Amazon",
    "1067983": "Berkshire Hathaway",
}
DEADLINES = ((2, 14), (5, 15), (8, 14), (11, 14))  # (month, day): 45 days after quarter end


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_info_table(body: bytes) -> dict[str, dict[str, Any]]:
    """13F information table XML -> {cusip: {name, cls, shares, value}} (options excluded,
    multiple rows for one security summed)."""
    root = ET.fromstring(body)
    out: dict[str, dict[str, Any]] = {}
    for el in root.iter():
        if _local(el.tag) != "infoTable":
            continue
        row: dict[str, str] = {}
        for child in el.iter():
            row[_local(child.tag)] = (child.text or "").strip()
        if row.get("putCall"):
            continue  # option positions are not ownership
        cusip = row.get("cusip", "").upper()
        if not cusip:
            continue
        try:
            shares = float(row.get("sshPrnamt") or 0)
            value = float(row.get("value") or 0)
        except ValueError:
            continue
        h = out.setdefault(
            cusip,
            {
                "name": row.get("nameOfIssuer", ""),
                "cls": row.get("titleOfClass", ""),
                "shares": 0.0,
                "value": 0.0,
            },
        )
        h["shares"] += shares
        h["value"] += value
    return out


def _fmt_shares(n: float) -> str:
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.2f}".rstrip("0").rstrip(".") + unit
    return f"{n:.0f}"


def _fmt_usd(v: float) -> str:
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.1f}".rstrip("0").rstrip(".") + unit
    return f"${v:.0f}"


def near_deadline(today: date, days: int = 2) -> bool:
    return any(abs((today - date(today.year, m, d)).days) <= days for m, d in DEADLINES)


class SEC13FSource(PollingSource):
    type_name = "sec_13f"
    default_interval = 600.0
    default_tier = SourceTier.PRIMARY

    def __init__(self, cfg: dict[str, Any], ctx: Any) -> None:
        super().__init__(cfg, ctx)
        filers = cfg.get("filers") or DEFAULT_FILERS
        if isinstance(filers, list):  # [{cik: ..., name: ...}]
            filers = {str(f["cik"]): str(f["name"]) for f in filers}
        self.filers = {str(int(cik)): str(name) for cik, name in dict(filers).items()}
        self.submissions_url = cfg.get("submissions_url", SUBMISSIONS_URL)
        self.archives_url = cfg.get("archives_url", ARCHIVES_URL)
        self.max_per_filing = int(cfg.get("max_per_filing", 12))
        self.raise_factor = float(cfg.get("raise_factor", 2.0))
        self.state_path: Path | None = (ctx.data_dir / "sec13f_state.json") if ctx.data_dir else None
        self.state: dict[str, str] = self._load_state()

    # ------------------------------------------------------------------ state

    def _load_state(self) -> dict[str, str]:
        if self.state_path and self.state_path.is_file():
            try:
                return dict(json.loads(self.state_path.read_text()))
            except (OSError, ValueError):
                pass
        return {}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state))
        except OSError as exc:
            log.warning("[%s] state not saved: %s", self.name, exc)

    @property
    def sec_headers(self) -> dict[str, str]:
        return {"User-Agent": self.ctx.user_agent, **self.extra_headers}

    def next_delay(self) -> float:
        delay = super().next_delay()
        if not self.health.consecutive_errors and near_deadline(datetime.now(timezone.utc).date()):
            return min(delay, 120.0)
        return delay

    # ------------------------------------------------------------------ polling

    async def fetch(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        errors: list[BaseException] = []
        for cik, name in self.filers.items():
            try:
                items.extend(await self.check_filer(cik, name))
            except (HTTPError, ValueError, KeyError, ET.ParseError) as exc:
                errors.append(exc)
                log.debug("[%s] %s: %s", self.name, name, exc)
        if errors and len(errors) == len(self.filers):
            raise errors[0]
        return items

    async def check_filer(self, cik: str, name: str) -> list[NewsItem]:
        resp = await self.ctx.http.get(
            self.submissions_url.format(cik=cik.zfill(10)), headers=self.sec_headers
        )
        filings = self._13f_filings(resp.json())
        if not filings:
            return []
        latest = filings[0]
        if self.state.get(cik) == latest["acc"]:
            return []
        first_sight = cik not in self.state
        self.state[cik] = latest["acc"]
        self._save_state()
        if first_sight and time.time() - latest["accepted"] > 2 * 86400:
            return []  # starting up: last quarter's filing is history, not news
        prev = filings[1] if len(filings) > 1 else None
        cur = await self._holdings(cik, latest["acc"])
        old = await self._holdings(cik, prev["acc"]) if prev else {}
        return self._diff(cik, name, latest, cur, old)

    @staticmethod
    def _13f_filings(sub: dict[str, Any]) -> list[dict[str, Any]]:
        """Original 13F-HR filings, newest first (amendments are skipped)."""
        recent = (sub.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        out = []
        for i, form in enumerate(forms):
            if form != "13F-HR":
                continue
            acc = recent["accessionNumber"][i]
            accepted = recent.get("acceptanceDateTime", [""] * len(forms))[i] or ""
            try:
                ts = datetime.fromisoformat(accepted.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = datetime.fromisoformat(recent["filingDate"][i]).replace(tzinfo=timezone.utc).timestamp()
            out.append(
                {"acc": acc, "accepted": ts, "period": (recent.get("reportDate") or [""] * len(forms))[i]}
            )
        out.sort(key=lambda f: f["accepted"], reverse=True)
        return out

    async def _holdings(self, cik: str, acc: str) -> dict[str, dict[str, Any]]:
        base = self.archives_url.format(cik=cik, acc=acc.replace("-", ""))
        idx = (await self.ctx.http.get(base + "index.json", headers=self.sec_headers)).json()
        names = [
            (it.get("name", ""), int(it.get("size") or 0))
            for it in (idx.get("directory") or {}).get("item", [])
        ]
        xmls = [(n, sz) for n, sz in names if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
        if not xmls:
            return {}
        # the information table: named *infotable* by most filers, otherwise the biggest XML
        pick = next((n for n, _ in xmls if "infotable" in n.lower() or "information" in n.lower()), None)
        pick = pick or max(xmls, key=lambda x: x[1])[0]
        body = (await self.ctx.http.get(base + pick, headers=self.sec_headers)).body
        return parse_info_table(body)

    # ------------------------------------------------------------------ the news

    def _diff(
        self,
        cik: str,
        filer: str,
        filing: dict[str, Any],
        cur: dict[str, dict[str, Any]],
        old: dict[str, dict[str, Any]],
    ) -> list[NewsItem]:
        uni = getattr(self.ctx, "universe", None)
        changes: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] = []
        for cusip, h in cur.items():
            prev = old.get(cusip)
            if prev is None or prev["shares"] <= 0:
                changes.append(("new", cusip, h, None))
            elif h["shares"] >= prev["shares"] * self.raise_factor:
                changes.append(("raised", cusip, h, prev))
        for cusip, prev in old.items():
            if cusip not in cur:
                changes.append(("exited", cusip, prev, prev))

        def listing(h: dict[str, Any]) -> Any:
            return uni.lookup_name(h["name"]) if uni is not None else None

        # smallest companies first: that's where a giant's stake moves the price
        ranked = sorted(changes, key=lambda c: getattr(listing(c[2]), "market_cap", None) or 9e15)
        acc_nodash = filing["acc"].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{filing['acc']}-index.htm"
        items = []
        for kind, cusip, h, prev in ranked[: self.max_per_filing]:
            li = listing(h)
            label = li.name if li is not None else h["name"].title()
            sym = f" ({li.symbol})" if li is not None else ""
            if kind == "new":
                title = f"{filer} 13F: new stake in {label}{sym} — {_fmt_shares(h['shares'])} shares, {_fmt_usd(h['value'])}"
            elif kind == "raised":
                x = h["shares"] / prev["shares"] if prev else 0
                title = f"{filer} 13F: raised stake in {label}{sym} {x:.1f}× — now {_fmt_shares(h['shares'])} shares"
            else:
                title = f"{filer} 13F: exited {label}{sym} — sold all {_fmt_shares(h['shares'])} shares"
            item = self.make_item(
                title=title,
                url=url,
                summary=f"Form 13F-HR for the quarter ended {filing.get('period') or '?'}; CUSIP {cusip}.",
                published=filing["accepted"],
                uid=f"13f:{filing['acc']}:{cusip}:{kind}",
                tickers=[li.symbol] if li is not None else [],
                author=filer,
            )
            item.extra.update({"form": "13F-HR", "cik": cik, "change": kind, "cusip": cusip})
            items.append(item)
        return items
