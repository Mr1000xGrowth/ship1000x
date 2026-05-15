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
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

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

    Format attendu :
      {data: [{starting_at, ending_at, results: [{amount: {value, currency}, ...}]}]}
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
    except (HTTPError, URLError, json.JSONDecodeError):
        return []  # cost_report optionnel, on continue sans si indispo
    return body.get("data", []) or []


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

    # Fetch cost_report en best-effort pour enrichir (optionnel)
    # Format : results[].amount.value (USD) + amount.currency
    cost_by_date: dict[str, float] = {}
    try:
        cost_data = _fetch_cost_report(admin_key, starting_at, ending_at)
        for bucket in cost_data:
            day = (bucket.get("starting_at") or "")[:10]
            if not day:
                continue
            for r in bucket.get("results") or []:
                amount = r.get("amount") or {}
                v = amount.get("value") if isinstance(amount, dict) else None
                if v is None:
                    # Fallback anciens schemas
                    v = r.get("cost_usd") or r.get("amount_usd") or 0.0
                cost_by_date[day] = cost_by_date.get(day, 0.0) + float(v or 0.0)
    except Exception:
        pass

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

            # Cost par result : prorata cost_by_date si multi-model
            cost_usd = 0.0
            row_tokens = uncached_in + cache_read + cache_write + tokens_out
            if day in cost_by_date and day_total_tokens > 0:
                cost_usd = cost_by_date[day] * (row_tokens / day_total_tokens)

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
                        # V5 model_stats a 1 entree (ce model precis)
                        "model_stats": {
                            model: {
                                "tokens_in": int(tokens_in),
                                "tokens_out": int(tokens_out),
                                "cost": float(cost_usd),
                                "turns": 0,  # non expose par l'API
                            }
                        },
                        # V6 auth_mode : l'Admin API agrege OAuth + API keys
                        "auth_mode": "billing_aggregated",
                    }
                ),
            }
            storage.upsert_event(event, replace=True)
            stats["events_ingested"] += 1
        stats["files_parsed"] += 1
        stats["sessions_ingested"] += 1

    return stats
