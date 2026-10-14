"""Form 4 insider buys, read the way the research says they mean something: opportunistic vs
routine insiders (Cohen, Malloy and Pomorski 2012), Rule 10b5-1 plans, the size against the
insider's own stake, offerings dressed as purchases, and the price now against what they paid."""

from __future__ import annotations

import json
from datetime import date

import pytest

from news247.market.live import LiveQuotes
from news247.market.universe import Universe
from news247.sources import SourceContext
from news247.sources.sec_insider import (
    InsiderContext,
    SECInsiderSource,
    classify_history,
    open_market_codes,
    parse_form4,
    submissions_form4,
)

from .conftest import fixture_text

FORM4 = fixture_text("sec_form4_purchase.txt")
UNI = {"ACMB": {"name": "Acme Biotech, Inc. Common Stock", "mcap": 150e6, "price": 3.2}}
POST = "<postTransactionAmounts><sharesOwnedFollowingTransaction><value>{}</value></sharesOwnedFollowingTransaction></postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>"


def ctx(http=None, **kw) -> SourceContext:
    return SourceContext(http=http, user_agent="Test/1.0 (t@example.com)", **kw)  # type: ignore[arg-type]


def with_holdings(doc: str, first_after: int, second_after: int) -> str:
    """Add sharesOwnedFollowingTransaction to the fixture's two purchases (500,000 then 300,000)."""
    rows = doc.split("</transactionAmounts>")
    rows[0] += "</transactionAmounts>" + POST.format(first_after)
    rows[1] += "</transactionAmounts>" + POST.format(second_after)
    return rows[0] + rows[1] + "</transactionAmounts>".join(rows[2:])


def with_footnote(doc: str, text: str) -> str:
    doc = doc.replace(
        "<transactionCode>P</transactionCode>", '<transactionCode>P</transactionCode><footnoteId id="F1"/>'
    )
    return doc.replace(
        "</nonDerivativeTable>",
        f'</nonDerivativeTable><footnotes><footnote id="F1">{text}</footnote></footnotes>',
    )


# --------------------------------------------------------------------------- the filing itself


def test_stake_increase_from_holdings_after_each_purchase():
    # held 400,000 before; +500,000 -> 900,000; +300,000 -> 1,200,000: tripled
    buy = parse_form4(with_holdings(FORM4, 900_000, 1_200_000))
    assert buy.before == 400_000 and buy.after == 1_200_000
    assert buy.stake_increase == pytest.approx(2.0)
    first = parse_form4(with_holdings(FORM4, 500_000, 800_000))  # a first position
    assert first.before == 0 and first.stake_increase == float("inf")
    assert parse_form4(FORM4).stake_increase is None  # holdings not stated


def test_ten_b5_one_box_and_footnote():
    boxed = FORM4.replace("</reportingOwner>", "</reportingOwner>\n    <aff10b5One>1</aff10b5One>")
    assert parse_form4(boxed).plan and not parse_form4(FORM4).plan
    noted = with_footnote(FORM4, "Purchased pursuant to a Rule 10b5-1 trading plan adopted on May 1, 2026.")
    assert parse_form4(noted).plan
    unboxed = FORM4.replace("</reportingOwner>", "</reportingOwner>\n    <aff10b5One>0</aff10b5One>")
    assert not parse_form4(unboxed).plan


@pytest.mark.parametrize(
    "note",
    [
        "Shares purchased in the Issuer's underwritten public offering at the public offering price.",
        "Acquired in a private placement pursuant to a securities purchase agreement.",
        "Purchased in the Company's registered direct offering.",
        "Shares acquired through the Employee Stock Purchase Plan.",
        "Shares purchased through the issuer's dividend reinvestment plan.",
    ],
)
def test_offerings_dressed_as_purchases_are_dropped(note):
    assert parse_form4(with_footnote(FORM4, note)) is None


def test_ordinary_footnotes_do_not_drop_a_purchase():
    note = "The price reported is a weighted average; shares were bought in multiple transactions from $3.05 to $3.35."
    buy = parse_form4(with_footnote(FORM4, note))
    assert buy is not None and buy.value == 2_540_000 and not buy.plan


def test_codes_in_a_filing():
    assert open_market_codes(FORM4) == {"P", "S", "M"}
    assert open_market_codes("no xml") == set()


# --------------------------------------------------------------------------- the insider's history


def test_routine_opportunistic_and_new():
    when = date(2026, 10, 7)
    long = [(date(2021, 3, 1), "x")]
    assert classify_history(long, {2025: True, 2024: True, 2023: True}, when)[0] == "routine"
    kind, note = classify_history(long, {2025: True, 2024: None, 2023: True}, when)
    assert kind == "opportunistic" and "only in 2023, 2025" in note
    assert classify_history(long, {2025: None, 2024: None, 2023: None}, when) == (
        "opportunistic",
        "no October trades in 2023-2025",
    )
    # a grant every October is a filing, not a trade: still opportunistic
    assert classify_history(long, {2025: False, 2024: False, 2023: False}, when)[0] == "opportunistic"
    kind, note = classify_history([(date(2025, 2, 1), "x")], {}, when)
    assert kind == "new" and "since Feb 2025" in note
    assert classify_history([], {}, when) == ("", "")


def test_submissions_form4_list():
    doc = {
        "filings": {
            "recent": {
                "accessionNumber": ["a1", "a2", "a3", "a4"],
                "form": ["4", "3", "4/A", "4"],
                "reportDate": ["2025-10-02", "2020-01-01", "", "bad"],
                "filingDate": ["2025-10-03", "2020-01-02", "2024-10-09", "2023-10-05"],
            }
        }
    }
    assert submissions_form4(doc) == [(date(2025, 10, 2), "a1"), (date(2024, 10, 9), "a3")]
    assert submissions_form4(json.dumps(doc)) == submissions_form4(doc)
    assert submissions_form4({}) == []


async def test_pattern_reads_the_same_month_of_three_years(server, http):
    import news247.sources.sec_insider as mod

    sale = FORM4.replace("<transactionCode>P</transactionCode>", "<transactionCode>S</transactionCode>")
    grant = (
        FORM4.replace("<transactionCode>P</transactionCode>", "<transactionCode>A</transactionCode>")
        .replace("<transactionCode>S</transactionCode>", "<transactionCode>A</transactionCode>")
        .replace("<transactionCode>M</transactionCode>", "<transactionCode>A</transactionCode>")
    )
    rows = [("2025-10-06", "0000000001-25-000001"), ("2024-10-04", "0000000001-24-000001"),
            ("2023-10-09", "0000000001-23-000001"), ("2023-10-20", "0000000001-23-000002"),
            ("2021-05-01", "0000000001-21-000001")]  # fmt: skip
    server.on(
        "/submissions/CIK0001999999.json",
        {"filings": {"recent": {"accessionNumber": [a for _, a in rows], "form": ["4"] * 5,
                                "reportDate": [d for d, _ in rows], "filingDate": [d for d, _ in rows]}}},
    )  # fmt: skip
    docs = {
        "0000000001-25-000001": sale,
        "0000000001-24-000001": FORM4,
        "0000000001-23-000001": grant,  # a grant that October...
        "0000000001-23-000002": sale,  # ... and a sale later the same month
    }
    for acc, body in docs.items():
        server.on(f"/Archives/edgar/data/1999999/{acc.replace('-', '')}/{acc}.txt", (200, body))
    old = (mod.SUBMISSIONS_URL, mod.ARCHIVE_URL)
    mod.SUBMISSIONS_URL = server.url("/") + "submissions/CIK{cik}.json"
    mod.ARCHIVE_URL = server.url("/") + "Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
    try:
        src = SECInsiderSource({"name": "s"}, ctx(http))
        kind, note = await src.pattern("0001999999", date(2026, 10, 7))
        assert kind == "routine" and "2023-2025" in note
        n = len(server.requests)
        assert await src.pattern("0001999999", date(2026, 10, 8)) == (kind, note)  # cached for the month
        assert len(server.requests) == n
        # in March there is no pattern at all
        assert (await src.pattern("0001999999", date(2026, 3, 9)))[0] == "opportunistic"
    finally:
        mod.SUBMISSIONS_URL, mod.ARCHIVE_URL = old


# --------------------------------------------------------------------------- the alert


def test_opportunistic_doubling_ceo_with_the_price_still_there():
    src = SECInsiderSource({"name": "s"}, ctx(universe=Universe.stub(UNI)))
    doc = with_holdings(FORM4, 900_000, 1_200_000)
    info = InsiderContext(pattern="opportunistic", pattern_note="no October trades in 2023-2025",
                          price_now=3.22, price_source="live", off_high_pct=-35.0)  # fmt: skip
    item = src.purchase_item(doc, "a", info=info)
    # base 35 (CEO, $1M+, 1.7% of the company) + doubled 8 + opportunistic 8 + 35% off the high 3
    assert item.extra["boost"] == 54
    assert item.title.endswith("(Form 4, open market, +200% to their stake, opportunistic)")
    ib = item.extra["insider_buy"]
    assert ib["stake_increase"] == pytest.approx(2.0) and ib["pattern"] == "opportunistic"
    assert ib["vs_insider_pct"] == pytest.approx(1.42, abs=0.01)  # $3.22 vs $3.175
    assert "still at or below their price" in item.summary and "35% below the 3-month high" in item.summary


def test_routine_and_planned_buys_are_demoted():
    src = SECInsiderSource({"name": "s"}, ctx(universe=Universe.stub(UNI)))
    planned = FORM4.replace("</reportingOwner>", "</reportingOwner><aff10b5One>1</aff10b5One>")
    item = src.purchase_item(planned, "a", info=InsiderContext(pattern="routine"))
    assert item.extra["boost"] == 35 - 15 - 12
    assert "10b5-1 plan" in item.title and "routine" in item.title
    assert "says little about today" in item.summary


def test_already_run_above_the_insider_price():
    src = SECInsiderSource({"name": "s"}, ctx(universe=Universe.stub(UNI)))
    item = src.purchase_item(FORM4, "a", info=InsiderContext(price_now=4.0, price_source="live"))
    assert "+26.0% vs the insider's $3.17, above their price" in item.summary


async def test_context_uses_live_price_then_the_last_close(server, http):
    import news247.sources.sec_insider as mod

    server.on("/spark", {"spark": {"result": [{"symbol": "ACMB", "response": [{"meta": {"chartPreviousClose": 4.5, "regularMarketPrice": 3.2},
              "timestamp": [1, 2, 3], "indicators": {"quote": [{"close": [5.0, 4.0, 3.2]}]}}]}]}})  # fmt: skip
    old = mod.SPARK_URL
    mod.SPARK_URL = server.url("/spark")
    try:
        live = LiveQuotes()
        import time as _t

        live.update("ACMB", 3.0, 3.2, _t.time(), "alpaca")
        src = SECInsiderSource(
            {"name": "s", "history": False}, ctx(http, universe=Universe.stub(UNI), live=live)
        )
        buy = parse_form4(FORM4)
        info = await src.context_for(buy)
        assert info.price_now == 3.0 and info.price_source == "live"
        assert info.off_high_pct == pytest.approx(-40.0)  # $3.00 vs the $5.00 high
        src2 = SECInsiderSource({"name": "s", "history": False}, ctx(http, universe=Universe.stub(UNI)))
        info2 = await src2.context_for(buy)
        assert info2.price_now == 3.2 and info2.price_source == "last close"
    finally:
        mod.SPARK_URL = old
