"""Push des insights precalcules vers un bucket S3.

Format : `s3://<bucket>/insights/<YYYY-MM>/<user-slug>.json`

Contenu : overview + multiplier + signals + profile pour le mois.
Un dashboard externe peut lire ce JSON en read-only et l'afficher direct
(pas de recalcul cote serveur).

Privacy : meme principe que les rollups. Uniquement des metriques agregees,
aucun contenu. Couvert par le consent global.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from ship1000x.insights.engine import Window, compute_overview, get_active_sec_by_project
from ship1000x.insights.multiplier import compute_multiplier
from ship1000x.insights.profile import compute_profile
from ship1000x.insights.signals import compute_all_signals


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
) -> dict[str, Any]:
    """Push le payload insights vers un bucket S3.

    Key = insights/YYYY-MM/<user-slug>/<machine-slug>.json (non compresse,
    lisible direct). Le machine_id dans la cle permet aux users multi-Mac
    de pousser depuis chacune de leurs machines sans ecraser.
    """
    from ship1000x.exporters.s3_push import _slugify

    bucket = cloud_config.get("bucket")
    if not bucket:
        raise ValueError("cloud.bucket manquant dans privacy.yaml")

    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

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
