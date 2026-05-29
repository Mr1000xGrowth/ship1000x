"""Tests for the gitleaks secret-scan helper.

No network, no real gitleaks binary call. The subprocess invocation is
mocked so the tests verify the sanitisation contract end-to-end.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ship1000x.core import gitleaks_scan


@pytest.fixture
def fake_repo(tmp_path):
    """A directory that looks like a git repo path (real .git presence
    is not required since we mock subprocess)."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    return repo


class TestIsEnabled:
    def test_returns_false_for_empty_or_missing_key(self):
        assert gitleaks_scan.is_enabled({}) is False
        assert gitleaks_scan.is_enabled({"unrelated": True}) is False

    def test_returns_false_when_disabled(self):
        assert gitleaks_scan.is_enabled({"gitleaks_scan": False}) is False

    def test_returns_true_when_enabled(self):
        assert gitleaks_scan.is_enabled({"gitleaks_scan": True}) is True

    def test_returns_false_on_non_dict_input(self):
        assert gitleaks_scan.is_enabled(None) is False
        assert gitleaks_scan.is_enabled("yes") is False


class TestScanRepoSanitisation:
    """Verify that the matched secret + committer identity are stripped
    before any finding leaves scan_repo()."""

    @staticmethod
    def _mock_subprocess_with(findings: list[dict]):
        """Return a context manager that mocks subprocess.run to emit
        the given findings as JSON on stdout."""

        class _Result:
            def __init__(self, stdout: str):
                self.stdout = stdout
                self.returncode = 0

        def _runner(*_args, **_kwargs):
            return _Result(json.dumps(findings))

        return patch.object(gitleaks_scan.subprocess, "run", side_effect=_runner)

    def test_strips_secret_and_committer_identity(self, fake_repo):
        raw_finding = {
            "RuleID": "aws-access-token",
            "Description": "AWS Access Token",
            "File": "/Users/alice/projects/sensitive/config.py",
            "StartLine": 42,
            "Commit": "abc123def456",
            "Date": "2026-05-20T12:00:00Z",
            # These three must NEVER appear in the sanitised output
            "Match": "AKIAIOSFODNN7EXAMPLE",
            "Secret": "AKIAIOSFODNN7EXAMPLE",
            "Author": "Alice Liddell",
            "Email": "alice@example.com",
        }
        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=True
        ), self._mock_subprocess_with([raw_finding]):
            findings = gitleaks_scan.scan_repo(fake_repo)
        assert len(findings) == 1
        f = findings[0]
        # Captured categorical / locator fields
        assert f["rule_id"] == "aws-access-token"
        assert f["description"] == "AWS Access Token"
        assert f["line"] == 42
        assert f["commit"] == "abc123def456"
        assert f["date"] == "2026-05-20T12:00:00Z"
        # File path stripped to basename
        assert f["file"] == "config.py"
        assert "/Users/" not in f["file"]
        # The dangerous fields are absent
        for forbidden in ("match", "secret", "author", "email", "Match", "Secret"):
            assert forbidden not in f

    def test_truncates_overly_long_fields(self, fake_repo):
        raw_finding = {
            "RuleID": "x" * 200,
            "Description": "y" * 500,
            "File": "a" * 300 + ".py",
            "StartLine": 1,
            "Commit": "z" * 100,
            "Date": "0" * 50,
        }
        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=True
        ), self._mock_subprocess_with([raw_finding]):
            findings = gitleaks_scan.scan_repo(fake_repo)
        f = findings[0]
        assert len(f["rule_id"]) <= 64
        assert len(f["description"]) <= 200
        assert len(f["file"]) <= 128
        assert len(f["commit"]) <= 40
        assert len(f["date"]) <= 30

    def test_returns_empty_when_binary_missing(self, fake_repo):
        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=False
        ):
            assert gitleaks_scan.scan_repo(fake_repo) == []

    def test_returns_empty_when_repo_path_missing(self, tmp_path):
        absent = tmp_path / "no-such-dir"
        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=True
        ):
            assert gitleaks_scan.scan_repo(absent) == []

    def test_returns_empty_on_timeout(self, fake_repo):
        import subprocess as sp

        def _raise(*_args, **_kwargs):
            raise sp.TimeoutExpired(cmd="gitleaks", timeout=10)

        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=True
        ), patch.object(gitleaks_scan.subprocess, "run", side_effect=_raise):
            assert gitleaks_scan.scan_repo(fake_repo) == []

    def test_returns_empty_on_invalid_json(self, fake_repo):
        class _Result:
            stdout = "not json {{"
            returncode = 0

        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=True
        ), patch.object(
            gitleaks_scan.subprocess, "run", return_value=_Result()
        ):
            assert gitleaks_scan.scan_repo(fake_repo) == []

    def test_handles_empty_stdout(self, fake_repo):
        class _Result:
            stdout = ""
            returncode = 0

        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=True
        ), patch.object(
            gitleaks_scan.subprocess, "run", return_value=_Result()
        ):
            assert gitleaks_scan.scan_repo(fake_repo) == []

    def test_handles_non_list_payload(self, fake_repo):
        """gitleaks may return `null` when no findings."""
        class _Result:
            stdout = "null"
            returncode = 0

        with patch.object(
            gitleaks_scan, "is_gitleaks_available", return_value=True
        ), patch.object(
            gitleaks_scan.subprocess, "run", return_value=_Result()
        ):
            assert gitleaks_scan.scan_repo(fake_repo) == []


class TestPrivacyOutputContract:
    """Whitelist check — gitleaks alert events should only carry the
    fields documented in privacy.py ALLOWED_META_KEYS."""

    def test_sanitize_finding_output_keys(self):
        raw = {
            "RuleID": "aws-access-token",
            "Description": "AWS",
            "File": "config.py",
            "StartLine": 1,
            "Commit": "abc",
            "Date": "2026-05-20",
            "Match": "SECRET",
            "Author": "X",
        }
        out = gitleaks_scan._sanitize_finding(raw)
        assert set(out.keys()) == {
            "rule_id", "description", "file", "line", "commit", "date"
        }
