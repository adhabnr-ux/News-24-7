"""The trend lane (trends.py): Camillo's method with criteria a machine can hold to.

Every feed is faked here in its real response shape (Wikimedia's per-article pageviews, Apple's
marketing-tools RSS v2, Yahoo's chart API): the build machine can't reach them, Render can.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from news247.analysis.scorer import Scorer
from news247.config import TrendsConfig, build_config
from news247.http import HTTPError
from news247.market.universe import Listing, Universe
from news247.models import NewsItem, Severity
from news247.storage import Storage
from news247.trends import (
    Brand,
    Evidence,
    TrendDesk,
    app_climb,
    format_board,
    judge,
    load_brands,
    parse_app_chart,
    resolve_titles,
    signal_alert_text,
    trend_direction,
    wiki_spike,
    wiki_stats,
)

DAY = 86400.0
NOW = time.time()


# --------------------------------------------------------------------------- fakes


class Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


class FakeHttp:
    """Routes by URL substring; a route can be data, an exception, or a callable(url, params)."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[str] = []

    async def get(self, url: str, headers: Any = None, params: Any = None, **kw: Any) -> Resp:
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return Resp(val(url, params) if callable(val) else val)
        raise HTTPError(404, url)


def wiki_json(views: list[float]) -> dict[str, Any]:
    return {
        "items": [
            {"article": "X", "timestamp": f"2026{i:06d}00", "views": int(v)} for i, v in enumerate(views)
        ]
    }


def yahoo_chart(closes: list[float], start: float, step: float = DAY) -> dict[str, Any]:
    ts = [int(start + i * step) for i in range(len(closes))]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": closes[-1], "chartPreviousClose": closes[0]},
                    "timestamp": ts,
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }


def app_feed(apps: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Apple's RSS v2 shape: feed.results[] with id, name, artistName (rank = order)."""
    return {
        "feed": {
            "title": "Top Free Apps",
            "results": [{"id": i, "name": n, "artistName": a, "kind": "apps"} for i, n, a in apps],
        }
    }


UNIVERSE = Universe(
    [
        Listing("QSR", "Restaurant Brands International Inc. Common Shares", market_cap=27e9, price=62.0),
        Listing("YUM", "Yum! Brands, Inc.", market_cap=39e9, price=140.0),
        Listing("MCD", "McDonald's Corporation Common Stock", market_cap=215e9, price=300.0),
        Listing("DUOL", "Duolingo, Inc. Class A Common Stock", market_cap=14e9, price=310.0),
        Listing("HOOD", "Robinhood Markets, Inc. Class A Common Stock", market_cap=95e9, price=110.0),
        Listing("GME", "GameStop Corporation Common Stock", market_cap=12e9, price=27.0),
        Listing("TINY", "Tiny Apps Inc. Common Stock", market_cap=0.9e9, price=4.0),
        Listing("WRNT", "Warrant Co Warrants", market_cap=1e9, common=False),
    ]
)


def desk(tmp_path, http=None, brands=None, cfg: TrendsConfig | None = None, universe=UNIVERSE) -> TrendDesk:
    return TrendDesk(
        cfg or TrendsConfig(),
        http or FakeHttp(),
        universe,
        Storage(tmp_path / "t.db"),
        brands=brands,
        state_path=tmp_path / "trends_state.json",
    )


def ev(
    kind, ticker="QSR", direction="up", weight="major", brand="Burger King", t=NOW, started=None
) -> Evidence:
    return Evidence(kind, ticker, brand, weight, direction, started or t, t, f"{kind} detail")


# --------------------------------------------------------------------------- the brand map


def test_brand_map_is_well_formed():
    brands = load_brands()
    assert len(brands) >= 45
    names = [b.brand for b in brands]
    assert len(names) == len(set(names)), "duplicate brand"
    for b in brands:
        assert b.weight in ("core", "major"), b
        assert b.ticker.isupper() and 1 <= len(b.ticker) <= 5, b
        assert b.terms and all(t == t.lower() for t in b.terms), b
        assert all(" " not in w for w in b.wiki), f"{b.brand}: wiki titles use underscores"
    by = {b.brand: b for b in brands}
    assert by["Burger King"].ticker == "QSR" and by["Burger King"].weight == "major"
    assert by["Taco Bell"].ticker == "YUM" and by["Duolingo"].weight == "core"


# --------------------------------------------------------------------------- attention


def flat(n: int = 63, level: float = 2000.0) -> list[float]:
    return [level + (37 * i % 11 - 5) * 20 for i in range(n)]  # +-100 views of everyday noise


def test_wiki_spike_needs_a_sustained_rise():
    sustained = flat(63) + [5200, 6100, 6800, 7400, 7100, 6900, 7300]
    s = wiki_spike(sustained)
    assert s is not None
    assert s["ratio"] > 3 and s["days_up"] == 7 and s["first_day"] == 7
    assert s["baseline"] == pytest.approx(2000, abs=120)


def test_one_day_news_spike_is_not_a_trend():
    one_day = flat(66) + [40000, 2200, 2100, 2000]  # 7-day average 3.2x, but only 1 day up
    assert wiki_stats(one_day)["ratio"] > 2
    assert wiki_spike(one_day) is None


def test_small_pages_and_short_series_are_ignored():
    small = flat(63, 300) + [1200] * 7  # 4x, but 1,200 views a day is nobody
    assert wiki_spike(small) is None
    assert wiki_spike([5000.0] * 20) is None
    assert wiki_spike(flat(63) + [3500] * 7) is None  # 1.75x: below the 2x bar


def test_noisy_baseline_raises_the_bar():
    noisy = [2000 if i % 2 else 6000 for i in range(63)] + [9000] * 7  # swings 3x every other day
    s = wiki_stats(noisy)
    assert s["ratio"] >= 2.0 and s["z"] < 4.0
    assert wiki_spike(noisy) is None


def test_spike_started_partway_through_the_week():
    late = flat(66) + [2000, 2000, 2000, 9000, 9500, 9800, 9900]
    s = wiki_spike(late)
    assert s is not None and s["days_up"] == 4 and s["first_day"] == 4


# --------------------------------------------------------------------------- the App Store chart


def test_parse_app_chart():
    chart = parse_app_chart(
        app_feed([("1", "Duolingo", "Duolingo"), ("2", "Temu", "Temu"), ("", "bad", "x")])
    )
    assert chart == [
        {"rank": 1, "id": "1", "name": "Duolingo", "artist": "Duolingo"},
        {"rank": 2, "id": "2", "name": "Temu", "artist": "Temu"},
    ]
    assert parse_app_chart({}) == [] and parse_app_chart(None) == []  # type: ignore[arg-type]


def snaps(ranks: list[int | None], start: float = NOW - 15 * DAY, step: float = 6 * 3600):
    return [(start + i * step, r) for i, r in enumerate(ranks)]


def test_app_climb_from_off_the_chart_into_the_top_50():
    hist = snaps([None] * 50 + [120, 70, 40, 31, 28])  # 55 snapshots ~ 13.5 days
    c = app_climb(hist)
    assert c is not None and c["rank"] == 28 and c["was"] is None
    assert c["since"] == hist[-3][0]  # the run inside the top 50 began at #40


def test_app_already_popular_is_not_a_climb():
    assert app_climb(snaps([30] * 50 + [25, 22, 20])) is None  # always there
    assert app_climb(snaps([140] * 50 + [45, 40, 38])) is None  # was #140: inside the top 150


def test_app_blip_must_hold():
    assert app_climb(snaps([None] * 50 + [30, 70, 40])) is None  # fell out in between
    assert app_climb(snaps([None] * 50 + [30, 40]), hold=2) is not None  # held long enough for hold=2
    assert app_climb(snaps([None, 30, 40]), hold=3) is None  # no history two weeks back


# --------------------------------------------------------------------------- direction from numbers


@pytest.mark.parametrize(
    "title,want",
    [
        ("Burger King visits rose 7.1% year over year in September, Placer.ai data shows", "up"),
        ("Taco Bell foot traffic fell 12% in September, according to Placer.ai", "down"),
        ("Placer.ai: Taco Bell visits -12.2% YoY", "down"),
        ("Sensor Tower: Duolingo downloads jump 40% after viral campaign", "up"),
        ("Chipotle traffic slows sharply in Q3, card data shows", "down"),
        ("Pokémon cards sold out at GameStop stores", None),
        ("COVID-19 era habits", None),
    ],
)
def test_trend_direction(title, want):
    assert trend_direction(title) == want


# --------------------------------------------------------------------------- the filters


CFG = TrendsConfig()


def test_two_kinds_agreeing_on_a_material_unpriced_company_is_high():
    sev, d, why = judge([ev("alt_data"), ev("app", brand="Burger King")], 27e9, -9.0, CFG)
    assert (sev, d) == ("HIGH", "up")
    assert "2 independent kinds" in why[-1]


def test_one_kind_is_medium_until_confirmed():
    sev, d, why = judge([ev("alt_data")], 27e9, -2.0, CFG)
    assert (sev, d) == ("MEDIUM", "up") and "until a second confirms" in why[-1]
    # attention alone has no direction: shown, never pushed
    assert judge([ev("attention", direction=None)], 27e9, 1.0, CFG)[:2] == ("MEDIUM", None)
    # two pieces of the same kind are still one kind
    assert judge([ev("alt_data"), ev("alt_data", brand="Popeyes")], 27e9, 0.0, CFG)[0] == "MEDIUM"


def test_attention_confirms_a_directional_kind():
    assert judge([ev("attention", direction=None), ev("app")], 27e9, 0.0, CFG)[:2] == ("HIGH", "up")


def test_too_big_to_matter():
    sev, _, why = judge([ev("alt_data", weight="core"), ev("app", weight="core")], 215e9, 0.0, CFG)
    assert sev == "LOW" and "too big" in why[0]
    assert judge([ev("alt_data"), ev("app")], 61e9, 0.0, CFG)[0] == "LOW"  # major brand: $60B cap
    assert judge([ev("alt_data", weight="auto"), ev("app", weight="auto")], 25e9, 0.0, CFG)[0] == "LOW"
    assert judge([ev("alt_data", weight="auto"), ev("app", weight="auto")], 15e9, 0.0, CFG)[0] == "HIGH"
    sev, _, why = judge([ev("alt_data"), ev("app")], None, 0.0, CFG)
    assert sev == "LOW" and "size unknown" in why[0]


def test_priced_in():
    sev, _, why = judge([ev("alt_data"), ev("app")], 27e9, 18.0, CFG)
    assert sev == "LOW" and "priced in" in why[0]
    assert (
        judge([ev("alt_data", direction="down"), ev("demand", direction="down")], 27e9, -16.0, CFG)[0]
        == "LOW"
    )
    # moving against the trend is the opportunity, not a reason to drop it
    assert (
        judge([ev("alt_data", direction="down"), ev("demand", direction="down")], 27e9, 16.0, CFG)[0]
        == "HIGH"
    )
    # direction-free evidence: a 15% move either way means the market noticed
    assert judge([ev("attention", direction=None)], 27e9, -20.0, CFG)[0] == "LOW"


def test_mixed_evidence_is_low():
    sev, d, why = judge([ev("alt_data", direction="up"), ev("app", direction="down")], 27e9, 0.0, CFG)
    assert (sev, d) == ("LOW", "mixed") and "both ways" in why[-1]


# --------------------------------------------------------------------------- evidence from the news


@pytest.fixture
def scorer():
    cfg = build_config({})
    return Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)


def test_tracker_headline_maps_brand_to_company(tmp_path, scorer):
    d = desk(tmp_path)
    item = NewsItem(
        source="t", title="Burger King visits rose 7.1% year over year in September, Placer.ai data shows"
    )
    an = scorer.score(item)
    assert "consumer_data_signal" in an.themes and an.severity < Severity.HIGH  # not a news push
    assert d.from_news(item, an) == ["QSR"]
    (e,) = d.evidence["QSR"]
    assert (e.kind, e.brand, e.weight, e.direction) == ("alt_data", "Burger King", "major", "up")
    assert d.from_news(item, an) == []  # the same (kind, brand) counts once inside the window


def test_sell_out_headline_is_demand(tmp_path, scorer):
    d = desk(tmp_path)
    item = NewsItem(source="t", title="Pokémon cards sold out at GameStop stores as collectors line up")
    an = scorer.score(item)
    assert "demand_signal" in an.themes
    assert d.from_news(item, an) == ["GME"]
    assert d.evidence["GME"][0].kind == "demand" and d.evidence["GME"][0].direction == "up"


def test_unmapped_brand_falls_back_to_a_tagged_listed_company(tmp_path, scorer):
    d = desk(tmp_path, brands=[])
    item = NewsItem(source="t", title="Sensor Tower: Duolingo downloads jump 40% after viral campaign")
    an = scorer.score(item)
    assert d.from_news(item, an) == ["DUOL"]
    assert d.evidence["DUOL"][0].weight == "auto"


def test_ordinary_news_is_not_trend_evidence(tmp_path, scorer):
    d = desk(tmp_path)
    for t in ("Nvidia sold out of Blackwell chips through 2026", "Burger King names new CEO"):
        item = NewsItem(source="t", title=t)
        assert d.from_news(item, scorer.score(item)) == []
    assert d.evidence == {}


def test_old_news_is_not_fresh_evidence(tmp_path, scorer):
    d = desk(tmp_path)
    item = NewsItem(
        source="t",
        title="Taco Bell foot traffic fell 12% in September, according to Placer.ai",
        published=NOW - 20 * DAY,
    )
    d.from_news(item, scorer.score(item))
    assert d.evidence["YUM"][0].detected == pytest.approx(NOW - 20 * DAY)
    assert d.evidence["YUM"][0].direction == "down"


# --------------------------------------------------------------------------- the scanners


# MediaWiki's answer (formatversion=2) to titles=Burger King|Taco Bell|Pokemon cards|Not A Real Article:
# the shape of query.normalized / query.redirects / query.pages as the API returns them
TITLES_ANSWER = {
    "batchcomplete": True,
    "query": {
        "normalized": [{"fromencoded": False, "from": "pokemon cards", "to": "Pokemon cards"}],
        "redirects": [{"from": "Pokemon cards", "to": "Pokémon Trading Card Game"}],
        "pages": [
            {"pageid": 1, "ns": 0, "title": "Burger King"},
            {"pageid": 2, "ns": 0, "title": "Taco Bell"},
            {"pageid": 3, "ns": 0, "title": "Pokémon Trading Card Game"},
            {"ns": 0, "title": "Not A Real Article", "missing": True},
        ],
    },
}


def test_resolve_titles():
    canonical, gone = resolve_titles(
        TITLES_ANSWER, ["Burger_King", "Taco_Bell", "pokemon_cards", "Not_A_Real_Article"]
    )
    assert canonical == {
        "Burger_King": "Burger_King",
        "Taco_Bell": "Taco_Bell",
        "pokemon_cards": "Pokémon_Trading_Card_Game",
    }
    assert gone == {"Not_A_Real_Article"}
    assert resolve_titles({}, ["A_B"]) == ({"A_B": "A_B"}, set())


async def test_wiki_scan_finds_spikes_and_reports_bad_titles(tmp_path):
    spike = flat(63) + [5200, 6100, 6800, 7400, 7100, 6900, 7300]
    http = FakeHttp(
        {
            "en.wikipedia.org/w/api.php": TITLES_ANSWER,
            "/Burger_King/": wiki_json(spike),
            "/Taco_Bell/": wiki_json(flat(70)),
            "/Pok%C3%A9mon_Trading_Card_Game/": wiki_json([20.0] * 70),
        }
    )
    brands = [
        Brand("Burger King", "QSR", "major", wiki=["Burger_King"]),
        Brand("Taco Bell", "YUM", "major", wiki=["Taco_Bell"]),
        Brand("Pokémon TCG", "GME", "major", wiki=["pokemon_cards"]),
        Brand("Nope", "YUM", "major", wiki=["Not_A_Real_Article"]),
    ]
    d = desk(tmp_path, http=http, brands=brands)
    assert await d.scan_wiki(NOW) == ["QSR"]
    e = d.evidence["QSR"][0]
    assert e.kind == "attention" and e.direction is None and "Wikipedia views" in e.detail
    assert e.started == pytest.approx(NOW - 7 * DAY)
    assert d.missing_articles == {"Not_A_Real_Article"}  # never asked for its page views
    assert d.redirected() == {"pokemon_cards": "Pokémon_Trading_Card_Game"}  # read the real article
    assert d.quiet_articles == {"pokemon_cards"}  # 20 views a day: flagged for a look
    assert d.last_wiki_day and d.last_resolve_day and d.stats["wiki_reads"] == 3
    assert [r["brand"] for r in d.heating_up()] == ["Burger King", "Taco Bell"]
    assert "pageviews/per-article/en.wikipedia/all-access/user/Burger_King/daily/" in http.calls[1]
    st = d.status()
    assert st["missing_articles"] == ["Not_A_Real_Article"] and st["quiet_articles"] == ["pokemon_cards"]
    calls = len(http.calls)
    await d.scan_wiki(NOW)
    assert len(http.calls) == calls + 3  # titles resolved once a day; the missing one isn't read


async def test_title_lookup_down_falls_back_to_the_titles_as_written(tmp_path):
    http = FakeHttp({"/Burger_King/": wiki_json(flat(70))})  # api.php unreachable
    d = desk(tmp_path, http=http, brands=[Brand("Burger King", "QSR", "major", wiki=["Burger_King"])])
    await d.scan_wiki(NOW)
    assert d.stats["wiki_reads"] == 1 and d.last_resolve_day == "" and not d.missing_articles


async def test_offline_wiki_scan_is_retried(tmp_path):
    d = desk(tmp_path, http=FakeHttp({"wikimedia": ConnectionError("proxy")}))
    await d.scan_wiki(NOW)
    assert d.last_wiki_day == "" and d.stats["wiki_errors"] > 0 and not d.missing_articles


async def test_app_chart_climb_becomes_evidence(tmp_path):
    d = desk(tmp_path)
    start = NOW - 15 * DAY
    others = [(str(100 + i), f"Game {i}", "Some Studio") for i in range(60)]
    for i in range(55):  # 6-hourly snapshots for ~two weeks
        t = start + i * 6 * 3600
        ranks = others[:]
        if i >= 52:  # Duolingo enters at #10 for the last 3 snapshots
            ranks.insert(9, ("570060128", "Duolingo - Language Lessons", "Duolingo"))
        d.record_apps(parse_app_chart(app_feed(ranks)), t)
    e = d.evidence["DUOL"][0]
    assert (e.kind, e.direction, e.weight) == ("app", "up", "core")
    assert "#10 on the US App Store free chart (two weeks ago outside the top 200)" in e.detail


async def test_app_developer_maps_to_a_listed_company(tmp_path):
    http = FakeHttp(
        {
            "rss.applemarketingtools.com": app_feed(
                [("1", "Tiny Photo", "Tiny Apps Inc."), ("2", "Temu", "Temu")]
            )
        }
    )
    d = desk(tmp_path, http=http, brands=[])
    await d.scan_apps(NOW)
    assert d.mapped_apps() == [
        {"rank": 1, "app": "Tiny Photo", "ticker": "TINY", "brand": "Tiny Photo", "weight": "auto"}
    ]
    assert d.last_apps == NOW and d.stats["app_snapshots"] == 1


async def test_app_feed_failure_is_counted(tmp_path):
    d = desk(tmp_path, http=FakeHttp({"rss.applemarketingtools.com": {"feed": {}}}))
    assert await d.scan_apps(NOW) == [] and d.stats["app_errors"] == 1 and d.last_apps == 0


# --------------------------------------------------------------------------- signals


def qsr_chart(url, params):
    # QSR slid 9% over the month the evidence built up
    closes = [68.0 - 0.2 * i for i in range(31)]
    return yahoo_chart(closes, NOW - 30 * DAY)


async def test_evaluate_signals_cooldown_and_upgrade(tmp_path):
    d = desk(tmp_path, http=FakeHttp({"/QSR": qsr_chart}))
    d.add(ev("alt_data", t=NOW - 3 * DAY))
    sig = await d.evaluate("QSR", NOW)
    assert sig.severity == "MEDIUM" and sig.move_pct == pytest.approx(
        -0.96, abs=0.01
    )  # since the evidence began
    assert sig.price == pytest.approx(62.0)
    assert await d.evaluate("QSR", NOW + 60) is None  # nothing new and stronger this month
    d.add(ev("app", t=NOW - DAY))
    sig2 = await d.evaluate("QSR", NOW + 120)
    assert sig2.severity == "HIGH" and sig2.kinds == ["app", "alt_data"]
    assert sig2.headline.startswith("▲ Trend: Burger King (QSR) · App Store climb + tracker data · stock")
    d.add(ev("demand", t=NOW))
    assert await d.evaluate("QSR", NOW + 180) is None  # already HIGH this month
    assert [r["severity"] for r in d.board(now=NOW + 200)] == ["HIGH"]  # latest per company


async def test_only_listed_common_stock(tmp_path):
    d = desk(tmp_path)
    d.add(ev("alt_data", ticker="WRNT"))
    d.add(ev("alt_data", ticker="PRIV"))
    assert await d.evaluate("WRNT", NOW) is None and await d.evaluate("PRIV", NOW) is None
    assert await d.evaluate("NONE", NOW) is None


async def test_stale_evidence_drops_out(tmp_path):
    d = desk(tmp_path, http=FakeHttp({"/QSR": qsr_chart}))
    d.add(ev("alt_data", t=NOW - 20 * DAY))
    assert await d.evaluate("QSR", NOW) is None


async def test_price_feed_down_still_judges(tmp_path):
    d = desk(tmp_path)  # every price read fails
    d.add(ev("alt_data"))
    sig = await d.evaluate("QSR", NOW)
    assert sig.severity == "MEDIUM" and sig.move_pct is None and sig.price is None


def test_alert_text():
    from news247.trends import TrendSignal

    sig = TrendSignal(
        id="x", ticker="QSR", brands=["Burger King"], severity="HIGH", direction="up", kinds=["app", "alt_data"],
        weight="major", market_cap=27e9, move_pct=-9.0, reasons=["2 independent kinds of evidence agree"],
        evidence=[ev("alt_data").__dict__], created=NOW, price=62.0,
    )  # fmt: skip
    title, body = signal_alert_text(sig)
    assert title == "▲ Trend: Burger King (QSR) · App Store climb + tracker data · stock -9.0% since"
    assert "• tracker data: alt_data detail" in body and "QSR · $27B · major brand" in body
    assert body.endswith(
        "Trend signals are leads to research, not results; the scorecard tracks how they do."
    )


# --------------------------------------------------------------------------- the scorecard


async def test_outcomes_are_marked_against_spy(tmp_path):
    start = NOW - 100 * DAY

    def chart(rate):
        return lambda url, params: yahoo_chart([100 * (1 + rate) ** i for i in range(101)], start)

    http = FakeHttp({"/SPY": chart(0.0005), "/QSR": chart(0.002), "/YUM": chart(-0.001)})
    d = desk(tmp_path, http=http)
    d.add(ev("alt_data", t=start + 1))
    d.add(ev("alt_data", ticker="YUM", brand="Taco Bell", t=start + 1))
    await d.evaluate("QSR", start + 2)
    await d.evaluate("YUM", start + 2)
    assert await d.mark_outcomes(NOW) == 6  # 2 signals x 30/60/90 days
    assert await d.mark_outcomes(NOW) == 0  # once each
    assert sum("/SPY" in c for c in http.calls) == 1  # one SPY read for 2 signals x 3 horizons
    card = d.scorecard()
    assert card["signals"] == 2
    m30 = card["by"]["MEDIUM/30d"]
    assert m30["n"] == 2 and m30["hits"] == 1  # QSR (up) beat SPY; YUM (up) lagged it
    text = format_board(d.board(days=120, now=NOW), card)
    assert "[MEDIUM]" in text and "90d: stock" in text and "MEDIUM/30d" in text


async def test_down_signals_score_when_the_stock_lags(tmp_path):
    start = NOW - 40 * DAY
    http = FakeHttp(
        {
            "/SPY": lambda u, p: yahoo_chart([100.0] * 41, start),
            "/YUM": lambda u, p: yahoo_chart([100 - 0.3 * i for i in range(41)], start),
        }
    )
    d = desk(tmp_path, http=http)
    d.add(ev("alt_data", ticker="YUM", brand="Taco Bell", direction="down", t=start + 1))
    await d.evaluate("YUM", start + 2)
    await d.mark_outcomes(NOW)
    s = d.scorecard()["by"]["MEDIUM/30d"]
    assert s["hit_rate"] == 1.0 and s["avg_excess"] > 5  # the short side's excess counts as positive


# --------------------------------------------------------------------------- state & loop


def test_state_survives_a_restart(tmp_path):
    d = desk(tmp_path)
    d.add(ev("alt_data"))
    d.record_apps([{"rank": 3, "id": "9", "name": "Duolingo", "artist": "Duolingo"}], NOW)
    d.last_wiki_day = "2026-10-10"
    d.save()
    d2 = desk(tmp_path)
    assert d2.evidence["QSR"][0].kind == "alt_data"
    assert d2.app_history["9"] == [(NOW, 3)] and d2.app_names["9"] == ("Duolingo", "Duolingo")
    assert d2.last_wiki_day == "2026-10-10"
    (tmp_path / "trends_state.json").write_text("{not json")
    assert desk(tmp_path).evidence == {}  # a corrupt file starts fresh


async def test_run_loop_scans_and_reports(tmp_path, monkeypatch):
    import asyncio

    spike = flat(63) + [5200, 6100, 6800, 7400, 7100, 6900, 7300]
    http = FakeHttp({"/Burger_King/": wiki_json(spike), "/QSR": qsr_chart, "rss.apple": app_feed([])})
    d = desk(tmp_path, http=http, brands=[Brand("Burger King", "QSR", "major", wiki=["Burger_King"])])
    got: list[Any] = []
    stop = asyncio.Event()

    async def on_signals(sigs):
        got.extend(sigs)
        stop.set()

    monkeypatch.setattr("news247.trends.datetime", _Noon)
    await asyncio.wait_for(d.run(on_signals, stop), 5)
    assert [s.ticker for s in got] == ["QSR"] and got[0].severity == "MEDIUM"
    assert json.loads((tmp_path / "trends_state.json").read_text())["last_wiki_day"]


class _Noon:
    """datetime stand-in: always 12:00 ET, so the daily Wikipedia scan is due."""

    @staticmethod
    def fromtimestamp(ts, tz=None):
        from datetime import datetime

        return datetime.fromtimestamp(ts, tz).replace(hour=12)


# --------------------------------------------------------------------------- engine, API, CLI


async def test_engine_publishes_confirmed_trends(cfg, scorer):
    from news247.engine import Engine
    from news247.notify import Dispatcher

    from .conftest import CaptureNotifier

    cfg.market.enabled = False
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    cap = CaptureNotifier(Severity.HIGH)
    disp.channels = [cap]
    eng = Engine(cfg, storage=Storage(":memory:"), dispatcher=disp, sources=[])
    assert eng.trends is not None and eng.status()["trends"]["brands"] >= 45
    eng.trends.universe = UNIVERSE
    eng.trends.http = FakeHttp({"/QSR": qsr_chart})
    eng.trends.add(ev("app", t=time.time() - DAY))  # the App Store climb, seen yesterday
    await eng.on_item(
        NewsItem(
            source="t", title="Burger King visits rose 7.1% year over year in September, Placer.ai data shows"
        )
    )
    await eng.drain(5)
    trend = [a for a in eng.storage.recent_alerts(10) if a["kind"] == "trend"]
    assert len(trend) == 1 and trend[0]["severity"] == "HIGH" and trend[0]["tickers"] == ["QSR"]
    assert trend[0]["edge"]["trend"]["kinds"] == ["app", "alt_data"]
    assert [a.kind for a in cap.alerts] == ["trend"]  # pushed (the headline itself stays LOW)
    brief = eng.brief()
    assert brief["trends"][0]["ticker"] == "QSR"


async def test_engine_keeps_low_trends_off_the_feed(cfg, scorer):
    from news247.engine import Engine

    cfg.market.enabled = False
    eng = Engine(cfg, storage=Storage(":memory:"), sources=[])
    eng.trends.universe = UNIVERSE
    eng.trends.http = FakeHttp()
    await eng.on_item(
        NewsItem(source="t", title="McDonald's visits rose 3% in September, Placer.ai data shows")
    )
    await eng.drain(5)
    assert not [a for a in eng.storage.recent_alerts(10) if a["kind"] == "trend"]
    (row,) = eng.trends_board()["rows"]
    assert row["ticker"] == "MCD" and row["severity"] == "LOW" and "too big" in row["reasons"][0]


async def test_trends_can_be_switched_off(tmp_path):
    from news247.engine import Engine

    cfg = build_config(
        {"general": {"data_dir": str(tmp_path)}, "trends": {"enabled": False}, "market": {"enabled": False}}
    )
    eng = Engine(cfg, storage=Storage(":memory:"), sources=[])
    assert eng.trends is None and eng.trends_board()["enabled"] is False
    assert eng.status()["trends"] == {"enabled": False} and eng.brief()["trends"] == []


async def test_api_trends(cfg):
    from aiohttp.test_utils import TestClient, TestServer

    from news247.engine import Engine
    from news247.web.server import WebServer

    cfg.market.enabled = False
    cfg.web.token = "s3cret"
    eng = Engine(cfg, storage=Storage(":memory:"), sources=[])
    eng.trends.universe = UNIVERSE
    eng.trends.http = FakeHttp({"/QSR": qsr_chart})
    eng.trends.add(ev("alt_data"))
    await eng.trends.evaluate("QSR")
    async with TestClient(TestServer(WebServer(eng, cfg.web).app)) as client:
        assert (await client.get("/api/trends")).status == 401
        data = await (await client.get("/api/trends?token=s3cret&days=7")).json()
        assert data["enabled"] and data["rows"][0]["ticker"] == "QSR"
        assert data["scorecard"]["signals"] == 1 and data["status"]["brands"] >= 45
        assert (await client.get("/api/trends?token=s3cret&days=x")).status == 200


async def test_cli_trends(tmp_path, capsys):
    from news247.cli import main

    db = tmp_path / "t.db"
    d = TrendDesk(TrendsConfig(), FakeHttp({"/QSR": qsr_chart}), UNIVERSE, Storage(db))
    d.add(ev("alt_data"))
    await d.evaluate("QSR")
    assert main(["trends", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "[MEDIUM]" in out and "Burger King (QSR)" in out and "Scorecard: 1 signals recorded" in out
    assert main(["trends", "--db", str(db), "YUM"]) == 0
    assert "No trend signals" in capsys.readouterr().out
    assert main(["trends", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["ticker"] == "QSR"
    assert main(["trends", "--db", str(tmp_path / "none.db")]) == 1


def test_new_app_needs_two_weeks_of_watching(tmp_path):
    """Right after a deploy (or days offline) the desk can't know where an app was two weeks
    ago, so nothing counts as a climb until the chart has been watched that long."""
    d = desk(tmp_path)
    start = NOW - 4 * DAY
    for i in range(10):
        chart = [{"rank": 5, "id": "9", "name": "Duolingo", "artist": "Duolingo"}] if i >= 7 else []
        d.record_apps(chart, start + i * 6 * 3600)
    assert "DUOL" not in d.evidence
    d2 = desk(tmp_path / "b")
    d2.record_apps([], NOW - 20 * DAY)
    d2.record_apps([], NOW - 14 * DAY)  # 6 days offline: watching starts over here
    assert d2.chart_since == NOW - 14 * DAY
    steps = int(14 * DAY / (6 * 3600))
    for i in range(1, steps + 1):  # every 6 hours since; Duolingo charts for the last three
        app = [{"rank": 5, "id": "9", "name": "Duolingo", "artist": "Duolingo"}] if i > steps - 3 else []
        d2.record_apps(app, NOW - 14 * DAY + i * 6 * 3600)
    assert d2.evidence["DUOL"][0].kind == "app"
    d2.save()
    assert desk(tmp_path / "b").chart_since == NOW - 14 * DAY


@pytest.mark.parametrize(
    "title,want",
    [
        # Restaurant Business, Oct 2026: the tracker's numbers, without naming the tracker
        ("Taco Bell's traffic still down double digits despite aggressive promotions", ("YUM", "down")),
        ("Chipotle traffic slows as diners pull back", ("CMG", "down")),
        ("Traffic at Chipotle fell 4% in September", ("CMG", "down")),
        ("Duolingo daily active users up 40%", ("DUOL", "up")),
        ("Taco Bell visits fell", ("YUM", "down")),
        # not leads: the company's own results, the roads, no metric, no direction
        ("McDonald's third-quarter earnings: traffic rose 2%", None),
        ("Lyft says traffic in San Francisco is up 5%", None),
        ("Chipotle names new CEO", None),
        ("Taco Bell visits", None),
    ],
)
def test_brand_metric_headlines_without_a_named_tracker(tmp_path, scorer, title, want):
    uni = Universe([Listing(t, t, market_cap=2e10) for t in ("QSR", "YUM", "MCD", "CMG", "DUOL", "LYFT")])
    d = desk(tmp_path, universe=uni)
    touched = d.from_news(NewsItem(source="t", title=title), scorer.score(NewsItem(source="t", title=title)))
    if want is None:
        assert touched == [] and d.evidence == {}
    else:
        assert touched == [want[0]]
        (e,) = d.evidence[want[0]]
        assert (e.kind, e.direction) == ("alt_data", want[1])


async def test_cli_check_against_live_shaped_feeds(cfg, server, monkeypatch, capsys):
    """`news247 trends --check` end to end through the real HTTP client: title lookup, page views
    (a 404 marks the title missing), and the App Store chart mapped to listed companies."""
    import news247.trends as trends
    from news247.cli import _trends_check

    spike = flat(63) + [5200, 6100, 6800, 7400, 7100, 6900, 7300]
    server.on("/w/api.php", TITLES_ANSWER)
    server.on("/pv/Burger_King", wiki_json(spike))
    server.on(
        "/apps.json", app_feed([("1", "Duolingo - Language Lessons", "Duolingo"), ("2", "Temu", "Temu")])
    )
    monkeypatch.setattr(trends, "WIKI_TITLES_URL", server.url("/w/api.php"))
    monkeypatch.setattr(trends, "WIKI_URL", server.url("/pv/") + "{article}?start={start}&end={end}")
    monkeypatch.setattr(trends, "APPS_URL", server.url("/apps.json"))
    assert await _trends_check(cfg, UNIVERSE, False) == 0
    out = capsys.readouterr().out
    assert "Wikipedia: 1/49 articles read, 48 failed" in out
    assert "✗ no such article: Taco_Bell" in out  # 404 from the page-view API
    assert "spike: Burger King (QSR)" in out
    assert "#1    Duolingo - Language Lessons" in out and "DUOL" in out
    titles = next(r for r in server.requests if r["path"] == "/w/api.php")["query"]
    assert titles["redirects"] == "1" and "Burger King" in titles["titles"].split("|")


def test_tracker_numbers_inside_an_earnings_story_are_not_a_lead(tmp_path, scorer):
    d = desk(tmp_path)
    title = "Burger King quarterly results: visits rose 7%, Placer.ai data shows"
    item = NewsItem(source="t", title=title)
    an = scorer.score(item)
    assert "consumer_data_signal" in an.themes
    assert d.from_news(item, an) == [] and d.evidence == {}


@pytest.mark.parametrize(
    "bad",
    [{"wiki_sustained_days": 8}, {"app_top": 150, "app_from": 150}, {"app_from": 300}, {"window_days": 0}],
)
def test_bad_trend_settings_fail_at_boot(bad):
    from news247.config import ConfigError

    with pytest.raises(ConfigError, match=r"\[trends\]"):
        build_config({"trends": bad})
