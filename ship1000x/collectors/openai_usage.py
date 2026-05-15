"""Collector OpenAI Organization Usage API — verite comptable.

Utilise l'Usage API d'OpenAI (2025+) pour recuperer les tokens / cost
agreges au niveau organisation. Couvre Codex, GPT-5, GPT-4o, o1, o3.

Prerequis :
  - OPENAI_ADMIN_KEY en variable d'environnement (sk-admin-* creee sur
    platform.openai.com/settings/organization/admin-keys).

Endpoints utilises :
  GET https://api.openai.com/v1/organization/costs
    ?start_time=1712102400 (epoch sec)
    &end_time=1714694400
    &bucket_width=1d
  GET https://api.openai.com/v1/organization/usage/completions
    (idem, plus granulaire par modele si besoin)
  Headers:
    Authorization: Bearer <admin_key>

Comme anthropic_usage : source="openai_usage" distincte des collecteurs
codex* pour permettre la reconciliation tokens estimes vs cost facture.
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

OPENAI_API_BASE = "https://api.openai.com"


def _stable_event_id(date: str, model: str) -> str:
    raw = f"openai_usage|{date}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _http_get(url: str, admin_key: str) -> dict[str, Any] | None:
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        import sys
        err = e.read().decode("utf-8", errors="replace")[:300]
        print(f"[openai_usage] HTTP {e.code} on {url[:80]}... : {err}", file=sys.stderr)
        return None
    except (URLError, json.JSONDecodeError) as e:
        import sys
        print(f"[openai_usage] fetch error : {e}", file=sys.stderr)
        return None


def _fetch_costs(
    admin_key: str,
    start_time: int,
    end_time: int,
) -> list[dict[str, Any]]:
    """Query /v1/organization/costs — cost journalier agrege."""
    params = {
        "start_time": str(start_time),
        "end_time": str(end_time),
        "bucket_width": "1d",
        "limit": "31",
    }
    url = f"{OPENAI_API_BASE}/v1/organization/costs?{urlencode(params)}"
    body = _http_get(url, admin_key)
    return (body or {}).get("data", []) or []


def _fetch_completions_usage(
    admin_key: str,
    start_time: int,
    end_time: int,
) -> list[dict[str, Any]]:
    """Query /v1/organization/usage/completions — tokens par modele."""
    params = {
        "start_time": str(start_time),
        "end_time": str(end_time),
        "bucket_width": "1d",
        "group_by": "model",
        "limit": "31",
    }
    url = f"{OPENAI_API_BASE}/v1/organization/usage/completions?{urlencode(params)}"
    body = _http_get(url, admin_key)
    return (body or {}).get("data", []) or []


def collect(
    storage, classifier, privacy_config: dict[str, Any]
) -> dict[str, int]:
    """Ingeste les usage quotidiens OpenAI via l'Admin Usage API.

    Skip silencieux si OPENAI_ADMIN_KEY absent.
    """
    stats = {
        "files_seen": 0,
        "files_parsed": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
    }

    admin_key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not admin_key:
        return stats

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=31)
    start_time = int(since.timestamp())
    end_time = int(now.timestamp())

    import sys
    # 1. Usage par modele (tokens detailles)
    usage_buckets = _fetch_completions_usage(admin_key, start_time, end_time)
    # 2. Cost journalier (agrege, pour reconcilier)
    cost_buckets = _fetch_costs(admin_key, start_time, end_time)
    print(
        f"[openai_usage] fetched {len(usage_buckets)} usage buckets, "
        f"{len(cost_buckets)} cost buckets",
        file=sys.stderr,
    )

    # Index cost par date (sum cross-projets)
    cost_by_date: dict[str, float] = {}
    for bucket in cost_buckets:
        day = datetime.fromtimestamp(
            bucket.get("start_time", 0), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        for r in bucket.get("results") or []:
            amount = r.get("amount", {}) or {}
            v = amount.get("value", 0.0) or 0.0
            cost_by_date[day] = cost_by_date.get(day, 0.0) + float(v)

    if not usage_buckets and not cost_buckets:
        return stats  # Ni usage ni cost → endpoint peut-etre pas accessible

    for bucket in usage_buckets:
        stats["files_seen"] += 1
        day = datetime.fromtimestamp(
            bucket.get("start_time", 0), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        results = bucket.get("results") or []

        # Calcul du ratio cost / tokens pour prorata multi-modele
        day_total_tokens = sum(
            (r.get("input_tokens", 0) or 0) + (r.get("output_tokens", 0) or 0)
            for r in results
        )
        day_cost = cost_by_date.get(day, 0.0)

        for r in results:
            model = r.get("model") or "unknown"
            tokens_in = r.get("input_tokens", 0) or 0
            tokens_out = r.get("output_tokens", 0) or 0
            cached_in = r.get("input_cached_tokens", 0) or 0
            total_tokens = tokens_in + tokens_out

            # Prorata du cost journalier selon les tokens de ce modele
            cost_usd = 0.0
            if day_total_tokens > 0 and day_cost > 0:
                cost_usd = day_cost * (total_tokens / day_total_tokens)

            started_iso = f"{day}T00:00:00+00:00"
            ended_iso = f"{day}T23:59:59+00:00"

            event = {
                "id": _stable_event_id(day, model),
                "source": "openai_usage",
                "event_type": "billing_snapshot",
                "started_at": started_iso,
                "ended_at": ended_iso,
                "duration_sec": 0,
                "wall_clock_sec": 0,
                "cwd": None,
                "project_id": "unclassified",
                "project_conf": 0.0,
                "tool_or_action": "openai_billing",
                "token_input": int(tokens_in),
                "token_output": int(tokens_out),
                "cost_estimated": float(cost_usd),
                "user_msg_type": None,
                "wordcount": 0,
                "confidence_flag": "high",
                "raw_meta": json.dumps(
                    {
                        "source_api": "openai_admin_usage_completions",
                        "cached_input_tokens": int(cached_in),
                        "model_stats": {
                            model: {
                                "tokens_in": int(tokens_in),
                                "tokens_out": int(tokens_out),
                                "cost": float(cost_usd),
                                "turns": r.get("num_model_requests", 0) or 0,
                            }
                        },
                        "auth_mode": "billing_aggregated",
                    }
                ),
            }
            storage.upsert_event(event, replace=True)
            stats["events_ingested"] += 1
        stats["files_parsed"] += 1
        stats["sessions_ingested"] += 1

    return stats
