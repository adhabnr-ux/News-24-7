from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from news247.cli import build_parser, main


def test_score_command(capsys):
    assert main(["score", "Introducing GPT-6", "--tier", "primary", "--entity", "OpenAI"]) == 0
    out = capsys.readouterr().out
    assert "HIGH" in out and "theme ai_lab_launch" in out and "thresholds" in out


def test_init_writes_valid_config(tmp_path: Path, capsys):
    dest = tmp_path / "config.yaml"
    assert main(["init", "--path", str(dest)]) == 0
    assert yaml.safe_load(dest.read_text())["general"]["data_dir"] == "./data"
    assert (tmp_path / ".env").read_text().startswith("# Secrets")
    assert main(["init", "--path", str(dest)]) == 1  # refuses to overwrite
    assert main(["-c", str(dest), "score", "hello"]) == 0


def test_bad_config_exit_code(tmp_path: Path, capsys):
    bad = tmp_path / "c.yaml"
    bad.write_text("nonsense: true\n")
    assert main(["-c", str(bad), "score", "x"]) == 2
    assert "config error" in capsys.readouterr().err


def test_test_notify_without_channels(tmp_path: Path, capsys):
    c = tmp_path / "c.yaml"
    c.write_text("notify:\n  console: {enabled: false}\n")
    assert main(["-c", str(c), "test-notify"]) == 1
    assert "No notification channels" in capsys.readouterr().out


def test_test_notify_console(tmp_path: Path, capsys):
    c = tmp_path / "c.yaml"
    c.write_text("general: {data_dir: " + str(tmp_path) + "}\n")
    assert main(["-c", str(c), "test-notify"]) == 0
    out = capsys.readouterr().out
    assert "TEST: OpenAI launches" in out and "console    ok" in out


def test_stats_without_db(tmp_path: Path, capsys):
    c = tmp_path / "c.yaml"
    c.write_text(f"general: {{data_dir: {tmp_path / 'nothing'}}}\n")
    assert main(["-c", str(c), "stats"]) == 1


def test_parser_defaults_to_run():
    p = build_parser()
    args = p.parse_args(["-c", "x.yaml", "run", "--no-web"])
    assert args.cmd == "run" and args.no_web and args.config == "x.yaml"
    with pytest.raises(SystemExit):
        p.parse_args(["score"])  # headline required
