"""Public documentation consistency checks.

These tests keep the open-source surface aligned with the current CLI and cost
truth contract. They intentionally read only repository docs and metadata.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DOCS = (
    "README.md",
    "docs/assets/calibrate.txt",
    "docs/assets/highlights.txt",
    "docs/COLLECTORS.md",
    "docs/COVERAGE.md",
    "docs/DASHBOARD_API.md",
    "docs/METHODOLOGY.md",
    "docs/PRIVACY.md",
    "docs/QUICKSTART.md",
    "docs/TRUST_SCORE.md",
)
PUBLIC_PACKAGE_FILES = tuple(
    path.relative_to(REPO_ROOT).as_posix()
    for path in (REPO_ROOT / "ship1000x").rglob("*.py")
)
LEGACY_TRACKER_COMMAND_RE = re.compile(
    r"`?tracker "
    r"(?:audit|calibrate|check-shell-config|daily|doctor|drop|explain|highlights|ingest|"
    r"init|insights|install-scheduler|projects|push|rollup|setup|today)"
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _project_version() -> str:
    match = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.MULTILINE)
    assert match is not None
    return match.group(1)


def _project_classifiers() -> list[str]:
    pyproject = _read("pyproject.toml")
    match = re.search(r"classifiers = \[(.*?)\]", pyproject, re.DOTALL)
    assert match is not None
    return re.findall(r'"([^"]+)"', match.group(1))


def test_readme_status_badge_matches_project_version():
    readme = _read("README.md")
    version = _project_version()

    assert f"Beta v{version}" in readme
    assert f"beta%20v{version}" in readme
    assert f"In beta (`v{version}`)" in readme
    assert "Beta v0.5.0" not in readme
    assert "In beta (`v0.5.0`)" not in readme


def test_package_classifier_matches_public_beta_status():
    classifiers = _project_classifiers()

    assert "Development Status :: 4 - Beta" in classifiers
    assert "Development Status :: 3 - Alpha" not in classifiers


def test_quickstart_uses_current_cli_and_paths():
    quickstart = _read("docs/QUICKSTART.md")

    assert "ship1000x init" in quickstart
    assert "ship1000x public-check --strict-history" in quickstart
    assert "ship1000x observation-audit --since 30d" in quickstart
    assert "public_proof_sequence" in quickstart
    assert "proof_status" in quickstart
    assert "The older `passed`" in quickstart
    assert "boolean is retained for exit-code compatibility" in quickstart
    assert "python tracker.py" not in quickstart
    assert "tracker today" not in quickstart
    assert "pip install -r requirements.txt" not in quickstart
    assert "~/ship1000x/db/tracker.sqlite" not in quickstart


def test_readme_public_proof_sequence_matches_public_check_contract():
    readme = _read("README.md")

    assert "public_claim_readiness" in readme
    assert "public_proof_sequence" in readme
    assert "PASS`, `CAVEAT`, or `NO GO`" in readme
    assert "proof_status" in readme
    assert "legacy `passed` boolean" in readme
    assert "ship1000x source-audit --strict-public --since 30d" in readme
    assert "ship1000x observation-audit --since 30d" in readme
    assert "ship1000x history-audit --since 30d" in readme
    assert "ship1000x reclassify --since 30d --dry-run" in readme
    assert "ship1000x public-check --strict-history --since 30d" in readme


def test_privacy_doc_uses_current_cli_paths_and_api_boundaries():
    privacy = _read("docs/PRIVACY.md")

    assert "~/.local/share/ship1000x/tracker.sqlite" in privacy
    assert "ship1000x push-insights --dry-run --since 7d" in privacy
    assert "Manual web exports are different" in privacy
    assert "`web_exports` reads local files" in privacy
    assert "~/ship1000x/db/tracker.sqlite" not in privacy
    assert "tracker push insights" not in privacy
    assert "The 4 collectors that touch external APIs" not in privacy


def test_architecture_does_not_claim_billed_estimated_is_invoice_truth():
    architecture = _read("docs/ARCHITECTURE.md")

    assert "cost.billed_estimated_usd" in architecture
    assert "coût réellement facturé" not in architecture
    assert "estimation locale" in architecture
    assert "invoice-grade truth" in architecture
    assert "~/.claude/plans" not in architecture


def test_scheduler_docs_use_public_placeholders_not_maintainer_paths():
    cadence = _read("docs/SHIP_LOCAL_CADENCE.md")

    assert "/Users/charles" not in cadence
    assert "/Users/YOUR_USER/path/to/ship1000x" in cadence
    assert "/Users/YOUR_USER/Library/Logs" in cadence


def test_source_quality_documents_dashboard_cost_truth_fields():
    source_quality = _read("docs/SOURCE_QUALITY.md")

    assert "/api/highlights" in source_quality
    assert "/api/projects" in source_quality
    assert "DASHBOARD_API.md" in source_quality
    assert "ship1000x.dashboard.highlights.v1" in source_quality
    assert "ship1000x.dashboard.project.v1" in source_quality
    assert "native_token_pricing_usd" in source_quality
    assert "events_with_unknown_billing_basis" in source_quality
    assert "cost_total_basis" in source_quality
    assert "total_cost_basis" in source_quality
    assert "api_equivalent_legacy_alias" in source_quality


def test_dashboard_api_contract_documents_cost_truth_and_schema_versions():
    dashboard_api = _read("docs/DASHBOARD_API.md")
    readme = _read("README.md")

    assert "ship1000x.dashboard.highlights.v1" in dashboard_api
    assert "ship1000x.dashboard.project.v1" in dashboard_api
    assert "ship1000x.dashboard.trend_point.v1" in dashboard_api
    assert "ship1000x.dashboard.trust.v1" in dashboard_api
    assert "ship1000x.dashboard.trust_source.v1" in dashboard_api
    assert "ship1000x.source_quality_report.v1" in dashboard_api
    assert "cost_truth" in dashboard_api
    assert "presentation_status" in dashboard_api
    assert "presentation_label" in dashboard_api
    assert "not invoice" in dashboard_api
    assert "API-equivalent" in dashboard_api
    assert "unknown_billing_basis_usd" in dashboard_api
    assert "docs/DASHBOARD_API.md" in readme


def test_readme_highlights_do_not_reintroduce_trust_score_bonus_cap():
    public_text = "\n".join(
        _read(path)
        for path in (
            "README.md",
            "docs/assets/highlights.txt",
            "docs/COLLECTORS.md",
            "docs/TRUST_SCORE.md",
        )
    )

    assert "+8 bonuses → 100/100" not in public_text
    assert "+8 bonuses" not in public_text
    assert "base (Factual)" not in public_text
    assert "global composite with bonuses" not in public_text
    assert "Global composite score" not in public_text
    assert "bonus(cadence_calibrated)" not in public_text
    assert "Cost / ligne nette" not in public_text
    assert "Cost vs Anthropic invoice" not in public_text
    assert "Coût API-equivalent          $5 396      [93% Factual" not in public_text
    assert "API-equivalent cost          $1,247          ██████████  Factual" not in public_text
    assert "weighted avg per source" in public_text
    assert "reported separately, never added" in public_text
    assert "robustness check" in public_text
    assert "API-equivalent cost" in public_text
    assert "Coût agentique" not in public_text
    assert "Coût API-equivalent" in public_text
    assert "native token/pricing" in public_text
    assert "rest heuristic" not in public_text
    assert "remainder indicative/unknown" in public_text
    assert "$0.0082   ultra-efficient" not in public_text
    assert "$0.0089   total API-eq / net line" in public_text
    assert "Factual / Indicative / Estimative" not in public_text
    assert "Factual / Defensible / Indicative / Unknown" in public_text


def test_public_docs_do_not_overclaim_invoice_or_official_cost_truth():
    public_text = "\n".join(
        _read(path)
        for path in (
            "docs/COVERAGE.md",
            "docs/COLLECTORS.md",
            "docs/METHODOLOGY.md",
            "docs/RECONCILE.md",
            "docs/SOURCE_QUALITY.md",
            "docs/TRUST_SCORE.md",
        )
    )

    forbidden = (
        "official invoice validation",
        "official invoice via",
        "official Anthropic invoice",
        "source of truth = published official",
        "Factual 100%",
        "what the provider actually billed",
        "closest local-readable approximation of the\ntruth",
        "reconcile with official invoice",
        "official validation",
        "cost actually\n  billed",
        "source of truth that should feed dashboard improvements",
    )
    for phrase in forbidden:
        assert phrase not in public_text
    assert "API-equivalent" in public_text
    assert "billing-side" in public_text
    assert "missing_or_unknown_auth_mode_events" in public_text
    assert "nonstandard_auth_modes" in public_text
    assert "provider_policy_snapshots=X/Y" in public_text
    assert "invalid_provider_policy_snapshots" in public_text
    assert "missing_provider_policy_snapshots" in public_text
    assert "missing_provider_policy_snapshot_events" in public_text
    assert "provider_policy_snapshot_coverage_pct" in public_text
    assert "strict_history_blocking" in public_text
    assert "strict_history_failures" in public_text
    assert "weak local route evidence" in public_text


def test_cli_operator_labels_do_not_use_generic_cost_wording():
    cli = (REPO_ROOT / "ship1000x" / "cli.py").read_text(encoding="utf-8")
    exporter = (REPO_ROOT / "ship1000x" / "exporters" / "markdown_report.py").read_text(
        encoding="utf-8"
    )

    assert "Cout $" not in cli
    assert "Cost $" not in cli
    assert "Cout IA" not in cli
    assert "Cout ($)" not in cli
    assert "Cost in window" not in cli
    assert "Cout $" not in exporter
    assert "Cout estime" not in exporter


def test_public_docs_do_not_use_legacy_tracker_commands():
    offenders: list[str] = []
    for path in PUBLIC_DOCS:
        text = _read(path)
        for match in LEGACY_TRACKER_COMMAND_RE.finditer(text):
            offenders.append(f"{path}: {match.group(0)}")

    assert offenders == []


def test_public_package_messages_do_not_use_legacy_tracker_commands():
    offenders: list[str] = []
    for path in PUBLIC_PACKAGE_FILES:
        text = _read(path)
        for match in LEGACY_TRACKER_COMMAND_RE.finditer(text):
            offenders.append(f"{path}: {match.group(0)}")

    assert offenders == []
