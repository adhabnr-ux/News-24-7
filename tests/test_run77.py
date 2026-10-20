"""Run 77: the playbook in every alert push (its way, or why it passes, right under the call) and the
playbook's morning in the Brief (what it enters at the open, waits on and passes, today's blind spots,
drifting kinds, yesterday)."""

from __future__ import annotations

import time

from news247.edge import brief_push
from news247.notify.channels import WebPushNotifier
from news247.pbmorning import morning, playbook_line

from .test_desk import news
from .test_personal import semis_engine
from .test_run60 import et
from .test_run64 import pair

# --------------------------------------------------------------------------- in the push


def styled(pin, pout, avg=1.4, n=20, **extra):
    p = pair(pin, pout)
    p["combo_rec"]["styles"] = {f"{pin}|{pout}": {"avg": avg, "n": n}}
    p["combo_rec"].update(extra)
    return p


def test_the_playbook_line():
    assert (
        playbook_line(styled("leg2", "d1"))
        == "🧭 The playbook: on the second leg, out the next day (+1.4% on average, 20)"
    )
    assert (
        playbook_line({**styled("alert", "close"), "liq": {"pass": True, "why": "a thin stock: …"}})
        == "🚫 The playbook passes: a thin stock: …"
    )
    smooth = styled(
        "alert",
        "d1",
        smooth={"pair": "alert|h1+d1", "out": "h1+d1", "avg": 1.1, "n": 18, "sd": 1.0, "best_sd": 2.0},
    )
    assert (
        playbook_line(smooth)
        == "🧭 The playbook: at the alert, out half an hour in, half the next day (+1.1% on average, 18)"
    )
    assert playbook_line({}) == ""


def test_the_push_carries_it_under_the_call():
    a = news("Acme wins $2B Pentagon contract")
    a.edge = {**styled("leg2", "d1"), "call": {"line": "✅ Act now: contract wins work here"}}
    body = WebPushNotifier.payload(None, a)["body"].split("\n")
    i = body.index("✅ Act now: contract wins work here")
    assert body[i + 1] == "🧭 The playbook: on the second leg, out the next day (+1.4% on average, 20)"


# --------------------------------------------------------------------------- the playbook's morning


def test_the_morning_notes(tmp_path):
    eng = semis_engine(tmp_path)
    now = et(14, 8, 15)  # Wednesday 08:15 ET
    for i, (sym, pin) in enumerate((("ACME", "open"), ("BETA", "open"), ("DLTA", "leg2"))):
        a = news(f"{sym} wins contract", tickers=(sym,))
        a.created = now - 3600 * (i + 1)
        a.edge = {"refs": {sym: 100.0}, **styled(pin, "close")}
        eng.storage.add_alert(a)
    gone = news("Gama wins contract", tickers=("GAMA",))
    gone.created = now - 1800
    gone.edge = {"refs": {"GAMA": 100.0}, **styled("alert", "close", avg=-0.5, **{"pass": True})}
    eng.storage.add_alert(gone)
    eng.blind_rates = {"spots": [{"dim": "day", "value": "Wednesday", "label": "Day of the week: Wednesday", "n": 30,
                                  "avg": -0.4, "older": -0.3, "newer": -0.5}]}  # fmt: skip
    eng.promise_rates = {"fda": {"label": "FDA approval", "verdict": "drifting"}}
    m = morning(eng, now)
    assert m["open"] == ["ACME", "BETA"] and m["waiting"] == ["DLTA"] and m["passes"] == 1
    assert m["blind"] == ["Day of the week: Wednesday"] and m["drifting"] == ["FDA approval"]
    assert m["lines"][:5] == [
        "At the open it enters ACME, BETA",
        "Waiting on its way in: DLTA",
        "Passing on 1 overnight alert",
        "Blind spots today: day of the week: Wednesday (it sits those out)",
        "Drifting from its promise: FDA approval",
    ]


def test_the_brief_push_carries_the_first_note():
    brief = {"overnight": [{"direction": "up", "title": "Acme wins contract"}], "calendar": [], "count": 1,
             "playbook": {"lines": ["At the open it enters ACME"]}}  # fmt: skip
    p = brief_push(brief)
    assert "🧭 At the open it enters ACME" in p["body"]
    quiet = {
        "overnight": [],
        "calendar": [],
        "count": 0,
        "playbook": {"lines": ["At the open it enters ACME"]},
    }
    assert brief_push(quiet) is None  # the playbook's note rides along; it never wakes you up alone


async def test_the_brief_has_it(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract")
    a.created = time.time() - 600
    a.edge = {"refs": {"ACME": 100.0}, **styled("open", "close")}
    eng.storage.add_alert(a)
    b = eng.brief()
    assert "playbook" in b and isinstance(b["playbook"]["lines"], list)
