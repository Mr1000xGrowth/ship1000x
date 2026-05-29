"""Shared cost-truth split helpers.

The same stored event cost can mean different things depending on auth route.
This module keeps dashboard and audit logic from silently drifting apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

UnknownCostStrategy = Literal["separate", "include_in_api_equivalent"]


@dataclass(frozen=True)
class EventCostTruth:
    api_equivalent_usd: float
    billed_estimated_usd: float
    subscription_absorbed_usd: float
    unknown_billing_basis_usd: float
    events_with_cost_truth: int
    events_with_unknown_billing_basis: int
    auth_mode: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def safe_raw_meta(raw_meta: Any) -> dict[str, Any]:
    """Parse raw metadata without surfacing or trusting raw content."""
    if isinstance(raw_meta, dict):
        return raw_meta
    if not raw_meta:
        return {}
    try:
        parsed = json.loads(str(raw_meta))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def api_equivalent_cost_from_row(row: Any) -> float:
    """Return operator-facing API-equivalent cost for a DB row-like object."""
    return event_cost_truth(
        stored_cost=float(
            _row_value(row, "cost_estimated", _row_value(row, "cost", 0.0)) or 0.0
        ),
        meta=safe_raw_meta(_row_value(row, "raw_meta")),
        unknown_strategy="include_in_api_equivalent",
    ).api_equivalent_usd


def event_cost_truth(
    *,
    stored_cost: float,
    meta: dict[str, Any],
    unknown_strategy: UnknownCostStrategy = "separate",
) -> EventCostTruth:
    """Classify one event cost without exposing raw metadata.

    ``unknown_strategy`` preserves existing public surfaces:

    - ``separate`` keeps legacy/unknown-route cost only in
      ``unknown_billing_basis_usd`` (source-audit split semantics).
    - ``include_in_api_equivalent`` also includes that cost in the
      API-equivalent total (dashboard total semantics).
    """
    usage = _dict(meta.get("usage"))
    cost_block = _dict(usage.get("cost"))
    auth_mode = (
        usage.get("auth_mode")
        or cost_block.get("auth_mode")
        or meta.get("auth_mode")
        or "unknown"
    )
    normalized_auth_mode = str(auth_mode).strip().lower()
    if normalized_auth_mode not in {"api_key", "oauth", "unknown"}:
        normalized_auth_mode = "unknown"

    cost_value = float(stored_cost or 0.0)
    explicit_api = _number(cost_block.get("api_equivalent_usd"))
    legacy_estimate = _number(cost_block.get("estimated_usd"))
    explicit_billed = _number(cost_block.get("billed_estimated_usd"))

    event_api_equivalent = (
        explicit_api
        if explicit_api is not None
        else legacy_estimate
        if legacy_estimate is not None
        else cost_value
    )
    api_from_metadata = explicit_api is not None or legacy_estimate is not None
    has_cost_truth = api_from_metadata or explicit_billed is not None or normalized_auth_mode != "unknown"

    if explicit_billed is not None:
        billed_estimated = explicit_billed
    elif normalized_auth_mode == "api_key":
        billed_estimated = event_api_equivalent
    else:
        billed_estimated = 0.0

    unknown_billing_basis = 0.0
    unknown_events = 0
    api_equivalent = event_api_equivalent
    if normalized_auth_mode == "unknown" and explicit_billed is None:
        unknown_billing_basis = max(0.0, event_api_equivalent)
        unknown_events = 1 if unknown_billing_basis > 0 else 0
        if unknown_strategy == "separate" and not api_from_metadata:
            api_equivalent = 0.0

    subscription_absorbed = 0.0
    if normalized_auth_mode == "oauth" or (
        explicit_billed is not None and billed_estimated < event_api_equivalent
    ):
        subscription_absorbed = max(0.0, event_api_equivalent - billed_estimated)

    return EventCostTruth(
        api_equivalent_usd=api_equivalent,
        billed_estimated_usd=billed_estimated,
        subscription_absorbed_usd=subscription_absorbed,
        unknown_billing_basis_usd=unknown_billing_basis,
        events_with_cost_truth=1 if has_cost_truth else 0,
        events_with_unknown_billing_basis=unknown_events,
        auth_mode=normalized_auth_mode,
    )
