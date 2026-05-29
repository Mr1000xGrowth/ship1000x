from __future__ import annotations

from ship1000x.core.cost_truth import event_cost_truth


def test_event_cost_truth_splits_api_key_cost_as_billed_estimated():
    truth = event_cost_truth(
        stored_cost=2.5,
        meta={"usage": {"auth_mode": "api_key"}},
    )

    assert truth.api_equivalent_usd == 2.5
    assert truth.billed_estimated_usd == 2.5
    assert truth.subscription_absorbed_usd == 0.0
    assert truth.unknown_billing_basis_usd == 0.0
    assert truth.events_with_cost_truth == 1
    assert truth.events_with_unknown_billing_basis == 0


def test_event_cost_truth_splits_oauth_cost_as_subscription_absorbed():
    truth = event_cost_truth(
        stored_cost=7.0,
        meta={"usage": {"auth_mode": "oauth"}},
    )

    assert truth.api_equivalent_usd == 7.0
    assert truth.billed_estimated_usd == 0.0
    assert truth.subscription_absorbed_usd == 7.0
    assert truth.unknown_billing_basis_usd == 0.0


def test_event_cost_truth_keeps_unknown_cost_separate_for_source_audit():
    truth = event_cost_truth(stored_cost=4.0, meta={})

    assert truth.api_equivalent_usd == 0.0
    assert truth.billed_estimated_usd == 0.0
    assert truth.subscription_absorbed_usd == 0.0
    assert truth.unknown_billing_basis_usd == 4.0
    assert truth.events_with_cost_truth == 0
    assert truth.events_with_unknown_billing_basis == 1


def test_event_cost_truth_keeps_explicit_api_estimate_visible_when_route_unknown():
    truth = event_cost_truth(
        stored_cost=0.0,
        meta={"usage": {"cost": {"estimated_usd": 4.0}}},
    )

    assert truth.api_equivalent_usd == 4.0
    assert truth.billed_estimated_usd == 0.0
    assert truth.unknown_billing_basis_usd == 4.0
    assert truth.events_with_cost_truth == 1
    assert truth.events_with_unknown_billing_basis == 1


def test_event_cost_truth_can_include_unknown_cost_in_dashboard_api_total():
    truth = event_cost_truth(
        stored_cost=4.0,
        meta={},
        unknown_strategy="include_in_api_equivalent",
    )

    assert truth.api_equivalent_usd == 4.0
    assert truth.unknown_billing_basis_usd == 4.0
    assert truth.events_with_unknown_billing_basis == 1


def test_event_cost_truth_prefers_explicit_cost_block_values():
    truth = event_cost_truth(
        stored_cost=99.0,
        meta={
            "usage": {
                "auth_mode": "oauth",
                "cost": {
                    "api_equivalent_usd": 10.0,
                    "billed_estimated_usd": 3.0,
                },
            }
        },
    )

    assert truth.api_equivalent_usd == 10.0
    assert truth.billed_estimated_usd == 3.0
    assert truth.subscription_absorbed_usd == 7.0
    assert truth.unknown_billing_basis_usd == 0.0
