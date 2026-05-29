"""Regression tests for the share_config filter in insights_push.

Investigation outcome (Wave 1 / J4-J5): the existing
``filter_payload_by_share_config`` is already fail-safe — when
``share_config`` is None or an empty dict, the conservative
``_SHARE_DEFAULTS`` apply (email hashed, financials stripped,
project_ids hashed). This suite locks that contract so a future
refactor cannot accidentally relax the defaults.

We do NOT test the S3 push itself (boto3 mocking is heavy and the
push path is exercised by integration tests on the maintainer's
machine). The dangerous surface is the filter — every byte that
leaves the local machine goes through ``filter_payload_by_share_config``
first.
"""

from __future__ import annotations

import json

from ship1000x.exporters.insights_push import (
    _FINANCIAL_KEYS,
    _SHARE_DEFAULTS,
    filter_payload_by_share_config,
)


def _payload_with_pii() -> dict:
    """A realistic payload that contains every dangerous field shape
    the filter must scrub or hash by default."""
    return {
        "_meta": {
            "user_email": "alice@example.com",
            "machine_id": "macbook-alice-001",
        },
        "global": {
            "multiplier": {
                "tjm_eur_per_day": 1000,
                "value_produit_eur": 50000,
                "equivalent_value_eur": 25000,
                "ratio_value_per_cost": 2.5,
                "value_eur": 30000,
                "cost_eur": 12000,
                # Non-financial field — must be preserved.
                "hours_active": 120,
            },
            "signals": {"focus_score": 0.8},
            "profile": {"cadence_class": "burst"},
        },
        "by_project": {
            "client-acme-secret-2026": {
                "multiplier": {"tjm_eur_per_day": 1500, "hours_active": 40},
                "signals": {"focus_score": 0.9},
            },
            "unclassified": {
                "multiplier": {"hours_active": 5},
            },
        },
    }


class TestDefaultsAreConservative:
    """When the caller passes ``share_config=None`` or ``{}``, the
    defaults must hide all PII / financials."""

    def test_share_defaults_hide_pii(self):
        """Documents the contract: every dangerous flag defaults to False/aggregate."""
        assert _SHARE_DEFAULTS["share_email"] is False
        assert _SHARE_DEFAULTS["share_financials"] is False
        assert _SHARE_DEFAULTS["share_projects"] == "aggregate"

    def test_none_config_hashes_email(self):
        result = filter_payload_by_share_config(_payload_with_pii(), None)
        email = result["_meta"]["user_email"]
        assert email.startswith("hashed:"), email
        assert "alice@example.com" not in json.dumps(result)

    def test_empty_dict_config_hashes_email(self):
        """Empty dict must behave identically to None (regression : a
        future refactor that confuses None vs {} would be a leak)."""
        result_none = filter_payload_by_share_config(_payload_with_pii(), None)
        result_empty = filter_payload_by_share_config(_payload_with_pii(), {})
        assert result_none == result_empty

    def test_none_config_strips_all_financial_keys(self):
        result = filter_payload_by_share_config(_payload_with_pii(), None)
        global_mult = result["global"]["multiplier"]
        for k in _FINANCIAL_KEYS:
            assert k not in global_mult, (
                f"financial key '{k}' leaked under default share_config"
            )
        # Non-financial field preserved.
        assert global_mult.get("hours_active") == 120

    def test_none_config_strips_financial_keys_per_project(self):
        result = filter_payload_by_share_config(_payload_with_pii(), None)
        for proj_data in result.get("by_project", {}).values():
            mult = proj_data.get("multiplier", {})
            for k in _FINANCIAL_KEYS:
                assert k not in mult

    def test_none_config_hashes_project_ids_except_unclassified(self):
        result = filter_payload_by_share_config(_payload_with_pii(), None)
        by_project = result.get("by_project", {})
        # The original sensitive project id must be gone.
        assert "client-acme-secret-2026" not in by_project
        # Hashed bucket is present
        assert any(k.startswith("proj-") for k in by_project)
        # Special-case "unclassified" is preserved as-is for analytics.
        assert "unclassified" in by_project

    def test_none_config_records_applied_filter_for_audit(self):
        """The applied share_config must be recorded in the payload's
        _meta so a server-side audit can verify what filter was used."""
        result = filter_payload_by_share_config(_payload_with_pii(), None)
        applied = result["_meta"].get("share_config_applied")
        assert applied is not None
        assert applied == _SHARE_DEFAULTS


class TestExplicitOptIn:
    """An explicit share_config must be honoured exactly — neither
    silently overridden nor partially merged with defaults in
    unexpected ways."""

    def test_explicit_full_projects_keeps_real_ids(self):
        cfg = {**_SHARE_DEFAULTS, "share_projects": "full"}
        result = filter_payload_by_share_config(_payload_with_pii(), cfg)
        assert "client-acme-secret-2026" in result["by_project"]

    def test_explicit_none_drops_by_project_entirely(self):
        cfg = {**_SHARE_DEFAULTS, "share_projects": "none"}
        result = filter_payload_by_share_config(_payload_with_pii(), cfg)
        assert "by_project" not in result

    def test_explicit_share_email_preserves_clear_email(self):
        cfg = {**_SHARE_DEFAULTS, "share_email": True}
        result = filter_payload_by_share_config(_payload_with_pii(), cfg)
        assert result["_meta"]["user_email"] == "alice@example.com"

    def test_explicit_share_financials_preserves_eur_amounts(self):
        cfg = {**_SHARE_DEFAULTS, "share_financials": True}
        result = filter_payload_by_share_config(_payload_with_pii(), cfg)
        assert result["global"]["multiplier"]["tjm_eur_per_day"] == 1000


class TestFilterIsSingleApplication:
    """Documenting that ``filter_payload_by_share_config`` is meant
    to be applied **exactly once** per payload, not iteratively.

    Re-running it on an already-filtered payload re-hashes the already
    hashed values (you get ``hashed:<hash of "hashed:abc">``), which is
    safe but not idempotent. The contract is "filter once before push".
    The insights_push module enforces this by calling the filter only
    inside the push function itself, never in nested helpers.
    """

    def test_double_filter_rehashes_email(self):
        once = filter_payload_by_share_config(_payload_with_pii(), None)
        twice = filter_payload_by_share_config(once, None)
        # Both stay safely hashed; only the hash value differs.
        assert once["_meta"]["user_email"].startswith("hashed:")
        assert twice["_meta"]["user_email"].startswith("hashed:")
        # The cleartext email never reappears.
        assert "alice@example.com" not in json.dumps(twice)
