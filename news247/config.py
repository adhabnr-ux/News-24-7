"""Configuration loading.

A config file is optional: with no file at all the monitor runs with the built-in
source list, built-in knowledge base and console + dashboard output. Anything set in
``config.yaml`` is merged on top of those defaults.

Strings may reference environment variables as ``${NAME}`` or ``${NAME:-fallback}``;
a ``.env`` file next to the config (or in the working directory) is loaded first, so
secrets such as bot tokens never have to live in the YAML itself.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .models import Severity

# innermost ${NAME} / ${NAME:-default} (default may not contain "$", "{" or "}"), so nested
# defaults like ${SMS_TO:-${IMESSAGE_TO:-}} resolve from the inside out
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^${}]*))?\}")


class ConfigError(ValueError):
    pass


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def expand_env(value: Any) -> Any:
    """Recursively substitute ${VAR} / ${VAR:-default} in strings."""
    if isinstance(value, str):
        for _ in range(10):  # nesting depth guard
            # shell semantics: the default applies when the variable is unset *or empty*
            new = _ENV_RE.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
            if new == value:
                break
            value = new
        return value
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def as_bool(value: Any, default: bool = True) -> bool:
    """YAML booleans, plus strings from ${ENV} substitution ("true", "0", "no", "")."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("", "none", "null"):
        return default
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    raise ConfigError(f"expected true/false, got {value!r}")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_package_yaml(name: str) -> Any:
    text = resources.files("news247.data").joinpath(name).read_text(encoding="utf-8")
    return yaml.safe_load(text)


# --------------------------------------------------------------------------- sections


@dataclass
class GeneralConfig:
    data_dir: str = "./data"
    user_agent: str = "News247Monitor/1.0 (contact: set general.user_agent in config.yaml)"
    max_item_age_minutes: float = 30.0
    log_level: str = "INFO"
    http_timeout_s: float = 15.0
    max_concurrency: int = 32
    db_retention_days: int = 30
    # free hosts that sleep when idle (Render Free: after 15 min without inbound traffic):
    # News247 requests its own public /health this often to stay awake 24/7
    keepalive_url: str = ""
    keepalive_s: float = 600.0
    # free Postgres (Neon/Supabase) for what must survive a temporary disk: phone subscriptions
    state_db: str = ""


@dataclass
class ScoringConfig:
    critical: float = 75.0
    high: float = 55.0
    medium: float = 35.0
    watchlist: list[str] = field(default_factory=list)
    keywords: dict[str, float] = field(default_factory=dict)  # extra/override keyword weights
    companies: list[dict[str, Any]] = field(default_factory=list)  # extra companies
    themes: dict[str, dict[str, Any]] = field(default_factory=dict)  # extra themes
    mute: list[str] = field(default_factory=list)  # regexes: matching items are never alerted
    cluster_window_minutes: float = 180.0

    def severity_for(self, score: float) -> Severity:
        if score >= self.critical:
            return Severity.CRITICAL
        if score >= self.high:
            return Severity.HIGH
        if score >= self.medium:
            return Severity.MEDIUM
        return Severity.LOW


@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str = "ollama"  # "ollama" (native API) or "openai" (any OpenAI-compatible server)
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b-instruct"
    api_key: str = ""
    timeout_s: float = 10.0
    min_score: float = 25.0  # only items scoring at least this are sent to the model
    budget_ms: int = 2500  # max time a HIGH alert waits on the model before going out anyway
    adjust: bool = True  # let the model move the score up/down
    max_concurrency: int = 2
    temperature: float = 0.0


@dataclass
class MoveRule:
    window: int  # seconds
    pct: float  # absolute % change that triggers


@dataclass
class GroupRule:
    name: str
    symbols: list[str]
    pct: float = 2.0  # average move of the group
    window: int = 600
    min_members: int = 3  # how many members must individually move >= pct/2 in the same direction


@dataclass
class MarketConfig:
    enabled: bool = True
    provider: str = "yahoo"  # "yahoo" (free, polled) or "finnhub" (free key, real-time websocket)
    finnhub_token: str = ""
    poll_seconds: float = 15.0
    symbols: list[str] = field(default_factory=list)
    benchmark: str = "SPY"
    rules: list[MoveRule] = field(default_factory=list)
    groups: list[GroupRule] = field(default_factory=list)
    cooldown_minutes: float = 20.0
    correlate_minutes: float = 45.0  # how far back to look for a news catalyst of a move
    day_move_pct: float = 7.0  # alert once per day when a symbol is this far from previous close
    extended_hours: bool = True  # include pre/post-market prices (yahoo chart mode)
    overrides: dict[str, list[MoveRule]] = field(default_factory=dict)  # per-symbol rules


@dataclass
class SmallCapConfig:
    """The little things: small/mid caps sized against their market cap, plus a movers radar."""

    enabled: bool = True
    # the universe: every listed US stock with its market cap (Nasdaq's public screener)
    universe_url: str = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
    refresh_hours: float = 6.0
    # pump-and-dump guards: below these a catalyst is shown but never boosted
    min_market_cap: float = 30e6
    min_price: float = 1.0
    max_market_cap: float = 10e9  # above this the big-cap scorer is in charge
    # the radar: small caps ripping with no headline yet (the news is about to drop)
    radar: bool = True
    radar_provider: str = "yahoo"  # "yahoo" (small-cap gainers/losers screeners) or "nasdaq"
    radar_seconds: float = 60.0
    radar_max_cap: float = 2e9  # small-cap thresholds below this; size-scaled ones above
    radar_min_pct: float = 20.0  # day move that puts a name on the radar
    radar_jump_pct: float = 8.0  # move between two scans (~1 min) that flags it at once
    radar_min_dollar_volume: float = 2e6  # a move nobody trades is not a move
    radar_push_pct: float = 35.0  # at/above this (with real volume) the radar pushes (HIGH)
    radar_daily_pushes: int = 8  # most radar pushes per day per class (small / big); the rest show in the app
    # bigger companies move less: their own day-move thresholds (jump = half, push = 1.5x)
    radar_mid_min_pct: float = 10.0  # $2B-$10B
    radar_large_min_pct: float = 6.0  # $10B-$200B (MRNA, $75B: +9% pre-market on Oct 9 2026)
    radar_mega_min_pct: float = 4.0  # $200B+
    # pre/after-hours sweep of the largest companies (Yahoo's screeners only rank the regular session)
    sweep_size: int = 400
    sweep_seconds: float = 60.0

    def filters(self) -> dict[str, float]:
        return {
            "min_market_cap": self.min_market_cap,
            "max_market_cap": self.max_market_cap,
            "min_price": self.min_price,
        }


@dataclass
class OptionsConfig:
    """The options tape: unusual volume and contracts up thousands of percent (market/options.py).
    Every threshold below can be set under ``options:`` in config.yaml."""

    enabled: bool = True
    # which companies: every listed US company worth at least this much, up to the largest
    min_market_cap: float = 1e9
    # where chains come from: "auto" = Alpaca when its keys are set (Cboe as the fallback),
    # else Cboe; "alpaca" or "cboe" to force one. fallback: try the other when one fails.
    provider: str = "auto"
    fallback: bool = True
    # Cboe's public delayed-quotes JSON (~15 min delayed, every expiry and strike, no key)
    chain_url: str = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    request_interval_s: float = 0.35  # at most ~3 Cboe reads a second (be polite to a free feed)
    # Alpaca options data (keys from app.alpaca.markets; ALPACA_KEY / ALPACA_SECRET in the
    # environment are picked up automatically). Free accounts get the "indicative" feed
    # (trades ~15 min delayed, quotes adjusted); the paid options subscription is "opra".
    alpaca_key: str = ""
    alpaca_secret: str = ""
    alpaca_feed: str = "indicative"
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_trading_url: str = (
        "https://paper-api.alpaca.markets"  # open interest; live keys: api.alpaca.markets
    )
    alpaca_request_interval_s: float = 0.31  # free plan: 200 requests a minute
    alpaca_max_pages: int = 20  # 1,000 contracts a page
    concurrency: int = 3  # chains read at once
    batch_size: int = 30  # chains per scheduling round
    max_quote_age_s: float = 1800.0  # a chain whose stock quote is older than this is stale: no alert
    # who is read most often: today's movers (|move| >= hot_move_pct) plus radar / news names,
    # up to hot_size of them every hot_seconds; everyone else rotates through, largest first
    hot_move_pct: float = 3.0
    hot_size: int = 40
    hot_seconds: float = 180.0
    min_refetch_seconds: float = 300.0  # the rotation never re-reads a chain sooner than this
    # spikes: a contract trading at spike_min_pct+ above its previous close
    spike_min_pct: float = 1000.0  # "thousands of percent": 1,000% = 11x the previous close
    spike_min_volume: int = 25  # contracts traded today
    spike_min_price: float = 0.10  # last price, $ per share (a $0.01 -> $0.11 print is noise)
    spike_min_premium: float = 5000.0  # $ traded in that contract today (volume x price x 100)
    spike_confirm_fraction: float = 0.5  # pushes only if the bid still shows >= half the threshold
    stale_base_ratio: float = 0.3  # previous close below 30% of its model value = stale print
    stale_min_fair: float = 0.05  # ... when that model value is at least $0.05
    # unusual volume: contracts trading far above their open interest, with real money
    flow_min_volume: int = 500  # contracts traded today
    flow_oi_multiple: float = 3.0  # volume >= 3x open interest (any volume when OI is 0)
    flow_min_contract_premium: float = 100_000.0  # $ in that contract today
    flow_min_premium: float = 250_000.0  # $ across a company's unusual contracts before an alert
    flow_push_premium: float = 1_000_000.0  # pushes at/above this ...
    flow_push_oi_multiple: float = 5.0  # ... when the biggest contract is >= 5x its open interest
    flow_refire_multiple: float = 2.0  # speaks again about a company when its unusual premium doubles
    # noise control
    daily_pushes: int = 12  # most options pushes per day; the rest show in the app
    max_rows: int = 5  # contracts listed per alert


@dataclass
class ChannelConfig:
    name: str
    enabled: bool = False
    min_severity: Severity = Severity.HIGH
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotifyConfig:
    channels: list[ChannelConfig] = field(default_factory=list)
    rate_limit_per_minute: int = 20
    quiet_hours: dict[str, Any] | None = None  # {start: "23:00", end: "07:00", min_severity: critical}


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8247
    token: str = (
        ""  # if set, the dashboard/API require ?token=... (do set it before exposing beyond localhost)
    )
    public_url: str = ""  # https://… address Macs use to reach the relay endpoint (auto-detected if empty)


@dataclass
class Config:
    general: GeneralConfig
    scoring: ScoringConfig
    llm: LLMConfig
    market: MarketConfig
    notify: NotifyConfig
    web: WebConfig
    sources: list[dict[str, Any]]
    knowledge: dict[str, Any]
    path: Path | None = None
    smallcap: SmallCapConfig = field(default_factory=SmallCapConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)

    @property
    def data_path(self) -> Path:
        p = Path(self.general.data_dir)
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent / p
        return p


# --------------------------------------------------------------------------- building

DEFAULT_RULES = [MoveRule(60, 1.5), MoveRule(300, 3.0), MoveRule(900, 5.0)]
_INDEX_RULES = [MoveRule(60, 0.5), MoveRule(300, 0.8), MoveRule(900, 1.2)]
_ETF_RULES = [MoveRule(60, 1.0), MoveRule(300, 2.0), MoveRule(900, 3.0)]
DEFAULT_OVERRIDES = {
    "SPY": _INDEX_RULES,
    "DIA": _INDEX_RULES,
    "IWM": [MoveRule(60, 0.7), MoveRule(300, 1.1), MoveRule(900, 1.6)],
    "QQQ": [MoveRule(60, 0.6), MoveRule(300, 1.0), MoveRule(900, 1.5)],
    "TLT": _INDEX_RULES,
    "SMH": _ETF_RULES,
    "IGV": _ETF_RULES,
    "XLK": _ETF_RULES,
    "XLE": _ETF_RULES,
    "GLD": _ETF_RULES,
    "USO": _ETF_RULES,
}
CHANNEL_NAMES = (
    "console",
    "desktop",
    "ntfy",
    "telegram",
    "discord",
    "slack",
    "pushover",
    "email",
    "webhook",
    "imessage",
    "relay",
    "twilio",
    "bluebubbles",
    "sendblue",
    "blooio",
    "textbelt",
    "whatsapp",
    "whatsapp_cloud",
    "webpush",
)


def _dataclass_from(cls: type, data: dict[str, Any] | None, section: str) -> Any:
    data = dict(data or {})
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}")
    for name, f in cls.__dataclass_fields__.items():  # type: ignore[attr-defined]
        if name not in data:
            continue
        try:
            if f.type in ("bool", bool):
                data[name] = as_bool(data[name])
            elif f.type in ("int", int) and isinstance(data[name], str):
                data[name] = int(data[name])
            elif f.type in ("float", float) and isinstance(data[name], str):
                data[name] = float(data[name])
        except ValueError:
            raise ConfigError(f"[{section}] {name}: expected a number, got {data[name]!r}") from None
    return cls(**data)


def _merge_sources(defaults: list[dict[str, Any]], user: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """User entries override built-ins with the same name; new names are appended."""
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for src in defaults + list(user or []):
        if not isinstance(src, dict) or "name" not in src:
            raise ConfigError(f"every source needs a 'name': {src!r}")
        name = src["name"]
        if name in by_name:
            by_name[name] = {**by_name[name], **src}
        else:
            by_name[name] = dict(src)
            order.append(name)
    return [by_name[n] for n in order]


def build_config(raw: dict[str, Any] | None, path: Path | None = None) -> Config:
    raw = expand_env(raw or {})
    known_sections = {
        "general",
        "scoring",
        "llm",
        "market",
        "notify",
        "web",
        "smallcap",
        "options",
        "sources",
        "include_default_sources",
    }
    unknown = set(raw) - known_sections
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    general = _dataclass_from(GeneralConfig, raw.get("general"), "general")
    scoring = _dataclass_from(ScoringConfig, raw.get("scoring"), "scoring")
    scoring.watchlist = [t.upper() for t in scoring.watchlist]
    if not scoring.critical >= scoring.high >= scoring.medium:
        raise ConfigError("[scoring] thresholds must satisfy critical >= high >= medium")
    llm = _dataclass_from(LLMConfig, raw.get("llm"), "llm")
    if llm.provider not in ("ollama", "openai"):
        raise ConfigError("[llm] provider must be 'ollama' or 'openai'")

    knowledge = load_package_yaml("knowledge.yaml")

    m = dict(raw.get("market") or {})
    rules = [MoveRule(int(r["window"]), float(r["pct"])) for r in m.pop("rules", [])] or list(DEFAULT_RULES)
    overrides = dict(DEFAULT_OVERRIDES)
    for sym, rs in (m.pop("overrides", None) or {}).items():
        overrides[str(sym).upper()] = [MoveRule(int(r["window"]), float(r["pct"])) for r in rs]
    groups_raw = m.pop("groups", None)
    if groups_raw is None:
        groups_raw = knowledge.get("baskets", {})
    groups = []
    for gname, g in (groups_raw or {}).items():
        if not g or g.get("enabled", True) is False:
            continue
        g = {k: v for k, v in g.items() if k != "enabled"}
        groups.append(GroupRule(name=gname, **g))
    market = _dataclass_from(MarketConfig, m, "market")
    market.rules, market.groups, market.overrides = rules, groups, overrides
    if market.provider not in ("yahoo", "finnhub"):
        raise ConfigError("[market] provider must be 'yahoo' or 'finnhub'")
    if market.provider == "finnhub" and not market.finnhub_token:
        raise ConfigError("[market] provider 'finnhub' needs finnhub_token (free at finnhub.io)")
    if not market.symbols:
        syms = list(scoring.watchlist) or list(knowledge.get("default_watchlist", []))
        for g in groups:
            syms.extend(g.symbols)
        market.symbols = syms
    market.symbols = list(dict.fromkeys(s.upper() for s in [*market.symbols, market.benchmark]))

    n = dict(raw.get("notify") or {})
    rate = int(n.pop("rate_limit_per_minute", 20))
    quiet = n.pop("quiet_hours", None)
    channels: list[ChannelConfig] = []
    n.setdefault("console", {"enabled": True, "min_severity": "medium"})
    for cname, opts in n.items():
        if cname not in CHANNEL_NAMES:
            raise ConfigError(f"[notify] unknown channel '{cname}' (known: {', '.join(CHANNEL_NAMES)})")
        opts = dict(opts or {})
        channels.append(
            ChannelConfig(
                name=cname,
                enabled=as_bool(opts.pop("enabled", True)),
                min_severity=Severity.parse(opts.pop("min_severity", "high")),
                options=opts,
            )
        )
    notify = NotifyConfig(channels=channels, rate_limit_per_minute=rate, quiet_hours=quiet)
    web = _dataclass_from(WebConfig, raw.get("web"), "web")
    smallcap = _dataclass_from(SmallCapConfig, raw.get("smallcap"), "smallcap")
    if smallcap.radar_provider not in ("yahoo", "nasdaq"):
        raise ConfigError("[smallcap] radar_provider must be 'yahoo' or 'nasdaq'")
    if smallcap.radar_seconds < 20:
        raise ConfigError("[smallcap] radar_seconds must be at least 20 (be polite to free endpoints)")

    options = _dataclass_from(OptionsConfig, raw.get("options"), "options")
    if options.request_interval_s < 0.2:
        raise ConfigError("[options] request_interval_s must be at least 0.2 (be polite to free endpoints)")
    if options.spike_min_pct <= 0 or not 0 < options.spike_confirm_fraction <= 1:
        raise ConfigError("[options] spike_min_pct must be > 0 and spike_confirm_fraction in (0, 1]")
    if options.concurrency < 1 or options.batch_size < 1:
        raise ConfigError("[options] concurrency and batch_size must be at least 1")
    if "{symbol}" not in options.chain_url:
        raise ConfigError("[options] chain_url must contain {symbol}")
    options.provider = options.provider.strip().lower()
    if options.provider not in ("auto", "alpaca", "cboe"):
        raise ConfigError("[options] provider must be 'auto', 'alpaca' or 'cboe'")
    options.alpaca_feed = options.alpaca_feed.strip().lower()
    if options.alpaca_feed not in ("indicative", "opra", ""):
        raise ConfigError("[options] alpaca_feed must be 'indicative' or 'opra'")
    if options.alpaca_request_interval_s < 0.005 or options.alpaca_max_pages < 1:
        raise ConfigError("[options] alpaca_request_interval_s must be >= 0.005 and alpaca_max_pages >= 1")
    # the same keys the Alpaca news source uses (or the names Alpaca's own tools use)
    options.alpaca_key = (
        options.alpaca_key or os.environ.get("ALPACA_KEY", "") or os.environ.get("ALPACA_API_KEY", "")
    ).strip()
    options.alpaca_secret = (
        options.alpaca_secret
        or os.environ.get("ALPACA_SECRET", "")
        or os.environ.get("ALPACA_SECRET_KEY", "")
    ).strip()
    if options.provider == "alpaca" and not (options.alpaca_key and options.alpaca_secret):
        raise ConfigError("[options] provider 'alpaca' needs ALPACA_KEY and ALPACA_SECRET")

    defaults = (
        expand_env(load_package_yaml("default_sources.yaml"))
        if as_bool(raw.get("include_default_sources", True))
        else []
    )
    sources = _merge_sources(defaults, raw.get("sources") or [])
    for s in sources:
        if "type" not in s:
            raise ConfigError(f"source '{s['name']}' needs a 'type'")
        s["enabled"] = as_bool(s.get("enabled", True))

    return Config(general, scoring, llm, market, notify, web, sources, knowledge, path, smallcap, options)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from ``path`` (or ./config.yaml if present, else pure defaults)."""
    candidate = Path(path) if path else Path("config.yaml")
    load_dotenv(candidate.parent / ".env" if path else Path(".env"))
    if candidate.is_file():
        with candidate.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{candidate}: top level must be a mapping")
        return build_config(raw, candidate.resolve())
    if path:
        raise ConfigError(f"config file not found: {candidate}")
    # No config file (typical on a cloud host): use the documented example, which takes
    # everything personal from environment variables.
    return build_config(load_package_yaml("config.example.yaml"), None)
