"""Privacy filter — sanitization avant stockage.

Regle non-negociable : aucun contenu (prompt, fichier, diff, reponse) ne doit
atterrir dans la DB. Uniquement des metadonnees quantitatives.

Cette fonction est le gardien. Tout event qui n'a pas ete passe par
`sanitize_event()` ne doit PAS etre stocke.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

HOME = str(Path.home())


# Cles interdites dans raw_meta (contiennent potentiellement du contenu)
FORBIDDEN_META_KEYS = {
    "content", "text", "message", "prompt", "response",
    "body", "source_code", "diff", "file_content",
    "input", "output", "result", "stdout", "stderr",
    "command", "new_string", "old_string",
}

# Cles autorisees dans raw_meta (metadata pure).
# Whitelist alignee sur l'inventaire reel des collectors (~46 cles observees).
# Tout ajout de cle par un collector doit etre liste ici sinon il est filtre.
ALLOWED_META_KEYS = {
    # Identifiants opaques (pas de contenu)
    "session_id", "session_uuid", "process_uuid", "workspace_id",
    "task_id", "pid", "commit_hash", "primary_project",
    # Compteurs numeriques
    "lines_added", "lines_deleted", "files_changed", "file_count",
    "lines_real_added", "lines_real_deleted",
    "lines_seed_added", "lines_seed_deleted",
    "lines_vendored_added", "lines_vendored_deleted",
    "lines_generated_added", "lines_generated_deleted",
    "block_count", "turn_count", "tool_call_count",
    "user_msg_count", "user_msg_counts", "assistant_turns",
    "api_turn_count", "marker_duration", "cwds_count",
    "match_count", "result_count", "file_size_bytes", "duration_ms",
    # Tokens (exposes pour audit)
    "cache_read_tokens", "cache_write_tokens", "cached_input_tokens",
    "reasoning_output_tokens",
    # Strings courtes categorielles
    "model", "mode", "auth_mode", "source_api", "tool_name",
    "extension", "extensions", "finish_reason", "is_seed_commit",
    # Structures agregees (timeline, stats par modele, ratios)
    "model_stats", "event_timeline", "tool_calls", "split_ratio",
    # Paths a anonymiser (traites specifiquement plus bas)
    "paths_sampled", "files_touched", "log_file",
}

# Sous-ensemble des cles ALLOWED dont la valeur peut contenir des paths
# absolus → necessite anonymisation recursive.
META_KEYS_WITH_PATHS = {
    "paths_sampled", "files_touched", "log_file", "primary_project",
}


def anonymize_path(path: str) -> str:
    """Remplace le home dir par ~ pour eviter de stocker le username."""
    if not path:
        return path
    if path.startswith(HOME):
        return "~" + path[len(HOME):]
    # Regex pour attraper /Users/<anyname>/ et /home/<anyname>/
    path = re.sub(r"^/Users/[^/]+", "~", path)
    path = re.sub(r"^/home/[^/]+", "~", path)
    return path


def hash_content(content: str) -> str:
    """SHA256 court pour dedup sans stocker le contenu."""
    if not content:
        return ""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _anonymize_value(value: Any) -> Any:
    """Anonymise recursivement les paths dans une valeur (str, list, dict)."""
    if isinstance(value, str):
        if "/" in value or "\\" in value:
            return anonymize_path(value)
        return value
    if isinstance(value, list):
        return [_anonymize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _anonymize_value(v) for k, v in value.items()}
    return value


def sanitize_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Filtre les cles autorisees uniquement, anonymise les paths recursivement."""
    if not meta:
        return {}
    clean: dict[str, Any] = {}
    for key, value in meta.items():
        if key in FORBIDDEN_META_KEYS:
            continue
        if key not in ALLOWED_META_KEYS:
            # Whitelist stricte : tout ce qui n'est pas explicitement autorise saute.
            continue
        if key in META_KEYS_WITH_PATHS:
            clean[key] = _anonymize_value(value)
        elif isinstance(value, str) and ("/" in value or "\\" in value):
            clean[key] = anonymize_path(value)
        else:
            clean[key] = value
    return clean


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Passage obligatoire avant upsert_event.

    Supprime ou anonymise tout ce qui pourrait etre du contenu.
    Les collectors passent raw_meta sous forme de string JSON ; on
    deserialize d'abord avant filtrage, puis on re-serialize pour
    conformite avec le format de stockage attendu (TEXT JSON).
    """
    safe = dict(event)
    # Anonymise cwd
    if safe.get("cwd"):
        safe["cwd"] = anonymize_path(safe["cwd"])
    # Filtre raw_meta (sera stocke en JSON string)
    raw_meta = safe.get("raw_meta")
    if raw_meta is not None:
        meta_dict: dict[str, Any] | None = None
        if isinstance(raw_meta, str):
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    meta_dict = parsed
            except (json.JSONDecodeError, ValueError):
                meta_dict = None
        elif isinstance(raw_meta, dict):
            meta_dict = raw_meta
        # Tout ce qui n'est ni dict ni JSON-dict valide est rejete par precaution.
        if meta_dict is not None:
            cleaned = sanitize_meta(meta_dict)
            safe["raw_meta"] = json.dumps(cleaned, ensure_ascii=False)
        else:
            safe["raw_meta"] = None
    # Garantit qu'aucun champ "content-like" ne traine au top-level
    for key in list(safe.keys()):
        if key in FORBIDDEN_META_KEYS:
            del safe[key]
    return safe


def is_excluded_path(path: str, exclude_patterns: list[str]) -> bool:
    """Check si un path match un pattern d'exclusion (glob-like simple)."""
    import fnmatch
    if not path:
        return False
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False
