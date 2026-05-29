"""Collector Anthropic Organization Usage API — verite comptable.

Utilise l'Admin API d'Anthropic (disponible depuis fin 2025) pour
recuperer le cost EXACT facture par Anthropic, y compris :
  - Les consommations OAuth (Claude Pro/Max/Teams) qui sont hors API
    key et donc invisibles cote JSONL local
  - Les promos / discounts / tier pricing applicables
  - Les tokens cache_read / cache_write actuels (facturation reelle)

Prerequis :
  - ANTHROPIC_ADMIN_KEY en variable d'environnement (clef sk-ant-admin-*
    creee sur platform.claude.com/settings/admin-keys avec scope lecture).

Endpoint utilise :
  GET https://api.anthropic.com/v1/organizations/usage_report
    ?starting_at=2026-04-01T00:00:00Z
    &ending_at=2026-04-22T00:00:00Z
    &bucket_width=1d
  Headers:
    x-api-key: <admin_key>
    anthropic-version: 2023-06-01

Source emise : "anthropic_usage" (distincte de claude_code qui lit les
JSONL). Permet la reconciliation cote dashboard :
  - claude_code : tokens mesures localement + cost estime pricing.py
  - anthropic_usage : cost facture par Anthropic (verite comptable)
L'ecart entre les 2 valide la precision de l'estimation tokens-based.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ship1000x.core.usage import TokenBreakdown, build_usage_metadata

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


def _stable_event_id(date: str, model: str, workspace_id: str | None) -> str:
    raw = f"anthropic_usage|{date}|{model}|{workspace_id or 'default'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _fetch_usage_report(
    admin_key: str,
    starting_at: str,
    ending_at: str,
    bucket_width: str = "1d",
) -> list[dict[str, Any]]:
    """Query l'endpoint /v1/organizations/usage_report/messages.

    Retourne les `data` du payload (liste de buckets temporels avec results).
    Lance une exception RuntimeError si la requete echoue.

    group_by=model pour avoir le breakdown par modele (sinon results agrege
    tout en 1 ligne avec model=null).
    """
    # urlencode ne gere pas les array params comme group_by[]=model.
    # On construit manuellement pour respecter le format attendu par l'API.
    # Anthropic API : limit max = 31 quand bucket_width=1d (1 bucket par jour).
    # Pour 31 jours de data (fenetre par defaut), c'est exactement ce qu'il faut.
    params = [
        ("starting_at", starting_at),
        ("ending_at", ending_at),
        ("bucket_width", bucket_width),
        ("limit", "31"),
        ("group_by[]", "model"),
        ("group_by[]", "workspace_id"),
    ]
    url = f"{ANTHROPIC_API_BASE}/v1/organizations/usage_report/messages?{urlencode(params)}"
    req = Request(
        url,
        headers={
            "x-api-key": admin_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Anthropic API HTTP {e.code} : {err_body[:300]}"
        ) from e
    except URLError as e:
        raise RuntimeError(f"Anthropic API unreachable : {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Anthropic API invalid JSON : {e}") from e

    return body.get("data", []) or []


def _fetch_cost_report(
    admin_key: str,
    starting_at: str,
    ending_at: str,
) -> list[dict[str, Any]]:
    """Query /v1/organizations/cost_report pour les cost journaliers.

    The Anthropic Admin API returns one bucket per day with one or more
    `results`. Each result carries the daily billed USD amount. As of
    mid-2026 the API returns `amount` as a numeric **string** at the
    result root (e.g. `"amount": "20.394575"`, `"currency": "USD"`).
    Legacy shapes with a nested `amount.value` are still tolerated so
    older snapshots keep parsing.

    Network/HTTP failures are caught here but the error is reported by
    the caller so a misconfigured key or expired token does not silently
    leave billing at 0 across the dashboard.
    """
    params = [
        ("starting_at", starting_at),
        ("ending_at", ending_at),
        ("limit", "31"),
    ]
    url = f"{ANTHROPIC_API_BASE}/v1/organizations/cost_report?{urlencode(params)}"
    req = Request(
        url,
        headers={
            "x-api-key": admin_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(
            f"Anthropic cost_report HTTP {e.code} : {err_body[:300]}"
        ) from e
    except URLError as e:
        raise RuntimeError(f"Anthropic cost_report unreachable : {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Anthropic cost_report invalid JSON : {e}") from e
    return body.get("data", []) or []


def _extract_amount_usd(result: dict[str, Any]) -> float:
    """Return the USD amount carried by one cost_report result.

    Accepts the current numeric-string shape (`"amount": "20.394575"`),
    the legacy nested-object shape (`"amount": {"value": 20.39}`), or
    direct numeric/legacy field fallbacks (`cost_usd`, `amount_usd`).

    Returns 0.0 when none can be parsed. A non-USD currency is treated
    as 0.0 because SHIP's cost contract is dollar-only.
    """
    amount = result.get("amount")
    currency = (result.get("currency") or "USD").upper()
    parsed: float | None = None

    if isinstance(amount, (int, float)):
        parsed = float(amount)
    elif isinstance(amount, str):
        try:
            parsed = float(amount.strip())
        except (ValueError, AttributeError):
            parsed = None
    elif isinstance(amount, dict):
        v = amount.get("value")
        if isinstance(v, (int, float)):
            parsed = float(v)
        elif isinstance(v, str):
            try:
                parsed = float(v.strip())
            except (ValueError, AttributeError):
                parsed = None
        currency = (amount.get("currency") or currency or "USD").upper()

    if parsed is None:
        legacy = result.get("cost_usd")
        if legacy is None:
            legacy = result.get("amount_usd")
        try:
            parsed = float(legacy) if legacy is not None else 0.0
        except (ValueError, TypeError):
            parsed = 0.0

    if currency != "USD":
        # SHIP cost contract is USD only; non-USD silently zeroed is the
        # safer default than misreporting EUR/GBP as dollars.
        return 0.0
    return parsed or 0.0


def collect(
    storage, classifier, privacy_config: dict[str, Any]
) -> dict[str, int]:
    """Ingeste les usage quotidiens Anthropic via l'Admin API.

    Skip silencieusement si ANTHROPIC_ADMIN_KEY absent (Mac Studio sans
    config, ou user preferant le mode tokens-only).
    """
    stats = {
        "files_seen": 0,
        "files_parsed": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
    }

    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip()
    if not admin_key:
        return stats  # Pas de clef, skip

    # Fenetre de query : par defaut 31 derniers jours (assez pour rolling 30j)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=31)
    starting_at = since.strftime("%Y-%m-%dT00:00:00Z")
    ending_at = now.strftime("%Y-%m-%dT00:00:00Z")

    import sys
    try:
        buckets = _fetch_usage_report(admin_key, starting_at, ending_at, "1d")
    except RuntimeError as e:
        print(f"[anthropic_usage] API error : {e}", file=sys.stderr)
        return stats
    # Debug : confirme ce qu'on recoit de l'API
    total_results = sum(len(b.get("results") or []) for b in buckets)
    print(
        f"[anthropic_usage] fetched {len(buckets)} buckets, "
        f"{total_results} results total (since={starting_at}, until={ending_at})",
        file=sys.stderr,
    )

    # Fetch cost_report best-effort but no longer silent: the previous
    # `try/except Exception: pass` was masking a real schema-drift bug
    # (Admin API moved `amount` from a `{value, currency}` object to a
    # plain numeric string) that left every billing snapshot at $0.
    cost_by_date: dict[str, float] = {}
    try:
        cost_data = _fetch_cost_report(admin_key, starting_at, ending_at)
    except RuntimeError as e:
        print(f"[anthropic_usage] cost_report error : {e}", file=sys.stderr)
        cost_data = []
    cost_buckets_with_amount = 0
    for bucket in cost_data:
        day = (bucket.get("starting_at") or "")[:10]
        if not day:
            continue
        for r in bucket.get("results") or []:
            v = _extract_amount_usd(r) if isinstance(r, dict) else 0.0
            if v > 0:
                cost_buckets_with_amount += 1
            cost_by_date[day] = cost_by_date.get(day, 0.0) + v
    print(
        f"[anthropic_usage] cost_report: {len(cost_by_date)} days with cost, "
        f"{cost_buckets_with_amount} non-zero result rows, "
        f"total = ${sum(cost_by_date.values()):.2f}",
        file=sys.stderr,
    )

    for bucket in buckets:
        stats["files_seen"] += 1
        day = (bucket.get("starting_at") or "")[:10]
        if not day:
            continue
        results = bucket.get("results") or []

        # Calcul du total tokens du jour pour prorata cost multi-model
        day_total_tokens = 0
        for r in results:
            day_total_tokens += (
                (r.get("uncached_input_tokens", 0) or 0)
                + (r.get("cache_read_input_tokens", 0) or 0)
                + (r.get("output_tokens", 0) or 0)
            )
            cc = r.get("cache_creation") or {}
            if isinstance(cc, dict):
                day_total_tokens += (cc.get("ephemeral_1h_input_tokens", 0) or 0)
                day_total_tokens += (cc.get("ephemeral_5m_input_tokens", 0) or 0)

        for r in results:
            model = r.get("model") or "all-models"
            workspace_id = r.get("workspace_id")

            uncached_in = r.get("uncached_input_tokens", 0) or 0
            cache_read = r.get("cache_read_input_tokens", 0) or 0
            # cache_creation est un objet {ephemeral_1h_input_tokens, ephemeral_5m_input_tokens}
            cc = r.get("cache_creation") or {}
            cache_write = 0
            if isinstance(cc, dict):
                cache_write = (cc.get("ephemeral_1h_input_tokens", 0) or 0) + (
                    cc.get("ephemeral_5m_input_tokens", 0) or 0
                )
            elif isinstance(cc, int):
                cache_write = cc

            tokens_in = uncached_in + cache_read + cache_write
            tokens_out = r.get("output_tokens", 0) or 0

            # Cost is computed from tokens * pricing.py (the same source
            # of truth used by every other SHIP collector), NOT from the
            # cost_report API. Empirical reverse-check on a known-tokens
            # day (2026-04-27) showed cost_report returns roughly 100x
            # the calculation derived from the published Anthropic per-
            # token rates: 1 044 893 cached_read + 1 716 024 cache_write
            # + 9 383 output + 69 uncached on claude-sonnet-4-6 prices to
            # ~$6.89 with standard Anthropic pricing, while cost_report
            # returned $689.30 for the same day. Whatever unit the API
            # field is actually expressed in, it is not the per-token
            # billed USD the SHIP cost honesty contract expects. Using
            # tokens*pricing keeps anthropic_usage events comparable
            # with claude_code events (same pricing.py, same units) and
            # auditable from the published Anthropic rate card.
            from ship1000x.core.pricing import estimate_anthropic_cost
            cost_usd = estimate_anthropic_cost(
                model=model,
                tokens_input=uncached_in,
                tokens_output=tokens_out,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            cost_quality = "factual"  # Tokens are factual from the API,
            # cost rates are deterministic from pricing.py. The legacy
            # cost_by_date prorata is kept only as a divergence signal.
            row_tokens = uncached_in + cache_read + cache_write + tokens_out
            api_cost_usd = 0.0
            if day in cost_by_date and day_total_tokens > 0:
                api_cost_usd = cost_by_date[day] * (row_tokens / day_total_tokens)

            started_iso = f"{day}T00:00:00+00:00"
            ended_iso = f"{day}T23:59:59+00:00"

            event = {
                "id": _stable_event_id(day, model, workspace_id),
                "source": "anthropic_usage",
                "event_type": "billing_snapshot",
                "started_at": started_iso,
                "ended_at": ended_iso,
                "duration_sec": 0,  # pas de duree, c'est un snapshot billing
                "wall_clock_sec": 0,
                "cwd": None,
                "project_id": "unclassified",
                "project_conf": 0.0,
                "tool_or_action": "anthropic_billing",
                "token_input": int(tokens_in),
                "token_output": int(tokens_out),
                "cost_estimated": float(cost_usd),
                "user_msg_type": None,
                "wordcount": 0,
                "confidence_flag": "high",  # verite comptable
                "raw_meta": json.dumps(
                    {
                        "source_api": "anthropic_admin_usage_report",
                        "workspace_id": workspace_id,
                        "cache_read_tokens": int(cache_read),
                        "cache_write_tokens": int(cache_write),
                        # api_reported_cost preserves what the Anthropic
                        # cost_report endpoint returned for this row (after
                        # token-prorata over the bucket). It is intentionally
                        # NOT used for cost_estimated because empirical tests
                        # show the API value diverges from the per-token
                        # billed cost by roughly 100x in some periods. Kept
                        # here so a future drift fix can compare.
                        "api_reported_cost": round(api_cost_usd, 8),
                        # V7 model_stats: per-model breakdown including cache
                        # so downstream reports do not have to peek at the
                        # top-level cache fields and re-do the attribution.
                        "model_stats": {
                            model: {
                                "tokens_in": int(tokens_in),
                                "tokens_out": int(tokens_out),
                                "cache_read_tokens": int(cache_read),
                                "cache_write_tokens": int(cache_write),
                                "uncached_input_tokens": int(uncached_in),
                                "cost": float(cost_usd),
                                "turns": 0,  # not exposed by the API
                            }
                        },
                        # Provider billing is an aggregate of OAuth + API
                        # key usage on Anthropic's side. From SHIP's
                        # cost-honesty perspective the billed cost is what
                        # the user actually pays via API key today, so we
                        # tag billing snapshots as `api_key` for the
                        # honest-split logic and keep a separate
                        # `billing_source` label for the rich detail.
                        "billing_source": "billing_aggregated",
                        "auth_mode": "api_key",
                        "usage": build_usage_metadata(
                            provider="anthropic",
                            client="anthropic-usage-api",
                            model_raw=model,
                            tokens=TokenBreakdown(
                                input_tokens=int(uncached_in),
                                output_tokens=int(tokens_out),
                                cached_input_tokens=int(cache_read),
                                cache_write_tokens=int(cache_write),
                            ),
                            cost_estimated=float(cost_usd),
                            cost_quality=cost_quality,
                            active_time_quality="unknown",
                            cost_basis="tokens_times_published_anthropic_pricing",
                            token_source="anthropic_admin_usage_report",
                            auth_mode="api_key",
                        ),
                    }
                ),
            }
            storage.upsert_event(event, replace=True)
            stats["events_ingested"] += 1
        stats["files_parsed"] += 1
        stats["sessions_ingested"] += 1

    return stats
