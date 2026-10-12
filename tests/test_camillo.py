"""The Camillo desk (camillo.py): TickerTags-style chatter, the catalyst, the checklist, the page."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from news247.camillo import (
    ChatterTags,
    EarningsBook,
    chatter_spike,
    checklist,
    load_playbook,
    page,
    parse_nasdaq_earnings,
)
from news247.config import TrendsConfig
from news247.http import HTTPError
from news247.market.universe import Listing, Universe
from news247.storage import Storage
from news247.trends import Brand, Evidence, TrendDesk, load_brands

ET = ZoneInfo("America/New_York")
NOW = time.time()
CFG = TrendsConfig()


class Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, headers=None, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        key = (params or {}).get("date") or url
        for k, v in self.routes.items():
            if k in key:
                if isinstance(v, Exception):
                    raise v
                return Resp(v(url, params) if callable(v) else v)
        if "date" in (params or {}):
            return Resp({"data": {"rows": []}})  # a day with no reports
        raise HTTPError(404, url)


# --------------------------------------------------------------------------- the playbook


def test_playbook_is_complete_and_sourced():
    p = load_playbook()
    assert p["intro"] and len(p["principles"]) >= 4 and len(p["process"]) >= 5
    for x in p["principles"]:
        assert x["title"] and x["text"] and x.get("sources"), x["title"]
    for c in p.get("cases", []):
        assert c["name"] and c["noticed"] and c.get("sources"), c["name"]
        assert all(s.get("label") for s in c["sources"])
    assert p["limits"], "the page says what the desk can't do"


# --------------------------------------------------------------------------- the catalyst


def test_parse_nasdaq_earnings():
    data = {"data": {"rows": [
        {"symbol": "QSR", "time": "time-pre-market", "name": "Restaurant Brands"},
        {"symbol": "yum", "time": "time-not-supplied"},
        {"symbol": "", "time": "time-after-hours"},
        "junk",
    ]}}  # fmt: skip
    assert parse_nasdaq_earnings(data) == [("QSR", "before the open"), ("YUM", "")]
    assert parse_nasdaq_earnings({"data": None}) == [] and parse_nasdaq_earnings({}) == []


async def test_earnings_book_reads_weekdays_and_keeps_the_first_date():
    monday = date(2026, 10, 12)
    rows = {
        "2026-10-13": {"data": {"rows": [{"symbol": "DPZ", "time": "time-pre-market"}]}},
        "2026-10-29": {"data": {"rows": [{"symbol": "QSR", "time": "time-pre-market"}]}},
        "2026-11-03": {"data": {"rows": [{"symbol": "YUM", "time": "time-pre-market"}]}},
        "2026-11-04": {
            "data": {"rows": [{"symbol": "QSR", "time": "time-after-hours"}]}
        },  # never: QSR is earlier
        "2026-10-14": ConnectionError("down"),
    }
    book = EarningsBook(FakeHttp(rows), days=30)
    read = await book.refresh(monday, pause=0)
    asked = [p["date"] for _, p in book.http.calls]
    assert all(date.fromisoformat(d).weekday() < 5 for d in asked) and len(asked) == read + 1
    assert book.lookup("QSR", monday) == {"date": "2026-10-29", "when": "before the open", "in_days": 17}
    assert book.lookup("yum", monday)["date"] == "2026-11-03" and book.lookup("XYZ", monday) is None
    assert book.lookup("DPZ", date(2026, 10, 20)) is None  # already reported
    assert book.errors == 1 and book.last_day == "2026-10-12"
    # a later read that loses a day keeps what it knew
    book.http = FakeHttp({"2026-10-13": {"data": {"rows": []}}})
    await book.refresh(date(2026, 10, 13), pause=0)
    assert book.lookup("QSR", date(2026, 10, 13))["date"] == "2026-10-29"


# --------------------------------------------------------------------------- chatter


def days(n: int, start: str = "2026-09-01") -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def test_chatter_spike_needs_three_days_and_a_real_share():
    d = days(20)
    quiet = [(x, 4, 20_000) for x in d[:17]]
    assert (
        chatter_spike(quiet + [(d[17], 20, 20_000), (d[18], 22, 20_000), (d[19], 18, 20_000)])["ratio"] == 5.0
    )
    one_day = quiet + [(d[17], 60, 20_000), (d[18], 4, 20_000), (d[19], 4, 20_000)]
    assert chatter_spike(one_day) is None  # a single viral post isn't a trend
    busier = quiet + [(d[17], 12, 60_000), (d[18], 12, 60_000), (d[19], 12, 60_000)]
    assert chatter_spike(busier) is None  # more mentions only because more posts were sampled
    tiny = [(x, 0, 20_000) for x in d[:17]] + [(d[17], 3, 20_000), (d[18], 3, 20_000), (d[19], 3, 20_000)]
    assert chatter_spike(tiny) is None  # 9 mentions: below the floor
    assert chatter_spike(quiet[:6]) is None  # not enough history
    thin = [(x, 4, 900) for x in d[:17]] + [(d[17], 20, 20_000), (d[18], 22, 20_000), (d[19], 18, 20_000)]
    assert chatter_spike(thin) is None  # the baseline days were barely sampled


def test_chatter_counts_tags_once_per_post_and_skips_other_languages():
    c = ChatterTags(load_brands())
    assert c.count("Burger King's new Whopper > McDonald's. burger king forever", "2026-10-12") == [
        "Burger King",
        "McDonald's",
    ]
    assert c.counts["2026-10-12"]["Burger King"] == 1 and c.posts["2026-10-12"] == 1
    assert c.count("#tacobell is not a mention of a tag word boundary", "2026-10-12") == []
    evt = {
        "kind": "commit",
        "commit": {"operation": "create", "collection": "app.bsky.feed.post", "record": {"text": "x"}},
    }
    assert ChatterTags.post_text(evt) == "x"
    evt["commit"]["record"]["langs"] = ["pt"]
    assert ChatterTags.post_text(evt) is None
    assert ChatterTags.post_text({"kind": "identity"}) is None
    c.load(c.dump())
    assert c.posts["2026-10-12"] == 2


class FakeWS:
    def __init__(self, msgs):
        self.msgs = list(msgs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def receive(self):
        import aiohttp

        if not self.msgs:
            return type("M", (), {"type": aiohttp.WSMsgType.CLOSED, "data": None})()
        return type("M", (), {"type": aiohttp.WSMsgType.TEXT, "data": self.msgs.pop(0)})()


class FakeSession:
    def __init__(self, msgs, fail_first=False):
        self.msgs, self.fail_first, self.urls = msgs, fail_first, []

    def ws_connect(self, url, **kw):
        self.urls.append(url)
        if self.fail_first and len(self.urls) == 1:
            raise ConnectionError("host down")
        return FakeWS(self.msgs)


async def test_sample_reads_the_firehose_and_falls_back_to_the_next_host():
    import json

    def post(text, lang="en"):
        return json.dumps({"kind": "commit", "commit": {"operation": "create", "collection": "app.bsky.feed.post",
                                                        "record": {"text": text, "langs": [lang]}}})  # fmt: skip

    msgs = [
        post("Taco Bell line around the block"),
        post("hola Taco Bell", "es"),
        "not json",
        post("nothing here"),
    ]
    c = ChatterTags(load_brands(), sample_s=5)
    session = FakeSession(msgs, fail_first=True)
    n = await c.sample(session, NOW)
    today = datetime.fromtimestamp(NOW, ET).date().isoformat()
    assert n == 2 and c.posts[today] == 2 and c.counts[today]["Taco Bell"] == 1
    assert len(session.urls) == 2 and "wantedCollections=app.bsky.feed.post" in session.urls[1]
    assert c.errors == 1 and c.last_sample == NOW


def test_chatter_becomes_evidence_on_the_desk(tmp_path):
    uni = Universe([Listing("QSR", "Restaurant Brands International", market_cap=27e9)])
    desk = TrendDesk(
        CFG,
        None,
        uni,
        Storage(tmp_path / "d.db"),
        brands=[Brand("Burger King", "QSR", "major", terms=["burger king"])],
    )
    ch = ChatterTags(desk.brands)
    desk.attach_camillo(None, ch)
    d = days(20)
    for i, x in enumerate(d):
        ch.posts[x] = 20_000
        ch.counts[x] = {"Burger King": 4 if i < 17 else 20}
    touched = desk.chatter_evidence(NOW, d[-1], chatter_spike)
    assert touched == ["QSR"]
    (e,) = desk.evidence["QSR"]
    assert e.kind == "chatter" and e.direction is None and "5.0x its usual share" in e.detail
    assert desk.chatter_evidence(NOW, d[-1], chatter_spike) == []  # once per window


# --------------------------------------------------------------------------- checklist & page


def sig(**kw):
    base = {"ticker": "QSR", "brands": ["Burger King"], "kinds": ["app", "alt_data"], "direction": "up",
            "market_cap": 27e9, "weight": "major", "move_pct": -9.0, "severity": "HIGH", "created": NOW,
            "evidence": [], "reasons": []}  # fmt: skip
    return {**base, **kw}


def test_checklist_all_pass():
    rows = checklist(sig(), CFG, {"date": "2026-10-29", "when": "before the open", "in_days": 17})
    assert [r["id"] for r in rows] == [
        "change", "confirmed", "direction", "material", "imbalance", "unpriced", "catalyst"
    ]  # fmt: skip
    assert all(r["pass"] for r in rows)
    assert rows[-1]["detail"] == "next report 2026-10-29 before the open (in 17 days)"


@pytest.mark.parametrize(
    "kw,failing",
    [
        ({"kinds": ["alt_data"]}, "confirmed"),
        ({"direction": None, "kinds": ["attention"]}, "direction"),
        ({"market_cap": 215e9}, "material"),
        ({"move_pct": 18.0}, "unpriced"),
        ({"move_pct": None}, "unpriced"),
    ],
)
def test_checklist_failures(kw, failing):
    rows = {r["id"]: r for r in checklist(sig(**kw), CFG, {"date": "2026-10-29", "when": "", "in_days": 17})}
    assert not rows[failing]["pass"]


def test_no_report_date_fails_the_catalyst():
    rows = {r["id"]: r for r in checklist(sig(), CFG, None)}
    assert not rows["catalyst"]["pass"] and rows["catalyst"]["detail"] == "next report date unknown"


async def test_page_puts_each_idea_against_the_method(tmp_path):
    uni = Universe([Listing("QSR", "Restaurant Brands International", market_cap=27e9, price=62.0)])
    desk = TrendDesk(
        CFG, None, uni, Storage(tmp_path / "d.db"), brands=[Brand("Burger King", "QSR", "major")]
    )
    book = EarningsBook(None)
    desk.attach_camillo(
        book, ChatterTags(desk.brands)
    )  # loads saved state (none here) before the dates are set
    book.next = {
        "QSR": {"date": (datetime.now(ET).date() + timedelta(days=17)).isoformat(), "when": "before the open"}
    }

    async def no_price(*a, **k):
        return -9.0, 62.0

    desk.stock_move = no_price  # type: ignore[method-assign]
    desk.add(Evidence("alt_data", "QSR", "Burger King", "major", "up", NOW, NOW, "visits +7.1%"))
    desk.add(Evidence("app", "QSR", "Burger King", "major", "up", NOW, NOW, "#18 on the App Store"))
    await desk.evaluate("QSR", NOW)
    p = page(desk)
    (idea,) = p["ideas"]
    assert idea["passed"] == 7 and idea["earnings"]["in_days"] == 17
    assert idea["consensus"]["level"] == "strong imbalance"  # no headline tells the story yet
    assert "Out at information parity, win or lose" in idea["exit"] and idea["exit"].startswith("A long idea")
    assert p["playbook"]["principles"] and p["status"]["earnings"]["companies"] == 1
    assert p["status"]["chatter"]["enabled"] is True


async def test_api_and_tab(cfg):
    from aiohttp.test_utils import TestClient, TestServer

    from news247.engine import Engine
    from news247.web.server import WebServer

    cfg.market.enabled = False
    cfg.web.token = "s3cret"
    eng = Engine(cfg, storage=Storage(":memory:"), sources=[])
    assert eng.trends.earnings is not None and eng.trends.chatter is not None
    async with TestClient(TestServer(WebServer(eng, cfg.web).app)) as client:
        assert (await client.get("/api/camillo")).status == 401
        data = await (await client.get("/api/camillo?token=s3cret")).json()
        assert data["enabled"] and data["playbook"]["principles"] and data["ideas"] == []
        html = await (await client.get("/app/")).text()
        assert 'data-view="camillo"' in html and 'data-tab="camillo"' in html
        js = await (await client.get("/app/app.js")).text()
        assert "loadCamillo" in js and '"camillo"' in js


def test_camillo_can_be_turned_off(tmp_path):
    from news247.config import build_config
    from news247.engine import Engine

    cfg = build_config({"general": {"data_dir": str(tmp_path)}, "market": {"enabled": False},
                        "trends": {"chatter": False, "earnings": False}})  # fmt: skip
    eng = Engine(cfg, storage=Storage(":memory:"), sources=[])
    assert eng.trends.chatter is None and eng.trends.earnings is None
    assert eng.camillo_page()["status"]["chatter"]["enabled"] is False


# --------------------------------------------------------------------------- information parity


def test_consensus_score_follows_the_book():
    from news247.camillo import consensus_score

    hype = [(f"Burger King sales surge {i}", f"src{i}") for i in range(5)]
    gloom = [("Burger King franchisee files for bankruptcy", "x")]
    s = consensus_score(hype + gloom)
    assert (s["hype"], s["gloom"], s["level"]) == (5, 1, "parity")  # 5 / 1 > 2: the story is told
    s = consensus_score(hype[:2] + gloom + [("Burger King traffic slows", "y")])
    assert s["score"] == 1.0 and s["level"] == "imbalance"
    s = consensus_score([("Burger King sales jump", "a"), ("Burger King sales jump again", "a")] + gloom + [
        ("Burger King traffic slows", "y"), ("Burger King weak quarter", "z")])  # fmt: skip
    assert s["hype"] == 1 and s["score"] == 0.33 and s["level"] == "strong imbalance"  # one quote per source
    # a short idea counts the other way: gloom is the trend's side
    s = consensus_score(
        [("Taco Bell traffic falls", "a"), ("Taco Bell sales slump", "b"), ("Taco Bell slows", "c")], "down"
    )
    assert s["level"] == "parity"
    assert consensus_score([])["level"] == "strong imbalance"
    assert (
        consensus_score([("Burger King names a new CEO", "a")])["level"] == "strong imbalance"
    )  # neither side


def test_coverage_reads_stored_headlines(tmp_path):
    from news247.analysis.scorer import Scorer
    from news247.camillo import coverage
    from news247.config import build_config
    from news247.models import NewsItem

    cfg = build_config({})
    sc = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    st = Storage(tmp_path / "c.db")
    for title, src, age in [
        ("Burger King traffic surges on Dragon Ball menu", "a", 1),
        ("Burger King's Whopper wins taste test", "b", 2),
        ("Burger Kingdom game review", "c", 1),  # not the brand
        ("Burger King sales jump", "d", 40),  # older than 30 days
    ]:
        item = NewsItem(source=src, title=title, detected=NOW - age * 86400)
        st.add_item(item, sc.score(item))
    got = coverage(st, ["Burger King", "whopper"], NOW - 30 * 86400)
    assert sorted(s for _, s in got) == ["a", "b"]
    assert coverage(st, [], NOW) == [] and coverage(None, ["x"], NOW) == []


def test_parity_fails_the_imbalance_test():
    rows = {
        r["id"]: r
        for r in checklist(
            sig(), CFG, None, {"score": 3.0, "hype": 6, "gloom": 2, "headlines": 9, "level": "parity"}
        )
    }
    assert not rows["imbalance"]["pass"] and "parity above 2" in rows["imbalance"]["detail"]
