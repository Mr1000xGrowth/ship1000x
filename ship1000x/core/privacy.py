"""Privacy filter — sanitization avant stockage.

Regle non-negociable : aucun contenu (prompt, fichier, diff, reponse) ne doit
atterrir dans la DB. Uniquement des metadonnees quantitatives.

Cette fonction est le gardien. Tout event qui n'a pas ete passe par
`sanitize_event()` ne doit PAS etre stocke.
"""

from __future__ import annotations

import hashlib
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

# Cles autorisees dans raw_meta (metadata pure)
ALLOWED_META_KEYS = {
    "tool_name", "extension", "file_size_bytes",
    "lines_added", "lines_deleted", "files_changed",
    "duration_ms", "model", "finish_reason",
    "match_count", "result_count",
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


def sanitize_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Filtre les cles autorisees uniquement, anonymise les paths."""
    if not meta:
        return {}
    clean: dict[str, Any] = {}
    for key, value in meta.items():
        if key in FORBIDDEN_META_KEYS:
            continue
        if key not in ALLOWED_META_KEYS:
            # Par defaut on skip — whitelist strict
            continue
        if isinstance(value, str) and ("/" in value or "\\" in value):
            clean[key] = anonymize_path(value)
        else:
            clean[key] = value
    return clean


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Passage obligatoire avant upsert_event.

    Supprime ou anonymise tout ce qui pourrait etre du contenu.
    """
    safe = dict(event)
    # Anonymise cwd
    if safe.get("cwd"):
        safe["cwd"] = anonymize_path(safe["cwd"])
    # Filtre raw_meta (sera stocke en JSON)
    if "raw_meta" in safe and isinstance(safe["raw_meta"], dict):
        safe["raw_meta"] = sanitize_meta(safe["raw_meta"])
    # Garantit qu'aucun champ "content-like" ne traine
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
