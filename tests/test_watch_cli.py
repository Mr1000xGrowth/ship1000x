"""CLI smoke tests for `ship1000x watch` (Wave 3 / Day 3-5)."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.runtime import watch_daemon


def test_watch_no_foreground_exits_2():
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["watch", "--no-foreground"])
    assert result.exit_code == 2
    assert "not supported in V1" in result.output.lower() or "v1" in result.output.lower()


def test_watch_foreground_runs_with_max_ticks(tmp_path, monkeypatch):
    """End-to-end: --foreground --interval 5 --max-ticks 1 ticks once and exits."""
    monkeypatch.setattr(watch_daemon, "DROP_DIR", tmp_path / "drop")

    # Inject a fake psutil module so the CLI doesn't need the real one.
    fake_psutil = SimpleNamespace(
        process_iter=lambda _attrs: [],
        net_connections=lambda kind="inet": [],
        CONN_LISTEN="LISTEN",
        NoSuchProcess=type("NSP", (Exception,), {}),
        AccessDenied=type("AD", (Exception,), {}),
        Error=type("E", (Exception,), {}),
    )
    real_run = watch_daemon.run_foreground

    def patched_run(*, interval_sec, max_ticks, log_fn=None, psutil_mod=None):
        return real_run(
            interval_sec=interval_sec,
            max_ticks=max_ticks,
            log_fn=log_fn,
            psutil_mod=fake_psutil,
        )

    monkeypatch.setattr(watch_daemon, "run_foreground", patched_run)
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli,
        ["watch", "--interval", "5", "--max-ticks", "1"],
    )
    # Click click.exceptions.Exit(0) is exit_code 0
    assert result.exit_code == 0


def test_watch_help_mentions_optional_dep():
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["watch", "--help"])
    assert result.exit_code == 0
    assert "psutil" in result.output.lower()
    assert "foreground" in result.output.lower()
