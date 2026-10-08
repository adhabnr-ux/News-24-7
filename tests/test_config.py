from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml

from news247.config import ConfigError, build_config, deep_merge, expand_env, load_config, load_dotenv
from news247.models import Severity
from news247.sources import SOURCE_TYPES


def test_defaults_build_without_any_file():
    cfg = build_config({})
    assert len(cfg.sources) > 30
    assert {s["type"] for s in cfg.sources} <= set(SOURCE_TYPES)
    assert cfg.market.benchmark in cfg.market.symbols
    names = [c.name for c in cfg.notify.channels]
    assert names == ["console"]
    assert cfg.scoring.severity_for(80) is Severity.CRITICAL
    assert cfg.scoring.severity_for(60) is Severity.HIGH
    assert cfg.scoring.severity_for(40) is Severity.MEDIUM
    assert cfg.scoring.severity_for(10) is Severity.LOW
    # index ETFs get tighter built-in thresholds than single stocks
    assert cfg.market.overrides["SPY"][0].pct < cfg.market.rules[0].pct


def test_example_config_is_valid():
    text = resources.files("news247.data").joinpath("config.example.yaml").read_text()
    cfg = build_config(yaml.safe_load(text))
    assert any(s["name"] == "x" and s["enabled"] is False for s in cfg.sources)
    assert "TSLA" in cfg.market.overrides
    assert {c.name for c in cfg.notify.channels} >= {"ntfy", "telegram", "discord", "email"}


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("N247_TOPIC", "secret-topic")
    out = expand_env({"a": "${N247_TOPIC}", "b": ["x-${MISSING:-fallback}"], "c": 5})
    assert out == {"a": "secret-topic", "b": ["x-fallback"], "c": 5}


def test_dotenv(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nexport N247_A='one'\nN247_B=two\nbad line\n")
    monkeypatch.delenv("N247_A", raising=False)
    monkeypatch.setenv("N247_B", "already-set")
    load_dotenv(env)
    import os

    assert os.environ["N247_A"] == "one"
    assert os.environ["N247_B"] == "already-set"  # real environment wins
    monkeypatch.delenv("N247_A")


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="unknown top-level"):
        build_config({"genral": {}})
    with pytest.raises(ConfigError, match=r"\[general\] unknown"):
        build_config({"general": {"user_agnet": "x"}})
    with pytest.raises(ConfigError, match="unknown channel"):
        build_config({"notify": {"pager": {}}})
    with pytest.raises(ConfigError, match="thresholds"):
        build_config({"scoring": {"critical": 10, "high": 50}})
    with pytest.raises(ConfigError, match="finnhub_token"):
        build_config({"market": {"provider": "finnhub"}})


def test_source_override_and_addition():
    cfg = build_config(
        {
            "sources": [
                {"name": "hackernews", "enabled": False},
                {"name": "mine", "type": "rss", "url": "https://example.com/feed"},
            ]
        }
    )
    by = {s["name"]: s for s in cfg.sources}
    assert by["hackernews"]["enabled"] is False and by["hackernews"]["type"] == "hackernews"
    assert by["mine"]["url"] == "https://example.com/feed"
    with pytest.raises(ConfigError, match="needs a 'type'"):
        build_config({"sources": [{"name": "nope"}]})


def test_without_default_sources():
    cfg = build_config(
        {"include_default_sources": False, "sources": [{"name": "a", "type": "rss", "url": "u"}]}
    )
    assert [s["name"] for s in cfg.sources] == ["a"]


def test_channels_and_market_groups():
    cfg = build_config(
        {
            "notify": {"ntfy": {"topic": "t", "min_severity": "critical"}, "console": {"enabled": False}},
            "market": {"groups": {"mine": {"symbols": ["AAA", "BBB"], "pct": 1.0}}, "symbols": ["aaa"]},
        }
    )
    ntfy = next(c for c in cfg.notify.channels if c.name == "ntfy")
    assert ntfy.enabled and ntfy.min_severity is Severity.CRITICAL and ntfy.options == {"topic": "t"}
    assert [g.name for g in cfg.market.groups] == ["mine"]
    assert cfg.market.symbols == ["AAA", "SPY"]


def test_load_config_file_relative_data_dir(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("general:\n  data_dir: ./state\n")
    cfg = load_config(p)
    assert cfg.data_path == tmp_path / "state"
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_deep_merge():
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}, "d": 4}) == {"a": {"b": 1, "c": 3}, "d": 4}
