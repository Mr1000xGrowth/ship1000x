"""Push des insights precalcules vers Garage S3 (advanced — optional).

Format : `s3://<bucket>/insights/<YYYY-MM>/<user-slug>.json`

Contenu : overview + multiplier + signals + profile pour le mois.
A dashboard you control reads this JSON read-only and displays directly
(no server-side recompute needed).

Privacy : meme principe que les rollups. Uniquement des metriques agregees,
aucun contenu. Couvert par le consent global.

Filtre share_config (defaut conservateur) :
- email hashe sauf opt-in
- financiers (tjm/value_produit) supprimes sauf opt-in
- project_ids hashes par defaut (mode "aggregate")
Configuration via privacy.yaml :
    cloud:
      bucket: ...
      share_config:
        share_email: false       # defaut : hash
        share_machine_id: true   # defaut : clair (necessaire multi-Mac)
        share_financials: false  # defaut : aucun chiffre euro
        share_projects: aggregate # "full" | "aggregate" | "none"
        share_signals: true
        share_profile: true
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from ship1000x.insights.engine import Window, compute_overview, get_active_sec_by_project
from ship1000x.insights.multiplier import compute_multiplier
from ship1000x.insights.profile import compute_profile
from ship1000x.insights.signals import compute_all_signals


_SHARE_DEFAULTS: dict[str, Any] = {
    "share_email": False,
    "share_machine_id": True,
    "share_financials": False,
    "share_projects": "aggregate",  # full | aggregate | none
    "share_signals": True,
    "share_profile": True,
}

_FINANCIAL_KEYS = (
    "tjm_eur_per_day", "value_produit_eur",
    "equivalent_value_eur", "ratio_value_per_cost",
    "value_eur", "cost_eur",
)


def _hash_short(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def filter_payload_by_share_config(
    payload: dict[str, Any],
    share_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Applique les regles de partage au payload avant push.

    Defaut conservateur (None ou config vide) : aucune PII en clair, aucun
    financier, project_ids hashes. L'opt-in est explicite cle par cle.
    """
    cfg = {**_SHARE_DEFAULTS, **(share_config or {})}
    out = deepcopy(payload)
    meta = out.setdefault("_meta", {})

    # Email
    if not cfg["share_email"]:
        email = meta.get("user_email", "")
        if email:
            meta["user_email"] = f"hashed:{_hash_short(email)}"

    # Machine ID
    if not cfg["share_machine_id"]:
        mid = meta.get("machine_id", "")
        if mid:
            meta["machine_id"] = f"hashed:{_hash_short(mid, 12)}"

    # Financials (multiplier au global et par projet)
    if not cfg["share_financials"]:
        for scope in (out.get("global", {}),):
            mult = scope.get("multiplier", {})
            if isinstance(mult, dict):
                for k in _FINANCIAL_KEYS:
                    mult.pop(k, None)
        for proj_data in out.get("by_project", {}).values():
            mult = proj_data.get("multiplier", {}) if isinstance(proj_data, dict) else {}
            if isinstance(mult, dict):
                for k in _FINANCIAL_KEYS:
                    mult.pop(k, None)

    # Projets
    proj_mode = cfg["share_projects"]
    if proj_mode == "none":
        out.pop("by_project", None)
    elif proj_mode == "aggregate":
        new_by = {}
        for pid, data in out.get("by_project", {}).items():
            if pid in ("unclassified", "unknown"):
                new_by[pid] = data
            else:
                new_by[f"proj-{_hash_short(str(pid), 12)}"] = data
        if new_by:
            out["by_project"] = new_by
    # "full" -> on laisse tel quel

    # Signals
    if not cfg["share_signals"]:
        out.get("global", {}).pop("signals", None)
        for proj_data in out.get("by_project", {}).values():
            if isinstance(proj_data, dict):
                proj_data.pop("signals", None)

    # Profile
    if not cfg["share_profile"]:
        out.get("global", {}).pop("profile", None)

    # Tracabilite : on stocke quel filtre a ete applique pour audit cote serveur
    meta["share_config_applied"] = cfg
    return out


def build_insights_payload(
    storage,
    window: Window,
    user_email: str,
    machine_id: str,
    tjm_eur_per_day: float | None = None,
    value_produit_eur: float | None = None,
) -> dict[str, Any]:
    """Genere le payload insights complet."""
    overview = compute_overview(storage, window)
    mult = compute_multiplier(
        storage,
        window,
        tjm_eur_per_day=tjm_eur_per_day,
        value_produit_eur=value_produit_eur,
    )
    sigs = compute_all_signals(storage, window)
    prof = compute_profile(storage, window)

    # Breakdown par projet
    by_project = get_active_sec_by_project(storage, window)
    project_breakdowns = {}
    for project_id in by_project.keys():
        if project_id == "unclassified":
            continue
        win_p = Window(since=window.since, until=window.until, project=project_id)
        ov_p = compute_overview(storage, win_p)
        mult_p = compute_multiplier(
            storage, win_p,
            tjm_eur_per_day=tjm_eur_per_day,
            value_produit_eur=value_produit_eur,
        )
        project_breakdowns[project_id] = {
            "active_sec": by_project[project_id],
            "totals": ov_p["totals"],
            "ratios": ov_p["ratios"],
            "multiplier": mult_p,
        }

    return {
        "_meta": {
            "version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_email": user_email,
            "machine_id": machine_id,
            "window": overview["window"],
        },
        "global": {
            "totals": overview["totals"],
            "ratios": overview["ratios"],
            "multiplier": mult,
            "profile": prof,
            "signals": sigs,
        },
        "by_project": project_breakdowns,
    }


def push_insights_to_s3(
    payload: dict[str, Any],
    cloud_config: dict[str, Any],
    user_email: str,
    machine_id: str = "unknown",
    month_key: str | None = None,
    dry_run: bool = False,
    share_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Push le payload insights vers Garage S3.

    Key = insights/YYYY-MM/<user-slug>/<machine-slug>.json (non compresse,
    lisible direct). Le machine_id dans la cle permet aux users multi-Mac
    de pousser depuis chacune de leurs machines sans ecraser.

    Le payload est filtre via share_config (defaut conservateur : email
    hashe, financiers exclus, project_ids hashes). Si share_config n'est
    pas passe, on lit cloud_config.get("share_config") (privacy.yaml).
    """
    from ship1000x.exporters.s3_push import _slugify

    bucket = cloud_config.get("bucket")
    if not bucket:
        raise ValueError("cloud.bucket manquant dans privacy.yaml")

    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    if share_config is None:
        share_config = cloud_config.get("share_config")
    payload = filter_payload_by_share_config(payload, share_config)

    # The S3 key uses the original user_email (not hashed) to allow
    # dashboard-side routing. PII inside the payload IS hashed by default.
    user_slug = user_email.replace("@", "-at-").replace(".", "-")
    machine_slug = _slugify(machine_id)
    key = f"insights/{month_key}/{user_slug}/{machine_slug}.json"
    body = json.dumps(payload, default=str, ensure_ascii=False, indent=2).encode("utf-8")

    if dry_run:
        return {
            "dry_run": True,
            "key": key,
            "size_bytes": len(body),
        }

    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError
    except ImportError as e:
        raise RuntimeError("boto3 requis. pip install boto3") from e

    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")

    endpoint = cloud_config.get("endpoint")
    region = cloud_config.get("region", "garage")
    s3_kwargs: dict[str, Any] = {"region_name": region}
    if endpoint:
        s3_kwargs["endpoint_url"] = endpoint
    s3_kwargs["config"] = Config(
        s3={"addressing_style": "path"},
        connect_timeout=15,
        read_timeout=30,
        retries={"max_attempts": 3},
    )
    client = boto3.client("s3", **s3_kwargs)
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata={
                "user-email": user_email,
                "month": month_key,
                "generated-at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        raise RuntimeError(f"Echec upload {key} : {e}") from e

    return {
        "dry_run": False,
        "key": key,
        "size_bytes": len(body),
    }
