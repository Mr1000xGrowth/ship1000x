"""Optional gitleaks integration for the git_multi collector.

Adds secret-scanning to the git ingestion path so SHIP can surface
"this commit may have leaked a credential" alongside the usual focus
+ cost signals. Strictly opt-in: enabled only when
``privacy_config["gitleaks_scan"]`` is true and the ``gitleaks`` binary
is on PATH. Silent no-op otherwise (the git collector never blocks).

Privacy model
-------------

Gitleaks emits findings that include the matched secret text, the
committer name, and the email address. SHIP **never** stores those.
The sanitisation layer in this module strips them before any event
leaves the function. We keep only:

- ``rule_id`` (categorical, e.g. ``aws-access-token``)
- ``description`` (short human label)
- ``file`` (basename only, not the full path)
- ``line`` (number, no surrounding context)
- ``commit`` (SHA, public anyway)
- ``date`` (ISO 8601)

This is enough for an audit-style "you may have leaked something in
commit X on file Y" alert without persisting the leaked value itself.

Inspiration : gitleaks (MIT, github.com/gitleaks/gitleaks) as a CLI
runtime, accessed through ``subprocess``. No Python dependency on the
gitleaks codebase — we shell out and parse the JSON report.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# Timeout per repo. A scan should complete in seconds for a typical
# project; we cap at 2 minutes so a misbehaving binary cannot stall the
# whole ingestion run.
SCAN_TIMEOUT_SEC = 120


def is_gitleaks_available() -> bool:
    """Return True if the ``gitleaks`` binary is reachable on PATH."""
    return shutil.which("gitleaks") is not None


def _anonymize_file(file_path: str) -> str:
    """Return only the file basename to avoid leaking absolute paths."""
    if not file_path:
        return ""
    return Path(file_path).name[:128]


def _sanitize_finding(raw: dict) -> dict:
    """Strip the matched secret + committer identity from a gitleaks finding."""
    return {
        "rule_id": str(raw.get("RuleID", "unknown"))[:64],
        "description": str(raw.get("Description", ""))[:200],
        "file": _anonymize_file(str(raw.get("File", ""))),
        "line": int(raw.get("StartLine", 0) or 0),
        "commit": str(raw.get("Commit", ""))[:40],
        "date": str(raw.get("Date", ""))[:30],
    }


def scan_repo(
    repo_path: Path,
    *,
    config_path: Path | None = None,
    timeout_sec: int = SCAN_TIMEOUT_SEC,
) -> list[dict]:
    """Run ``gitleaks detect`` on a repo and return sanitised findings.

    Returns an empty list on any failure (binary missing, timeout,
    invalid JSON, non-existent repo, ...). Never raises — the calling
    ingestion path must continue regardless of scan outcomes.

    ``config_path`` may point to a ``.gitleaks.toml`` baseline that the
    user installed locally to mute known false positives.
    """
    if not is_gitleaks_available():
        return []
    if not repo_path.exists() or not repo_path.is_dir():
        return []
    cmd = [
        "gitleaks",
        "detect",
        "--source",
        str(repo_path),
        "--report-format",
        "json",
        "--report-path",
        "/dev/stdout",
        "--no-banner",
        # exit-code 0 even if leaks found: we treat findings as data,
        # not as a CI failure.
        "--exit-code",
        "0",
    ]
    if config_path is not None and config_path.exists():
        cmd.extend(["--config", str(config_path)])
    try:
        result = subprocess.run(  # noqa: S603 - cmd is fully literal + repo_path
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if not result.stdout:
        return []
    try:
        raw_findings = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(raw_findings, list):
        return []
    return [
        _sanitize_finding(f) for f in raw_findings if isinstance(f, dict)
    ]


def is_enabled(privacy_config: dict) -> bool:
    """Whether the user has opted into gitleaks scanning."""
    if not isinstance(privacy_config, dict):
        return False
    return bool(privacy_config.get("gitleaks_scan", False))
