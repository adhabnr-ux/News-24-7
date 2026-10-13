"""Replay a real trading period through today's rules: every headline scored as it was published
(plus how long News247 takes to read that source), the alerts it would have stored, the radar's
price alerts at the minute it could see each move, and the scorecard's verdict per move. Used by
the week (Oct 5-9 2026) and the Monday (Oct 12 2026) audits."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from news247.analysis.scorer import Analysis, Scorer
from news247.config import build_config
from news247.market.universe import Universe
from news247.models import Alert, NewsItem, PriceMove, Severity, SourceTier
from news247.scorecard import Scorecard
from news247.storage import Storage

ET = ZoneInfo("America/New_York")


def replay(doc: dict[str, Any]) -> tuple[dict[str, Analysis], dict[str, dict[str, Any]]]:
    """``doc``: {universe, headlines, moves} as in tests/fixtures/week_2026-10-05.json.
    Returns (title -> analysis, symbol -> scorecard verdict)."""
    cfg = build_config({})
    uni = Universe.stub(doc["universe"])
    scorer = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    scorer.attach_universe(uni, **cfg.smallcap.filters())
    st = Storage(":memory:")
    scored: dict[str, Analysis] = {}
    for h in doc["headlines"]:
        item = NewsItem(
            source=h["source"],
            title=h["title"],
            url=f"https://example.com/{h['published']}",
            published=h["published"],
            detected=h["published"] + h["lag"],
            tier=SourceTier[h["tier"].upper()],
        )
        an = scorer.score(item)
        scored[h["title"]] = an
        st.add_item(item, an)
        if an.severity >= Severity.MEDIUM:  # the engine publishes MEDIUM and up
            st.add_alert(
                Alert(
                    kind="news",
                    severity=an.severity,
                    title=item.title,
                    body="",
                    tickers=an.tickers,
                    created=item.detected,
                    item=item,
                    analysis=an,
                )
            )
    for m in doc["moves"]:
        st.add_alert(
            Alert(
                kind="price",
                severity=Severity[m["severity"]],
                title=f"{m['symbol']} {m['pct']:+.1f}%",
                body=m["note"],
                tickers=[m["symbol"]],
                created=m["at"],
                move=PriceMove(m["symbol"], m["pct"], 86400, 1.0, 1.0, detected=m["at"]),
            )
        )
    card = Scorecard(st, uni)
    verdicts: dict[str, dict[str, Any]] = {}
    for day in sorted({datetime.fromtimestamp(m["at"], ET).date().isoformat() for m in doc["moves"]}):
        for v in card.build(day, now=2e9)["verdicts"]:
            verdicts[v["symbol"]] = v
    return scored, verdicts
