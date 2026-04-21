"""S3 push — envoie les daily_rollup agreges vers un bucket S3.

Format du payload :
  s3://<bucket>/rollups/<YYYY-MM>/<user>.jsonl.gz

Contenu : JSONL compressee, 1 ligne = 1 rollup.
Volume typique : ~50 KB/mois/personne. Negligeable.

Privacy : seul le contenu conforme `get_rollups_for_push()` est envoye.
"""

from __future__ import annotations

import gzip
import io
import json
import re
from datetime import datetime, timezone
from typing import Any


def _slugify(raw: str) -> str:
    """Transforme un machine_id / hostname en slug safe pour path S3.

    "Mac-Studio.local" -> "mac-studio-local"
    "Charles's MacBook Pro" -> "charles-s-macbook-pro"
    """
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "unknown-machine"


def build_jsonl_gz(
    rollups: list[dict[str, Any]],
    user_email: str,
    machine_id: str,
) -> bytes:
    """Construit le payload JSONL.gz pour un mois donne."""
    lines = []
    # Header : meta
    header = {
        "_meta": True,
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_email": user_email,
        "machine_id": machine_id,
        "rollup_count": len(rollups),
    }
    lines.append(json.dumps(header))

    # Rollups
    for r in rollups:
        lines.append(json.dumps(r, separators=(",", ":")))

    raw = "\n".join(lines).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        gz.write(raw)
    return buf.getvalue()


def group_rollups_by_month(rollups: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Regroupe les rollups par YYYY-MM (pour partition S3)."""
    buckets: dict[str, list] = {}
    for r in rollups:
        date = r["date"]  # YYYY-MM-DD
        month = date[:7]  # YYYY-MM
        buckets.setdefault(month, []).append(r)
    return buckets


def push_to_s3(
    rollups: list[dict[str, Any]],
    cloud_config: dict[str, Any],
    user_email: str,
    machine_id: str = "unknown",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Push rollups vers un bucket S3 partitionnes par mois.

    Returns:
        {"uploaded": int, "dry_run": bool, "objects": [{"key", "size_bytes", "month", "rollup_count"}]}
    """
    if not rollups:
        return {"uploaded": 0, "dry_run": dry_run, "objects": []}

    bucket = cloud_config.get("bucket")
    if not bucket:
        raise ValueError("cloud.bucket manquant dans privacy.yaml")

    # Identifiants safes pour paths S3. On inclut le machine_id dans la cle
    # pour supporter les users multi-Mac : chaque Mac pousse dans son propre
    # sous-dossier et les readers cote dashboard agregent toutes les machines
    # d'un meme user.
    user_slug = user_email.replace("@", "-at-").replace(".", "-")
    machine_slug = _slugify(machine_id)

    by_month = group_rollups_by_month(rollups)
    results = []

    if dry_run:
        for month, rs in by_month.items():
            payload = build_jsonl_gz(rs, user_email, machine_id)
            key = f"rollups/{month}/{user_slug}/{machine_slug}.jsonl.gz"
            results.append({
                "key": key,
                "size_bytes": len(payload),
                "month": month,
                "rollup_count": len(rs),
            })
        return {"uploaded": 0, "dry_run": True, "objects": results}

    try:
        import os
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError
    except ImportError as e:
        raise RuntimeError("boto3 requis pour push S3. pip install boto3") from e

    # Garage S3 ne supporte pas la streaming signature boto3 par defaut
    # (envoie `x-amz-content-sha256: STREAMING-...` que Garage rejette).
    # On force le hash deterministe via ces 2 env vars (AWS SDK v2 style).
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
    # Credentials via env vars AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
    # ou ~/.aws/credentials.
    client = boto3.client("s3", **s3_kwargs)

    uploaded = 0
    for month, rs in by_month.items():
        payload = build_jsonl_gz(rs, user_email, machine_id)
        key = f"rollups/{month}/{user_slug}/{machine_slug}.jsonl.gz"
        try:
            # NOTE : on ne set PAS ContentEncoding=gzip ici. Sinon certains
            # clients S3 cote lecture (ex: AWS SDK JS v3) decompressent le body
            # automatiquement avant qu'on ait la main dessus, et un
            # `gunzipSync` explicite cote reader echoue avec "incorrect header
            # check". On laisse ContentType=application/gzip qui signale le
            # format sans activer la decompression auto.
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=payload,
                ContentType="application/gzip",
                Metadata={
                    "user-email": user_email,
                    "machine-id": machine_id,
                    "rollup-count": str(len(rs)),
                    "generated-at": datetime.now(timezone.utc).isoformat(),
                },
            )
            uploaded += 1
            results.append({
                "key": key,
                "size_bytes": len(payload),
                "month": month,
                "rollup_count": len(rs),
            })
        except ClientError as e:
            raise RuntimeError(f"Echec upload {key} : {e}") from e

    return {"uploaded": uploaded, "dry_run": False, "objects": results}
