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

    return Config(general, scoring, llm, market, notify, web, sources, knowledge, path)


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
