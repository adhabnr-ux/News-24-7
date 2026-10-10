"""Backtest the importance criteria against history.

``news247/data/history.yaml`` lists real market-moving events (with the headlines that
first reported them) and "noise" headlines that sounded important but moved nothing. The
backtest scores each headline exactly as live items are scored and reports:

* recall    — share of real events where at least one first-report headline reaches the
              alert threshold (default HIGH, i.e. it would have been texted to you)
* false alarms — share of noise headlines that would have been texted

Run ``news247 backtest`` after changing keywords/themes to see what you gained or broke.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .analysis.scorer import Scorer
from .config import Config, load_package_yaml
from .market.universe import Universe
from .models import Analysis, NewsItem, Severity, SourceTier

_TIERS = {t.name.lower(): t for t in SourceTier}
# Mirrors the default config: posts on these labs' own channels are always texted (VIP rule)
VIP_ENTITIES = {"OpenAI", "Anthropic", "Google DeepMind", "xAI"}


@dataclass
class EventResult:
    event: dict[str, Any]
    best: Analysis
    best_headline: str
    caught: bool  # caught from its FIRST report (before coverage of the move existed)
    per_headline: list[tuple[str, Analysis]] = field(default_factory=list)
    caught_any: bool = False  # caught from first reports or later coverage
    caught_deployed: bool = False  # caught as deployed: scorer OR the VIP rule for AI-lab posts


@dataclass
class BacktestReport:
    threshold: Severity
    events: list[EventResult]
    noise: list[tuple[str, Analysis]]

    @property
    def recall(self) -> float:
        return sum(e.caught for e in self.events) / len(self.events) if self.events else 0.0

    @property
    def recall_deployed(self) -> float:
        return sum(e.caught_deployed for e in self.events) / len(self.events) if self.events else 0.0

    @property
    def recall_any(self) -> float:
        return sum(e.caught_any for e in self.events) / len(self.events) if self.events else 0.0

    @property
    def false_alarm_rate(self) -> float:
        if not self.noise:
            return 0.0
        return sum(a.severity >= self.threshold for _, a in self.noise) / len(self.noise)

    def by_category(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for e in self.events:
            cat = e.event.get("category", "other")
            caught, total = out.get(cat, (0, 0))
            out[cat] = (caught + e.caught, total + 1)
        return out


def headline_item(h: Any, default_tier: str = "media") -> NewsItem:
    """A history headline is either a string or {text, tier, entities, summary, source, tickers}.
    ``tickers`` are the exchange:ticker categories a wire attaches (read by the RSS source)."""
    if isinstance(h, str):
        h = {"text": h}
    tier = _TIERS[str(h.get("tier", default_tier)).lower()]
    return NewsItem(
        source=h.get("source", "history"),
        title=h["text"],
        summary=h.get("summary", ""),
        tier=tier,
        tickers=[str(t).upper() for t in h.get("tickers", [])],
        extra={"entities": list(h.get("entities", [])), "boost": float(h.get("boost", 0))},
    )


def load_history() -> dict[str, Any]:
    return load_package_yaml("history.yaml") or {}


def history_universe(history: dict[str, Any]) -> Universe | None:
    """The market caps the live universe would have known for the history's small caps."""
    entries: dict[str, dict[str, Any]] = {}
    for ev in history.get("events", []):
        sc = ev.get("smallcap")
        if sc:
            entries[str(sc["symbol"]).upper()] = sc
    for h in history.get("noise", []):
        sc = h.get("smallcap") if isinstance(h, dict) else None
        if sc:
            entries[str(sc["symbol"]).upper()] = sc
    return Universe.stub(entries) if entries else None


def _event_universe(history: dict[str, Any], sc: dict[str, Any]) -> Universe:
    u = history_universe(history) or Universe()
    entries = {
        sym: {"name": li.name, "mcap": li.market_cap, "price": li.price, "industry": li.industry}
        for sym, li in u.by_symbol.items()
    }
    entries[str(sc["symbol"]).upper()] = sc
    return Universe.stub(entries)


def history_scorer(cfg: Config, history: dict[str, Any]) -> Scorer:
    scorer = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    uni = history_universe(history)
    if uni is not None:
        scorer.attach_universe(uni, **cfg.smallcap.filters())
    return scorer


def run_backtest(
    cfg: Config, history: dict[str, Any] | None = None, threshold: Severity = Severity.HIGH
) -> BacktestReport:
    history = history if history is not None else load_history()
    scorer = history_scorer(cfg, history)
    events: list[EventResult] = []
    base_universe = scorer.smallcap.universe if scorer.smallcap is not None else None
    for ev in history.get("events", []):
        # each event is scored with the size its company had that morning (SOUN was $0.9B when
        # NVIDIA's stake surfaced and ~$4.7B when its exit did)
        sc = ev.get("smallcap")
        if scorer.smallcap is not None:
            scorer.smallcap.universe = _event_universe(history, sc) if sc else base_universe
        firsts = [
            headline_item(h, ev.get("tier", "media"))
            for h in ev.get("first_reports", ev.get("headlines", []))
        ]
        later = [headline_item(h, "media") for h in ev.get("coverage", [])]
        scored_first = [(i.title, scorer.score(i)) for i in firsts]
        scored_later = [(i.title, scorer.score(i)) for i in later]
        if not scored_first and not scored_later:
            continue
        pool = scored_first or scored_later
        title, best = max(pool, key=lambda s: s[1].score)
        caught_first = bool(scored_first) and best.severity >= threshold
        caught_any = any(a.severity >= threshold for _, a in scored_first + scored_later)
        vip = any(
            i.tier is SourceTier.PRIMARY and VIP_ENTITIES & set(i.extra.get("entities", [])) for i in firsts
        )
        deployed = caught_first or (vip and threshold <= Severity.HIGH)
        events.append(
            EventResult(ev, best, title, caught_first, scored_first + scored_later, caught_any, deployed)
        )
    if scorer.smallcap is not None:
        scorer.smallcap.universe = base_universe
    noise = []
    for h in history.get("noise", []):
        item = headline_item(h)
        noise.append((item.title, scorer.score(item)))
    return BacktestReport(threshold, events, noise)


def format_report(rep: BacktestReport, verbose: bool = False) -> str:
    lines = [
        f"Backtest: {len(rep.events)} historical market-moving events, {len(rep.noise)} noise headlines"
        f" (alert threshold: {rep.threshold.name})",
        "",
        f"  Caught from the FIRST report:    {sum(e.caught for e in rep.events)}/{len(rep.events)}"
        f"  = {rep.recall:.0%}   (texted before the move was news)",
        f"  As deployed (+ VIP rule):        {sum(e.caught_deployed for e in rep.events)}/{len(rep.events)}"
        f"  = {rep.recall_deployed:.0%}   (AI-lab posts are always texted)",
        f"  Caught at all (incl. coverage):  {sum(e.caught_any for e in rep.events)}/{len(rep.events)}"
        f"  = {rep.recall_any:.0%}",
        f"  False alarms on noise:           {sum(a.severity >= rep.threshold for _, a in rep.noise)}/{len(rep.noise)}"
        f"  = {rep.false_alarm_rate:.0%}",
        "",
        "  By category:",
    ]
    for cat, (c, t) in sorted(rep.by_category().items()):
        mark = "✓" if c == t else "✗"
        lines.append(f"    {mark} {cat:<34} {c}/{t}")
    missed = [e for e in rep.events if not e.caught_deployed]
    if missed:
        lines += ["", "  MISSED events (best headline and its score):"]
        for e in missed:
            lines.append(
                f"    {e.event.get('date', '?')}  {e.event.get('category', '')}: {e.event.get('move', '')}"
            )
            lines.append(f"        {e.best.score:5.1f} {e.best.severity.name:<8} {e.best_headline[:110]}")
            if verbose:
                lines += [f"            {r}" for r in e.best.reasons]
    alarms = [(t, a) for t, a in rep.noise if a.severity >= rep.threshold]
    if alarms:
        lines += ["", "  FALSE ALARMS (noise that would have been texted):"]
        for t, a in alarms:
            lines.append(f"        {a.score:5.1f} {a.severity.name:<8} {t[:110]}")
            if verbose:
                lines += [f"            {r}" for r in a.reasons]
    if verbose:
        lines += ["", "  All events:"]
        for e in rep.events:
            lines.append(
                f"    {'✓' if e.caught else '✗'} {e.best.score:5.1f} {e.event.get('date', '?')} {e.best_headline[:100]}"
            )
    return "\n".join(lines)
