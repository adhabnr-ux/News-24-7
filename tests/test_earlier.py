"""Knowing earlier: binary-event dates captured from headlines, and the giants' 13F stakes."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news247.analysis.catalyst_dates import extract_event
from news247.analysis.smallcap import SmallCapDesk
from news247.config import build_config
from news247.edge import Calendar, brief_push, build_brief
from news247.engine import Engine
from news247.market.universe import Universe
from news247.models import NewsItem
from news247.models import SourceTier as T
from news247.sources import SourceContext, build_sources
from news247.sources.sec_13f import SEC13FSource, near_deadline, parse_info_table
from news247.storage import Storage

ET = ZoneInfo("America/New_York")


def et(*a: int) -> float:
    return datetime(*a, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- binary events


@pytest.mark.parametrize(
    "title,summary,want",
    [
        (
            "Kodiak Sciences to Present Topline Results on September 28, 2026 from DAYBREAK Pivotal Phase 3 Study",
            "Conference call at 8:30 a.m. ET.",
            ("2026-09-28", "08:30", "readout", "KOD · Phase 3 trial readout"),
        ),
        (
            "Acme Biotech Announces FDA Acceptance of NDA with Priority Review",
            "The FDA assigned a PDUFA target action date of June 30, 2027.",
            ("2027-06-30", "", "pdufa", "KOD · FDA decision (PDUFA)"),
        ),
        (
            "FDA Advisory Committee Meeting Scheduled for October 15 to Review Acme's Drug",
            "",
            ("2026-10-15", "", "adcom", "KOD · FDA advisory committee"),
        ),
        (
            "Acme to Report Pivotal Results on January 12",  # no year, already past this year: next one
            "",
            ("2027-01-12", "", "readout", "KOD · Pivotal trial readout"),
        ),
    ],
)
def test_extract_event(title, summary, want):
    ev = extract_event(title, summary, "KOD", now=et(2026, 9, 26, 9))
    assert ev is not None and (ev["date"], ev["time"], ev["event"], ev["title"]) == want
    assert ev["kind"] == "binary" and ev["id"] == f"KOD:{want[0]}:{want[2]}"


@pytest.mark.parametrize(
    "title,summary,symbol",
    [
        ("Acme to Present at the Investor Conference on October 3", "", "ACMB"),  # not a binary event
        ("Acme Reports Topline Results", "", "ACMB"),  # the event itself, no future date
        ("Acme to Present Topline Results on September 1, 2026", "", "ACMB"),  # already past
        ("Acme to Present Topline Phase 3 Results on October 20, 2026", "", ""),  # who? no symbol
        ("Acme Announces Q3 Results", "Readout expected; to report topline Phase 3 data on Nov 2", "ACMB"),
    ],
)
def test_extract_event_ignores(title, summary, symbol):
    assert extract_event(title, summary, symbol, now=et(2026, 9, 26, 9)) is None


def test_calendar_and_brief_carry_binary_events():
    cal = Calendar({"events": []})
    ev = extract_event(
        "Kodiak Sciences to Present Topline Results on September 28, 2026 from Pivotal Phase 3 Study",
        "Call at 8:30 a.m. ET",
        "KOD",
        now=et(2026, 9, 25, 9),
    )
    ev.update(impact=2, note="Kodiak Sciences · $1.7B small cap")
    assert cal.add(ev) and not cal.add(ev)  # once per id
    up = cal.upcoming(days=7, now=et(2026, 9, 27, 8))
    assert [e["title"] for e in up if e["kind"] == "binary"] == ["KOD · Phase 3 trial readout"]
    # the night before: the Brief tells you to position
    b = build_brief([], cal, {}, [], et(2026, 9, 27, 8, 15))
    push = brief_push(b)
    assert push is not None and "Tomorrow 08:30 ET: KOD · Phase 3 trial readout" in push["body"]
    # the morning of
    b = build_brief([], cal, {}, [], et(2026, 9, 28, 7))
    assert "Today 08:30 ET: KOD · Phase 3 trial readout" in brief_push(b)["body"]


async def test_engine_captures_and_remembers_binary_events(tmp_path: Path):
    cfg = build_config(
        {
            "general": {"data_dir": str(tmp_path)},
            "market": {"enabled": False},
            "notify": {"console": {"enabled": False}},
        }
    )
    Universe.stub(
        {
            "KOD": {
                "name": "Kodiak Sciences Inc.",
                "mcap": 1.7e9,
                "price": 32.0,
                "industry": "Biotechnology: Pharmaceutical Preparations",
            }
        }
    ).save(tmp_path / "universe.json.gz")
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), sources=[])
    soon = datetime.fromtimestamp(time.time() + 3 * 86400, ET)
    title = f"Kodiak Sciences to Present Topline Results on {soon:%B} {soon.day}, {soon.year} from DAYBREAK Pivotal Phase 3 Study"
    await eng.on_item(
        NewsItem(
            source="globenewswire",
            title=title,
            tier=T.WIRE,
            tickers=["KOD"],
            url="https://example.com/kod",
            published=time.time() - 5,
        )
    )
    await eng.drain(2)
    (ev,) = [e for e in eng.calendar.upcoming(days=10) if e["kind"] == "binary"]
    assert ev["title"] == "KOD · Phase 3 trial readout" and ev["impact"] == 2 and "small cap" in ev["note"]
    assert ev["url"] == "https://example.com/kod"
    # survives a restart
    eng2 = Engine(cfg, storage=Storage(tmp_path / "e.db"), sources=[])
    assert [e["id"] for e in eng2.calendar.upcoming(days=10) if e["kind"] == "binary"] == [ev["id"]]


# --------------------------------------------------------------------------- 13F


def info_table(rows: list[tuple[str, str, int, int, str]]) -> bytes:
    ns = "http://www.sec.gov/edgar/document/thirteenf/informationtable"
    body = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{c}</cusip>"
        f"<value>{v}</value><shrsOrPrnAmt><sshPrnamt>{s}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
        + (f"<putCall>{pc}</putCall>" if pc else "")
        + "</infoTable>"
        for n, c, s, v, pc in rows
    )
    return f'<?xml version="1.0"?><informationTable xmlns="{ns}">{body}</informationTable>'.encode()


def test_parse_info_table_sums_and_skips_options():
    h = parse_info_table(
        info_table(
            [
                ("SOUNDHOUND AI INC", "836100107", 1_000_000, 2_000_000, ""),
                ("SOUNDHOUND AI INC", "836100107", 730_000, 1_700_000, ""),  # a second manager's row
                ("ARM HOLDINGS PLC", "042068205", 50_000, 7_000_000, "Call"),  # options are not ownership
            ]
        )
    )
    assert set(h) == {"836100107"}
    assert h["836100107"]["shares"] == 1_730_000 and h["836100107"]["value"] == 3_700_000


def test_near_deadline():
    from datetime import date

    assert near_deadline(date(2026, 11, 13)) and near_deadline(date(2027, 2, 16))
    assert not near_deadline(date(2026, 10, 10))


def submissions(accs: list[tuple[str, str]]) -> dict:
    return {
        "cik": "1045810",
        "filings": {
            "recent": {
                "accessionNumber": [a for a, _ in accs],
                "form": ["13F-HR"] * len(accs),
                "filingDate": [t[:10] for _, t in accs],
                "acceptanceDateTime": [t for _, t in accs],
                "reportDate": ["2023-12-31"] * len(accs),
            }
        },
    }


async def test_13f_source_reads_new_raised_and_exited(server, http, tmp_path: Path):
    now = datetime.now(ZoneInfo("UTC"))
    fresh, older = now.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "2023-11-14T16:00:00.000Z"
    server.on(
        "/submissions/CIK0001045810.json",
        submissions([("0001045810-24-000012", fresh), ("0001045810-23-000099", older)]),
    )
    server.on(
        "/archives/1045810/000104581024000012/index.json",
        {
            "directory": {
                "item": [{"name": "primary_doc.xml", "size": 900}, {"name": "infotable.xml", "size": 5000}]
            }
        },
    )
    server.on(
        "/archives/1045810/000104581023000099/index.json",
        {
            "directory": {
                "item": [{"name": "primary_doc.xml", "size": 900}, {"name": "x91.xml", "size": 4000}]
            }
        },
    )
    server.on(
        "/archives/1045810/000104581024000012/infotable.xml",
        (
            200,
            info_table(
                [
                    ("SOUNDHOUND AI INC", "836100107", 1_730_000, 3_700_000, ""),  # new
                    ("ARM HOLDINGS PLC", "042068205", 1_100_000, 147_000_000, ""),  # raised 2.2x
                    ("RECURSION PHARMACEUTICALS INC", "75629V104", 7_700_000, 76_000_000, ""),  # unchanged
                ]
            ).decode(),
        ),
    )
    server.on(
        "/archives/1045810/000104581023000099/x91.xml",
        (
            200,
            info_table(
                [
                    ("ARM HOLDINGS PLC", "042068205", 500_000, 30_000_000, ""),
                    ("RECURSION PHARMACEUTICALS INC", "75629V104", 7_700_000, 50_000_000, ""),
                    ("TUSIMPLE HOLDINGS INC", "90089L108", 3_000_000, 3_000_000, ""),  # exited
                ]
            ).decode(),
        ),
    )
    uni = Universe.stub(
        {
            "SOUN": {"name": "SoundHound AI, Inc.", "mcap": 0.9e9, "price": 4.0},
            "ARM": {"name": "Arm Holdings plc", "mcap": 130e9, "price": 120.0},
            "RXRX": {"name": "Recursion Pharmaceuticals, Inc.", "mcap": 1.5e9, "price": 7.0},
        }
    )
    ctx = SourceContext(
        http=http, user_agent="News247Test/1.0 (test@example.com)", data_dir=tmp_path, universe=uni
    )
    src = SEC13FSource(
        {
            "name": "13f",
            "filers": {"1045810": "NVIDIA"},
            "submissions_url": server.url("").rstrip("/") + "/submissions/CIK{cik}.json",
            "archives_url": server.url("").rstrip("/") + "/archives/{cik}/{acc}/",
        },
        ctx,
    )
    items = await src.fetch()
    titles = [i.title for i in items]
    assert (
        titles[0] == "NVIDIA 13F: new stake in SoundHound AI, Inc. (SOUN) — 1.73M shares, $3.7M"
    )  # smallest first
    assert "NVIDIA 13F: raised stake in Arm Holdings plc (ARM) 2.2× — now 1.1M shares" in titles
    assert "NVIDIA 13F: exited Tusimple Holdings Inc — sold all 3M shares" in titles
    assert not any("Recursion" in t for t in titles)  # unchanged positions are not news
    soun = items[0]
    assert (
        soun.tickers == ["SOUN"]
        and soun.tier is T.PRIMARY
        and soun.uid.startswith("13f:0001045810-24-000012:")
    )
    assert soun.url.endswith("/000104581024000012/0001045810-24-000012-index.htm")
    assert json.loads((tmp_path / "sec13f_state.json").read_text()) == {"1045810": "0001045810-24-000012"}
    assert await src.fetch() == []  # same filing: nothing new
    # a fresh start with last quarter's filing already on file: baseline, not news
    server.on("/submissions/CIK0001045810.json", submissions([("0001045810-23-000099", older)]))
    (tmp_path / "sec13f_state.json").unlink()
    src2 = SEC13FSource(
        {
            "name": "13f",
            "filers": {"1045810": "NVIDIA"},
            "submissions_url": server.url("").rstrip("/") + "/submissions/CIK{cik}.json",
            "archives_url": server.url("").rstrip("/") + "/archives/{cik}/{acc}/",
        },
        ctx,
    )
    assert await src2.fetch() == [] and src2.state == {"1045810": "0001045810-23-000099"}


def test_13f_stake_and_exit_are_sized_for_small_caps():
    d = SmallCapDesk(Universe.stub({"SOUN": {"name": "SoundHound AI, Inc.", "mcap": 0.9e9, "price": 4.0}}))
    new = d.read(
        "NVIDIA 13F: new stake in SoundHound AI, Inc. (SOUN) — 1.73M shares, $3.7M", tickers=["SOUN"]
    )
    out = d.read("NVIDIA 13F: exited SoundHound AI, Inc. (SOUN) — sold all 1.73M shares", tickers=["SOUN"])
    assert (new.catalyst.kind, new.catalyst.direction, new.material) == ("mega_partner", "up", True)
    assert (out.catalyst.kind, out.catalyst.direction, out.material) == ("mega_exit", "down", True)


def test_13f_source_is_registered_and_on_by_default(tmp_path: Path):
    cfg = build_config({"general": {"data_dir": str(tmp_path)}})
    src = next(s for s in cfg.sources if s["type"] == "sec_13f")
    built = build_sources([src], SourceContext(http=None, user_agent="t", data_dir=tmp_path))  # type: ignore[arg-type]
    assert isinstance(built[0], SEC13FSource) and set(built[0].filers.values()) >= {
        "NVIDIA",
        "Berkshire Hathaway",
    }
