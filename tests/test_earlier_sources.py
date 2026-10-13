"""Earlier, closer-to-the-source signals: insiders buying (SEC Form 4), activist short sellers'
own sites, and the regulators whose single decisions move whole groups (FCC, CMS, DOE, NRC, S&P
Dow Jones Indices). Each one is the first public trace of a move that the wires repeat later."""

from __future__ import annotations

import re
import time

import pytest

from news247.analysis.scorer import Scorer
from news247.config import build_config
from news247.market.universe import Universe
from news247.models import NewsItem, Severity
from news247.models import SourceTier as T
from news247.sources import SOURCE_TYPES, SourceContext, build_sources
from news247.sources.pagewatch import PageWatchSource
from news247.sources.sec_insider import SECInsiderSource, _money, _owner_name, parse_form4

from .conftest import fixture_text

UNI = {
    "ACMB": {"name": "Acme Biotech, Inc. Common Stock", "mcap": 150e6, "price": 3.2},
    "IEP": {"name": "Icahn Enterprises L.P. - Depositary units", "mcap": 7e9, "price": 20.0},
    "HUM": {"name": "Humana Inc. Common Stock", "mcap": 30e9, "price": 250.0},
}


def ctx(http=None, **kw) -> SourceContext:
    return SourceContext(http=http, user_agent="Test/1.0 (t@example.com)", **kw)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def scorer() -> Scorer:
    cfg = build_config({})
    s = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    s.attach_universe(Universe.stub(UNI), **cfg.smallcap.filters())
    return s


def score(s: Scorer, title: str, tier: T = T.MEDIA, entities: tuple[str, ...] = (), boost: float = 0):
    item = NewsItem(
        source="t",
        title=title,
        url="u",
        tier=tier,
        published=time.time(),
        extra={"entities": list(entities), "boost": boost},
    )
    return s.score(item)


# --------------------------------------------------------------------------- Form 4


FORM4 = fixture_text("sec_form4_purchase.txt")


def form4(**subs: str) -> str:
    doc = FORM4
    for old, new in subs.items():
        doc = doc.replace(old, new)
    return doc


def atom(*entries: tuple[str, str, str, str]) -> str:
    """EDGAR's getcurrent feed: (accession, cik, company, role) rows, newest first."""
    rows = "".join(
        f"<entry><title>4 - {co} ({cik}) ({role})</title>"
        f'<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/{int(cik)}/x-index.htm"/>'
        f'<summary type="html">Filed: 2026-10-09 AccNo: {acc}</summary>'
        f"<updated>2026-10-09T16:31:0{i}-04:00</updated>"
        f"<id>urn:tag:sec.gov,2008:accession-number={acc}</id></entry>"
        for i, (acc, cik, co, role) in enumerate(entries)
    )
    return (
        f'<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">{rows}</feed>'
    )


def test_form4_sums_open_market_purchases_only():
    buy = parse_form4(FORM4)
    assert buy is not None
    assert buy.ticker == "ACMB" and buy.cik == "0001555555" and buy.issuer == "ACME BIOTECH, INC."
    assert buy.role == "CEO" and _owner_name(buy.owner) == "Jane A Smith"
    # 500,000 @ 3.10 + 300,000 @ 3.30; the sale (S) and the option exercise (M) are not buys
    assert buy.shares == 800_000 and buy.value == pytest.approx(2_540_000)
    assert (buy.low, buy.high) == (3.10, 3.30) and buy.dates == ["2026-10-07", "2026-10-08"]
    assert buy.avg_price == pytest.approx(3.175)


def test_form4_without_purchases_is_ignored():
    sale_only = re.sub(r"<transactionCode>P</transactionCode>", "<transactionCode>S</transactionCode>", FORM4)
    assert parse_form4(sale_only) is None
    assert parse_form4("<SEC-DOCUMENT>no xml here</SEC-DOCUMENT>") is None
    assert parse_form4("<ownershipDocument><issuer>broken") is None


@pytest.mark.parametrize(
    "title,role",
    [
        ("Chief Executive Officer", "CEO"),
        ("EVP and CFO", "CFO"),
        ("President and COO", "President"),
        ("Executive Chairman", "Chair"),
        ("General Counsel", "General Counsel"),
    ],
)
def test_form4_roles(title, role):
    assert parse_form4(form4(**{"Chief Executive Officer": title})).role == role


def test_form4_director_and_owner_roles_without_a_title():
    doc = form4(
        **{"<officerTitle>Chief Executive Officer</officerTitle>": "", "<isOfficer>1</isOfficer>": ""}
    )
    assert parse_form4(doc).role == "Director"
    doc = doc.replace("<isDirector>1</isDirector>", "<isDirector>0</isDirector>").replace(
        "<isTenPercentOwner>0</isTenPercentOwner>", "<isTenPercentOwner>1</isTenPercentOwner>"
    )
    assert parse_form4(doc).role == "10% owner"


def test_money_and_names():
    assert _money(2_540_000) == "$2.5M" and _money(310_000) == "$310K"
    assert _owner_name("SMITH JANE A") == "Jane A Smith"
    assert _owner_name("Icahn Carl C") == "Icahn Carl C"  # already mixed case: left alone


def test_purchase_item_title_boost_and_size():
    src = SECInsiderSource({"name": "sec-insider"}, ctx(universe=Universe.stub(UNI)))
    item = src.purchase_item(FORM4, "0001127602-26-031337", published=1_791_570_000.0)
    assert item is not None
    assert item.title == (
        "CEO Jane A Smith buys $2.5M of Acme Biotech, Inc. (ACMB) stock at $3.10-$3.30 (Form 4, open market)"
    )
    assert item.tickers == ["ACMB"] and item.uid == "sec4:0001127602-26-031337"
    assert item.url == "https://www.sec.gov/Archives/edgar/data/1555555/000112760226031337/"
    assert item.tier is T.PRIMARY and item.published == 1_791_570_000.0
    ib = item.extra["insider_buy"]
    assert (
        ib["value"] == 2_540_000
        and ib["role"] == "CEO"
        and ib["share_of_cap"] == pytest.approx(0.016933, rel=1e-3)
    )
    # base 8 + CEO 6 + $1M+ 6 + 1.7% of the company 15
    assert item.extra["boost"] == 35
    assert "1.69% of the company's $0.15B value" in item.summary


def test_purchase_below_min_value_or_without_size_data():
    src = SECInsiderSource({"name": "s", "min_value": 5_000_000}, ctx())
    assert src.purchase_item(FORM4, "a") is None
    src = SECInsiderSource({"name": "s"}, ctx())  # no universe: no size bonus, still reported
    item = src.purchase_item(FORM4, "a")
    assert item.extra["boost"] == 20 and item.extra["insider_buy"]["share_of_cap"] is None


def test_cluster_of_insiders_buying_the_same_week():
    src = SECInsiderSource({"name": "s"}, ctx())
    first = src.purchase_item(FORM4, "a")
    again = src.purchase_item(FORM4, "b")  # the same person filing again is not a cluster
    other = src.purchase_item(form4(**{"SMITH JANE A": "DOE JOHN"}), "c")
    assert first.extra["boost"] == again.extra["boost"] == 20
    assert other.extra["boost"] == 30 and other.extra["insider_buy"]["cluster"] == ["SMITH JANE A"]
    assert "cluster: also bought this week by Jane A Smith" in other.summary


async def test_insider_feed_primes_then_reads_new_filings(server, http, tmp_path):
    import news247.sources.sec_edgar as edgar
    import news247.sources.sec_insider as mod

    feed = {"body": atom(("0000000001-26-000001", "0001555555", "ACME BIOTECH, INC.", "Issuer"))}
    server.on("/cgi-bin/browse-edgar", lambda r: _text(feed["body"]))
    server.on("/files/company_tickers.json", {"0": {"cik_str": 1555555, "ticker": "ACMB", "title": "Acme"}})
    server.on("/Archives/edgar/data/1555555/000000000226000002/0000000002-26-000002.txt", (200, FORM4))
    server.on("/Archives/edgar/data/1555555/000000000226000003/0000000003-26-000003.txt", (500, "down"))
    old = (edgar.CURRENT_URL, edgar.TICKERS_URL, mod.CURRENT_URL, mod.ARCHIVE_URL)
    edgar.TICKERS_URL = server.url("/files/company_tickers.json")
    mod.CURRENT_URL = server.url("/cgi-bin/browse-edgar") + "?type={form}&count={count}"
    mod.ARCHIVE_URL = server.url("/") + "Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
    try:
        src = SECInsiderSource({"name": "sec-insider"}, ctx(http, data_dir=tmp_path))
        assert await src.fetch() == []  # start-up: what is already on the list is history
        assert not any("/Archives/" in r["path"] for r in server.requests)
        feed["body"] = atom(
            ("0000000003-26-000003", "0001555555", "ACME BIOTECH, INC.", "Issuer"),  # unreadable
            ("0000000002-26-000002", "0001555555", "ACME BIOTECH, INC.", "Issuer"),
            ("0000000002-26-000002", "0001999999", "SMITH JANE A", "Reporting"),  # the owner's row
            ("0000000009-26-000009", "0007777777", "PRIVATE FUND LP", "Issuer"),  # no ticker
            ("0000000001-26-000001", "0001555555", "ACME BIOTECH, INC.", "Issuer"),  # seen
        )
        items = await src.fetch()
        assert [i.uid for i in items] == ["sec4:0000000002-26-000002"]
        archive = [r["path"] for r in server.requests if "/Archives/" in r["path"]]
        assert len(archive) == 2  # the private filer and the seen filing were never fetched
        assert await src.fetch() == []  # nothing is read twice
        assert all(r["headers"]["User-Agent"] == "Test/1.0 (t@example.com)" for r in server.requests)
    finally:
        edgar.CURRENT_URL, edgar.TICKERS_URL, mod.CURRENT_URL, mod.ARCHIVE_URL = old


def _text(body: str):
    from aiohttp import web

    return web.Response(text=body, content_type="application/atom+xml")


def test_ceo_buying_a_big_slice_of_a_small_company_pushes(scorer):
    src = SECInsiderSource({"name": "s"}, ctx(universe=Universe.stub(UNI)))
    item = src.purchase_item(FORM4, "a")
    a = scorer.score(item)
    assert a.severity >= Severity.HIGH, a.reasons
    assert a.tickers[0] == "ACMB"
    # a director's $300K buy in a $30B company is information, not an alert
    small = form4(
        **{
            "<officerTitle>Chief Executive Officer</officerTitle>": "",
            "<isOfficer>1</isOfficer>": "",
            "<value>500000</value>": "<value>1000</value>",
            "<value>300,000</value>": "<value>1200</value>",
            "<value>3.10</value>": "<value>250</value>",
            "<value>3.30</value>": "<value>250</value>",
            "ACMB": "HUM",
            "ACME BIOTECH, INC.": "HUMANA INC",
        }
    )
    big = SECInsiderSource({"name": "s"}, ctx(universe=Universe.stub(UNI))).purchase_item(small, "b")
    assert big is not None and big.extra["boost"] == 8
    assert scorer.score(big).severity < Severity.HIGH


def test_registered_and_in_the_defaults():
    assert "sec_insider" in SOURCE_TYPES
    built = {s.name: s for s in build_sources(build_config({}).sources, ctx())}
    assert isinstance(built["sec-insider-buys"], SECInsiderSource)
    assert built["sec-insider-buys"].interval == 30 and built["sec-insider-buys"].min_value == 250_000
    for name in (
        "fcc-headlines",
        "cms-newsroom",
        "doe-newsroom",
        "nrc-news",
        "spdji-press",
        "short-muddy-waters",
        "short-culper",
        "gnews-short-reports",
        "gnews-spacex-deals",
        "gnews-gse-investors",
    ):
        assert name in built, name
        assert built[name].tier in (T.PRIMARY, T.MEDIA)
    assert built["sec-8k"].forms and "SC 14D9" in built["sec-8k"].forms


# --------------------------------------------------------------------------- short sellers


def short_watches() -> dict[str, PageWatchSource]:
    built = build_sources(build_config({}).sources, ctx())
    return {s.name: s for s in built if s.name.startswith("short-")}


def test_every_short_seller_watch_accepts_reports_and_rejects_navigation():
    watches = short_watches()
    assert len(watches) >= 12
    for name, src in watches.items():
        host = re.sub(r"^https?://(www\.)?", "", src.url).split("/")[0]
        pat = src.pattern
        for good in (
            f"https://{host}/super-micro-fresh-evidence-of-accounting-manipulation/",
            f"https://www.{host}/2026/10/acme-biotech-the-emperor-has-no-trials",
            f"https://{host}/wp-content/uploads/2026/10/ACMB-report.pdf",
        ):
            assert pat.search(good), (name, good)
        for bad in (
            f"https://{host}/about-us/",
            f"https://{host}/contact/",
            f"https://{host}/category/short-reports/",
            f"https://{host}/privacy-policy",
            f"https://{host}/research/",
            f"https://{host}/",
            f"https://{host}/terms-of-use-and-disclaimer/",
            "https://twitter.com/some-other-site-link",
        ):
            assert not pat.search(bad), (name, bad)


async def test_a_new_short_report_is_a_high_down_alert(server, http, scorer):
    page = {
        "html": (
            '<a href="/about-us/">About Us</a><a href="/contact/">Contact</a>'
            '<a href="/old-report-on-someone-else/"><h2>Older Report</h2></a>'
        )
    }
    server.on("/", lambda r: _html(page["html"]))
    host = server.url("/").split("//")[1].rstrip("/")
    src = PageWatchSource(
        {
            "name": "short-test",
            "url": server.url("/"),
            "link_pattern": short_watches()["short-culper"].pattern.pattern.replace(
                r"culperresearch\.com", re.escape(host)
            ),
            "tier": "primary",
            "entities": ["Culper Research"],
            "boost": 15,
        },
        ctx(http),
    )
    # (the runner keeps the first poll as the baseline; fetch() itself returns every match)
    assert [i.title for i in await src.fetch()] == ["Older Report"]
    page["html"] += (
        '<a href="/icahn-enterprises-the-corporate-raider-throwing-stones-from-his-glass-house/">'
        "<h2>Icahn Enterprises: The Corporate Raider Throwing Stones From His Glass House</h2></a>"
    )
    items = [i for i in await src.fetch() if i.title != "Older Report"]
    assert [i.title for i in items] == [
        "Icahn Enterprises: The Corporate Raider Throwing Stones From His Glass House"
    ]
    a = scorer.score(items[0])
    assert a.severity >= Severity.HIGH, a.reasons
    assert a.direction == "down" and "short_seller_report" in a.themes
    assert "IEP" in a.tickers  # the named target, from the listed universe


def _html(body: str):
    from aiohttp import web

    return web.Response(text=f"<html><body>{body}</body></html>", content_type="text/html")


@pytest.mark.parametrize(
    "title,floor",
    [
        ("Icahn Enterprises shares plunge 20% after Hindenburg short report", Severity.HIGH),
        ("Muddy Waters is short AppLovin, alleging inflated ad results", Severity.MEDIUM),
        (
            "Short seller Fuzzy Panda targets Acme Biotech, citing undisclosed related parties",
            Severity.MEDIUM,
        ),
    ],
)
def test_short_seller_headlines(scorer, title, floor):
    a = score(scorer, title)
    assert a.severity >= floor, a.reasons
    assert a.direction == "down" and "short_seller_report" in a.themes


@pytest.mark.parametrize(
    "title",
    [
        "The Hindenburg disaster: 88 years later, new footage emerges",
        "Muddy waters for the Fed as officials split on the path of rates",
        "Blue orca sightings off the coast of Washington delight whale watchers",
        "Spruce Point lighthouse reopens to visitors after restoration",
    ],
)
def test_short_seller_names_in_ordinary_news_are_not_alerts(scorer, title):
    a = score(scorer, title)
    assert "short_seller_report" not in a.themes, a.reasons
    assert a.severity < Severity.HIGH, a.reasons


# --------------------------------------------------------------------------- regulators


@pytest.mark.parametrize(
    "title,tier,entities,theme,tickers,direction",
    [
        (
            "CY 2026 Medicare Advantage and Part D Rate Announcement",
            T.PRIMARY,
            ("CMS",),
            "medicare_advantage_rates",
            {"HUM", "UNH", "CVS"},
            None,
        ),
        (
            "CMS proposes 2027 Medicare Advantage rates nearly flat, well below expectations",
            T.MEDIA,
            (),
            "medicare_advantage_rates",
            {"HUM", "UNH"},
            "down",
        ),
        (
            "Health insurer stocks surge after Medicare Advantage rate hike tops expectations",
            T.MEDIA,
            (),
            "medicare_advantage_rates",
            {"HUM", "CVS"},
            "up",
        ),
        (
            "Department of Energy Announces Selections for Advanced Nuclear Fuel Line Pilot Projects",
            T.PRIMARY,
            ("Department of Energy",),
            "nuclear_policy_award",
            {"OKLO", "LEU"},
            "up",
        ),
        (
            "NRC Approves Construction Permit for Kemmerer Unit 1",
            T.PRIMARY,
            ("NRC",),
            "nrc_licensing",
            {"OKLO", "SMR"},
            "up",
        ),
        (
            "FCC approves SpaceX request to use EchoStar spectrum for Starlink direct-to-cell",
            T.PRIMARY,
            ("FCC",),
            "satellite_carrier_threat",
            {"VZ", "T", "TMUS"},
            None,
        ),
    ],
)
def test_regulator_decisions_tag_the_group(scorer, title, tier, entities, theme, tickers, direction):
    a = score(scorer, title, tier, entities)
    assert theme in a.themes, a.reasons
    assert tickers <= set(a.tickers), a.tickers
    if tier is T.PRIMARY:
        assert a.severity >= Severity.HIGH, a.reasons
    if direction:
        assert a.direction == direction, a.reasons


@pytest.mark.parametrize(
    "title,entities",
    [
        ("CMS releases new data on hospital price transparency compliance", ("CMS",)),
        ("Energy Secretary visits national lab to tour supercomputer", ("Department of Energy",)),
        ("FCC Announces Agenda for October Open Meeting", ("FCC",)),
        ("NRC Commissioner to speak at industry conference", ("NRC",)),
        ("Jane Doe approved for nuclear medicine residency", ()),
    ],
)
def test_routine_regulator_posts_stay_low(scorer, title, entities):
    a = score(scorer, title, T.PRIMARY, entities)
    assert a.severity < Severity.HIGH, a.reasons
    assert not {"medicare_advantage_rates", "nuclear_policy_award", "nrc_licensing"} & set(a.themes)


def test_spdji_pattern_takes_index_changes_only():
    built = {s.name: s for s in build_sources(build_config({}).sources, ctx())}
    pat = built["spdji-press"].pattern
    assert pat.search(
        "https://press.spglobal.com/2025-09-05-AppLovin,-Robinhood-Markets-and-Emcor-Group-Set-to-Join-S-P-500"
    )
    assert pat.search(
        "https://press.spglobal.com/2026-03-06-Datadog-Set-to-Replace-Juniper-Networks-in-S-P-500"
    )
    assert not pat.search("https://press.spglobal.com/2026-10-01-S-P-Global-Reports-Third-Quarter-Results")
    nrc = built["nrc-news"].pattern
    assert nrc.search("https://www.nrc.gov/docs/ML2627/ML26279A123.pdf")
    assert nrc.search("https://www.nrc.gov/reading-rm/doc-collections/news/2026/26-045.pdf")
    assert not nrc.search("https://www.nrc.gov/about-nrc.html")


def test_partnership_units_are_common_stock_and_match_by_name():
    from news247.market.universe import _NOT_COMMON, name_key

    # MLPs list "Common Units" / "Depositary units"; SPACs list "Units" (a share plus a warrant)
    assert not _NOT_COMMON.search("Icahn Enterprises L.P. - Depositary units")
    assert not _NOT_COMMON.search("Energy Transfer LP Common Units")
    assert _NOT_COMMON.search("Acme Acquisition Corp - Units")
    assert name_key("Icahn Enterprises L.P. - Depositary units") == "icahn enterprises"
    assert name_key("Energy Transfer LP Common Units") == "energy transfer"
    uni = Universe.stub(UNI)
    assert [li.symbol for li in uni.find_named("Icahn Enterprises: The Corporate Raider")] == ["IEP"]
