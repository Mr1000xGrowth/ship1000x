"""Observabilité coût + tokens par (jour, machine, source, modèle).

Agrège le superset ``usage_breakdown`` (tokens par type) + le coût
API-équivalent + la qualité de tarif dans la table ``daily_model_usage``.
Table LONGUE et agnostique : un nouveau modèle = de nouvelles lignes.

Régénéré par ``ship1000x rollup`` (idempotent, purge + recompute la fenêtre).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ship1000x.core.pricing import PRICING_VERSION, resolve_model_pricing
from ship1000x.core.storage import Storage
from ship1000x.core.usage import canonicalize_model

# Champs tokens agrégés (sous-ensemble numérique du superset).
_TOKEN_FIELDS = (
    "fresh_input", "cache_read", "cache_write_5m", "cache_write_1h",
    "output_tokens", "thinking", "reasoning", "web_search_requests",
)


def _infer_provider(source: str, model: str, usage: dict) -> str:
    prov = usage.get("provider")
    if prov:
        return prov
    m = (model or "").lower()
    if "codex" in source or m.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return "anthropic"


def _pricing_quality(provider: str, model: str) -> str:
    """exact si le modèle est tarifé explicitement, sinon fallback."""
    try:
        p = resolve_model_pricing(provider, model)
    except Exception:
        return "fallback"
    if getattr(p, "unknown_model", False):
        return "fallback"
    return "exact" if getattr(p, "match_quality", "") == "exact" else "fallback"


def rebuild_model_usage(storage: Storage, since: datetime | None = None) -> dict[str, int]:
    """(Re)calcule daily_model_usage pour la fenêtre depuis ``since``.

    Purge + recompute (idempotent). Une ligne par (date, machine, source, modèle).
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=180)
    since_date = since.date().isoformat()

    with storage.conn() as conn:
        rows = conn.execute(
            """
            SELECT date(started_at) AS day, machine_id, source,
                   cost_estimated, raw_meta
            FROM events
            WHERE source != 'git' AND started_at IS NOT NULL
              AND date(started_at) >= ?
            """,
            (since_date,),
        ).fetchall()

    # clé = (day, machine_id, source, model)
    agg: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        meta = r["raw_meta"]
        if not meta:
            continue
        try:
            m = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            continue
        ub = m.get("usage_breakdown")
        if not isinstance(ub, dict):
            continue
        usage = m.get("usage") or {}
        # Re-dériver le canonical depuis le modèle PRÉCIS (turn_context.model
        # côté Codex, model côté Claude) plutôt que de faire confiance au
        # `model_canonical` figé à l'ingest : ce dernier rabattait gpt-5.4 /
        # gpt-5.3-codex / claude-opus-4-8 sur des buckets génériques plus
        # anciens (gpt-5 / claude-opus-4). On re-canonicalise ici pour que les
        # versions réellement détectées apparaissent en lignes distinctes.
        raw_model = m.get("model") or usage.get("model_canonical")
        model = canonicalize_model(raw_model)
        provider = _infer_provider(r["source"], model, usage)
        key = (r["day"], r["machine_id"] or "unknown-machine", r["source"], model)
        a = agg.get(key)
        if a is None:
            a = {f: 0 for f in _TOKEN_FIELDS}
            a.update({
                "provider": provider,
                "cost_api_equivalent": 0.0,
                "cost_billed": 0.0,
                "auth_modes": {},
            })
            agg[key] = a
        for f in _TOKEN_FIELDS:
            a[f] += int(ub.get(f, 0) or 0)
        a["cost_api_equivalent"] += float(r["cost_estimated"] or 0.0)
        cost = usage.get("cost") or {}
        a["cost_billed"] += float(cost.get("billed_estimated_usd", 0.0) or 0.0)
        am = usage.get("auth_mode") or "unknown"
        a["auth_modes"][am] = a["auth_modes"].get(am, 0) + 1

    now = datetime.now(timezone.utc).isoformat()
    with storage.conn() as conn:
        conn.execute("DELETE FROM daily_model_usage WHERE date >= ?", (since_date,))
        for (day, machine_id, source, model), a in agg.items():
            provider = a["provider"]
            auth_dominant = max(a["auth_modes"].items(), key=lambda kv: kv[1])[0] if a["auth_modes"] else "unknown"
            conn.execute(
                """
                INSERT INTO daily_model_usage (
                    date, machine_id, source, provider, model,
                    fresh_input, cache_read, cache_write_5m, cache_write_1h,
                    output_tokens, thinking, reasoning, web_search_requests,
                    cost_api_equivalent, cost_billed, auth_mode,
                    pricing_quality, pricing_version, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day, machine_id, source, provider, model,
                    a["fresh_input"], a["cache_read"], a["cache_write_5m"],
                    a["cache_write_1h"], a["output_tokens"], a["thinking"],
                    a["reasoning"], a["web_search_requests"],
                    round(a["cost_api_equivalent"], 6), round(a["cost_billed"], 6),
                    auth_dominant, _pricing_quality(provider, model),
                    PRICING_VERSION, now,
                ),
            )

    days = len({k[0] for k in agg})
    return {"model_rows": len(agg), "days": days}
