"""Run 68: denials and walk-backs (a follow-up headline that undoes an alert's news: linked, graded,
flagged, pushed, and the playbook autopilot's calls on it closed) and confidence ranges on the
playbook (an 80% bootstrap range on each pair's average, and an unproven verdict)."""

from __future__ import annotations

import time

from news247.combos import combo_rates, ranged
from news247.denials import denial_rates, is_denial, line, undercuts

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

# --------------------------------------------------------------------------- denials


def test_the_words():
    for t in ("Acme denies report of takeover talks", "Beta says it is not in talks with Gamma",
              "Delta clarifies guidance comments", "Acme calls off merger", "Acme terminates the merger agreement",
              "Company says takeover rumors are baseless", "Acme retracts statement", "Acme has no plans to sell"):  # fmt: skip
        assert is_denial(t), t
    for t in (
        "Acme wins $2B Pentagon contract",
        "Acme raises guidance",
        "Den Haag office opens",
        "Acme plans offering",
    ):
        assert not is_denial(t), t


def story(eng, i, ago_days, prev_close, deny_close):
    a = news(f"Acme in takeover talks, sources say {i}")
    a.created = time.time() - 86400 * ago_days
    a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": prev_close}}}
    eng.storage.add_alert(a)
    d = news(f"Acme denies takeover report {i}", direction="down")
    d.created = a.created + 7200
    d.edge = {"refs": {"ACME": 108.0}, "outcome": {"ACME": {"sign": -1, "close": deny_close}}}
    eng.storage.add_alert(d)
    return a, d


def test_a_denial_finds_the_alert_it_undercuts(tmp_path):
    eng = semis_engine(tmp_path)
    a, d = story(eng, 0, 2, 8.0, -4.0)
    assert undercuts(eng, d.to_dict())["id"] == a.id
    assert undercuts(eng, a.to_dict()) is None  # not a denial
    late = news("Acme denies takeover report again")
    late.created = a.created + 4 * 86400
    assert undercuts(eng, late.to_dict()) is None  # the rumor and its first denial are 3+ days back


def test_what_stocks_did_after_denials(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(6):
        story(eng, i, 10 + i * 5, 8.0, -4.0)  # up 8% on the rumor, then 4% more given back after the denial
    r = denial_rates(eng)
    assert r["n"] == 6 and r["further"]["avg"] == 4.0 and r["further"]["hit"] == 1.0
    assert r["kept"] == round((108 * 0.96 / 100 - 1) * 100 / 8.0, 2)  # 3.68% of the 8% run left
    assert line({"hours": 2.0, "prev_title": "Acme in takeover talks", "rec": r}).startswith(
        "A denial 2 hours after its alert (“Acme in takeover talks”). After denials here, stocks moved +4.0% further against the original call"
    )


async def test_a_denial_is_flagged_pushed_and_acted_on(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
        rid = eng.desk.get(DEV)["autopilots"][0]["id"]
        now = time.time()
        a = news("Acme in takeover talks, sources say")
        a.created = now - 3600
        a.edge = {"refs": {"ACME": 100.0}}
        eng.storage.add_alert(a)
        eng.desk.apply(
            DEV, "track_add", {"symbol": "ACME", "direction": "long", "alert_id": a.id, "entry": 100.0}
        )
        eng.desk.get(DEV)["tracked"][-1]["auto"] = rid
        eng.live_quotes.update("ACME", 104.0, 99.0, now - 5, "tape")
        d = news("Acme denies takeover report", direction="down")
        await eng.publish(d, push=False)
        for _ in range(20):
            await __import__("asyncio").sleep(0.05)
        e = eng.storage.get_alert(d.id)["edge"]
        assert e["denies"]["prev_id"] == a.id and e["denies"]["line"].startswith(
            "A denial 1 hour after its alert"
        )
        assert eng.storage.get_alert(a.id)["edge"]["denied"]["by"] == d.id
        assert eng.desk.get(DEV)["closed"][0]["lesson"] == "autopilot: the news was denied"
        msg = ph.read(next(r for r in server.requests if r["path"] == "/push/a")["body"])
        assert msg["title"] == "⚠ ACME: a denial after its alert" and msg["open"] == f"/app/#a={d.id}"
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- confidence ranges


def test_the_range_of_an_average():
    xs = [3.0] * 9 + [-1.0] * 3
    lo, hi = ranged(xs, "contract|alert|d1")
    assert lo < 2.0 < hi and (lo, hi) == ranged(xs, "contract|alert|d1")  # seeded: the same every time
    assert ranged([1.0] * 10, "x") == (1.0, 1.0)


def test_an_unproven_pair(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(12):  # the next day edges the close, but noisily
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (20 - i)
        a.edge = {"outcome": {"ACME": {"sign": 1, "close": 1.0, "d1": 6.0 if i % 2 else -3.6}}}
        eng.storage.add_alert(a)
    r = combo_rates(eng)["contract"]
    b = r["styles"][r["best"]]
    assert (
        r["best"] == "alert|d1"
        and b["lo"] <= r["styles"]["alert|close"]["avg"] <= b["hi"]
        and r["proven"] is False
    )
    from news247.combos import combo_line

    assert "inside that range: unproven" in combo_line(r)
