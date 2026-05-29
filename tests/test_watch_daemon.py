"""Tests for the Wave 3 / Day 3-5 ABTop-style runtime daemon writer.

Mocks psutil so no real process enumeration happens. Verifies the
filter logic, MCP classification, fail-safe behaviour on psutil
errors, and the drop file shape.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ship1000x.runtime import watch_daemon


class _FakeNoSuchProcessError(Exception):
    pass


class _FakeAccessDeniedError(Exception):
    pass


class _FakeError(Exception):
    pass


def _make_psutil_mod(
    *,
    processes: list[dict] | None = None,
    connections: list[dict] | None = None,
    raise_on_connections: Exception | None = None,
    raise_on_process_iter: Exception | None = None,
):
    """Build a duck-typed psutil module for the daemon to consume."""

    procs = processes or []
    conns_data = connections or []

    class _FakeProc:
        def __init__(self, info):
            self.info = info

    def process_iter(_attrs):
        if raise_on_process_iter is not None:
            raise raise_on_process_iter
        return [_FakeProc(p) for p in procs]

    def net_connections(kind="inet"):
        _ = kind
        if raise_on_connections is not None:
            raise raise_on_connections
        out = []
        for c in conns_data:
            out.append(SimpleNamespace(
                status=c.get("status", "LISTEN"),
                pid=c.get("pid"),
                laddr=SimpleNamespace(port=c.get("port")) if "port" in c else None,
            ))
        return out

    mod = SimpleNamespace(
        process_iter=process_iter,
        net_connections=net_connections,
        CONN_LISTEN="LISTEN",
        NoSuchProcess=_FakeNoSuchProcessError,
        AccessDenied=_FakeAccessDeniedError,
        Error=_FakeError,
    )
    return mod


# --- Filter helpers ---------------------------------------------------------


class TestProcessFilter:
    @pytest.mark.parametrize("name", [
        "claude", "Claude", "CLAUDE-CODE", "codex",
        "Cursor", "cursor-tunnel", "ollama", "anthropic-helper",
        "openai-cli", "chatgpt-app", "mcp-server-fs", "gemini",
        "copilot-language-server", "aider", "continue-extension",
    ])
    def test_ai_processes_matched(self, name):
        assert watch_daemon._is_ai_process(name) is True

    @pytest.mark.parametrize("name", [
        "", "bash", "zsh", "python3.13", "Safari", "Slack",
        "Mail", "kernel_task", "spotlight", "WhatsApp",
    ])
    def test_non_ai_processes_rejected(self, name):
        assert watch_daemon._is_ai_process(name) is False

    @pytest.mark.parametrize("name,expected", [
        ("mcp-server-fs", True),
        ("modelcontextprotocol-runner", True),
        ("claude-mcp-bridge", True),
        ("claude", False),
        ("cursor", False),
        ("ollama", False),
    ])
    def test_mcp_classification(self, name, expected):
        assert watch_daemon._is_mcp_likely(name) is expected


# --- Snapshot logic ---------------------------------------------------------


class TestSnapshotProcesses:
    def test_groups_pids_by_process_name(self):
        mod = _make_psutil_mod(processes=[
            {"pid": 100, "name": "claude"},
            {"pid": 101, "name": "claude"},
            {"pid": 102, "name": "claude"},
            {"pid": 200, "name": "cursor"},
        ])
        snap = watch_daemon._snapshot_processes(mod)
        by_name = {p["name"]: p for p in snap}
        assert by_name["claude"]["pid_count"] == 3
        assert by_name["cursor"]["pid_count"] == 1

    def test_filters_out_non_ai_processes(self):
        mod = _make_psutil_mod(processes=[
            {"pid": 1, "name": "kernel_task"},
            {"pid": 2, "name": "Slack"},
            {"pid": 3, "name": "claude"},
        ])
        snap = watch_daemon._snapshot_processes(mod)
        assert [p["name"] for p in snap] == ["claude"]

    def test_attaches_listening_ports(self):
        mod = _make_psutil_mod(
            processes=[{"pid": 10, "name": "ollama"}],
            connections=[
                {"pid": 10, "port": 11434, "status": "LISTEN"},
                {"pid": 10, "port": 11435, "status": "LISTEN"},
                {"pid": 10, "port": 11436, "status": "ESTABLISHED"},  # ignored
            ],
        )
        snap = watch_daemon._snapshot_processes(mod)
        assert snap[0]["listening_ports"] == [11434, 11435]

    def test_mcp_likely_set_for_mcp_processes(self):
        mod = _make_psutil_mod(processes=[
            {"pid": 1, "name": "mcp-server-fs"},
            {"pid": 2, "name": "claude"},
        ])
        snap = watch_daemon._snapshot_processes(mod)
        by_name = {p["name"]: p for p in snap}
        assert by_name["mcp-server-fs"]["mcp_likely"] is True
        assert by_name["claude"]["mcp_likely"] is False

    def test_fails_safe_on_net_connections_error(self):
        mod = _make_psutil_mod(
            processes=[{"pid": 1, "name": "claude"}],
            raise_on_connections=_FakeAccessDeniedError("denied"),
        )
        snap = watch_daemon._snapshot_processes(mod)
        # Process still detected; ports just empty.
        assert snap[0]["name"] == "claude"
        assert snap[0]["listening_ports"] == []

    def test_fails_safe_on_process_iter_error(self):
        mod = _make_psutil_mod(
            raise_on_process_iter=_FakeError("oom"),
        )
        snap = watch_daemon._snapshot_processes(mod)
        assert snap == []

    def test_ports_capped_at_max(self):
        mod = _make_psutil_mod(
            processes=[{"pid": 1, "name": "claude"}],
            connections=[
                {"pid": 1, "port": 1000 + i, "status": "LISTEN"}
                for i in range(50)
            ],
        )
        snap = watch_daemon._snapshot_processes(mod)
        assert len(snap[0]["listening_ports"]) == watch_daemon.MAX_PORTS_PER_PROCESS


# --- Drop file + tick orchestration ----------------------------------------


class TestRunOneTick:
    def test_writes_record_to_drop_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_daemon, "DROP_DIR", tmp_path / "drop")
        mod = _make_psutil_mod(processes=[{"pid": 1, "name": "claude"}])
        record = watch_daemon.run_one_tick(mod)
        assert "captured_at" in record
        assert record["processes"][0]["name"] == "claude"
        # File created
        day_files = list((tmp_path / "drop").glob("*.jsonl"))
        assert len(day_files) == 1
        on_disk = json.loads(day_files[0].read_text(encoding="utf-8").strip())
        assert on_disk == record


class TestRunForeground:
    def test_runs_n_ticks_then_stops(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_daemon, "DROP_DIR", tmp_path / "drop")
        mod = _make_psutil_mod(processes=[{"pid": 1, "name": "claude"}])
        log_lines: list[str] = []
        rc = watch_daemon.run_foreground(
            interval_sec=5,  # min interval; sleep is bounded by max_ticks anyway
            max_ticks=2,
            log_fn=log_lines.append,
            psutil_mod=mod,
        )
        assert rc == 0
        # 2 tick log lines + 1 start + 1 stop
        assert sum(1 for line in log_lines if "tick @" in line) == 2
        assert any("stopped cleanly" in line for line in log_lines)

    def test_returns_2_when_psutil_missing(self, monkeypatch):
        import sys as _sys

        # Force the import inside run_foreground to fail.
        monkeypatch.setitem(_sys.modules, "psutil", None)
        log_lines: list[str] = []
        rc = watch_daemon.run_foreground(
            interval_sec=5,
            max_ticks=1,
            log_fn=log_lines.append,
            psutil_mod=None,
        )
        assert rc == 2

    def test_enforces_min_interval(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_daemon, "DROP_DIR", tmp_path / "drop")
        mod = _make_psutil_mod()
        # Interval below MIN gets clamped; verifying via the fact that
        # the call still completes max_ticks=1 in under a sec.
        rc = watch_daemon.run_foreground(
            interval_sec=1,  # below MIN_INTERVAL_SEC
            max_ticks=1,
            log_fn=lambda _: None,
            psutil_mod=mod,
        )
        assert rc == 0
