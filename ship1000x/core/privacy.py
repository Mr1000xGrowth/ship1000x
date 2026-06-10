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
    # Claude Desktop session sidecar identifiers and metadata fields.
    # All categorical / numeric / GitHub-public values; no raw content.
    "cli_session_id", "title", "title_source", "permission_mode",
    "pr_number", "pr_url", "pr_repository", "pr_state",
    "remote_mcp_count", "is_archived",
    # Compteurs numeriques
    "lines_added", "lines_deleted", "files_changed", "file_count",
    "lines_real_added", "lines_real_deleted",
    "lines_seed_added", "lines_seed_deleted",
    "lines_vendored_added", "lines_vendored_deleted",
    "lines_generated_added", "lines_generated_deleted",
    # Nature du travail (decompose "real" : code/docs/config/data). Compteurs
    # numeriques uniquement, aucun chemin ni contenu.
    "lines_code_added", "lines_code_deleted",
    "lines_docs_added", "lines_docs_deleted",
    "lines_config_added", "lines_config_deleted",
    "lines_data_added", "lines_data_deleted",
    "block_count", "turn_count", "tool_call_count",
    "user_msg_count", "user_msg_counts", "assistant_turns",
    "api_turn_count", "marker_duration", "cwds_count",
    "match_count", "result_count", "file_size_bytes", "duration_ms",
    # Tokens (exposes pour audit)
    "cache_read_tokens", "cache_write_tokens", "cached_input_tokens",
    "reasoning_output_tokens",
    # Claude Code /compact event tracking. `compact_count` is the
    # number of `isCompactSummary: true` records emitted during the
    # day; `compact_first` / `compact_last` are ISO timestamps of the
    # first and last compact. Safe — pure counters + ISO timestamps,
    # never the summary payload itself.
    "compact_count", "compact_first", "compact_last",
    # Gitleaks secret-scan findings (opt-in). Stored sanitised by
    # `ship1000x.core.gitleaks_scan._sanitize_finding` — only the
    # rule id (categorical), short description, file basename, line
    # number, and commit SHA. Never the matched secret string itself,
    # never the committer name/email.
    "gitleaks_rule_id", "gitleaks_description", "gitleaks_file",
    "gitleaks_line",
    # Claude Code statusline tick aggregation (Wave 3). Safe — only
    # numeric counters + categorical session id + max context usage
    # percentage. Never the transcript content or current prompt.
    "statusline_tick_count", "context_used_percent_max",
    "compact_triggered",
    # ABTop-style runtime daemon tick aggregation (Wave 3 / Day 3-5).
    # Safe — pure numeric counters + categorical process name. No
    # cmdline, no PIDs, no usernames. Process names are an enum-like
    # categorical sourced from the AI whitelist in watch_daemon.py.
    "process_name", "process_tick_count", "max_concurrent_processes",
    "open_ports_count", "mcp_servers_detected",
    # Local usage proxy bridge. Safe — the
    # upstream proxy's redaction layer guarantees only 9 keys
    # survive (provider / model / endpoint / 4 token counts /
    # usage_quality / captured_at). All categorical strings or
    # numeric counters. Never any conversation content or
    # auth header. The cache_*_input_tokens names match the
    # Anthropic API response shape upstream of the proxy.
    "provider", "endpoint", "usage_quality",
    "cache_read_input_tokens", "cache_creation_input_tokens",
    # Strings courtes categorielles
    "model", "mode", "auth_mode", "source_api", "tool_name",
    "extension", "extensions", "finish_reason", "is_seed_commit",
    # Codex rollout client identity. Categorical enum sourced from the
    # rollout `session_meta.originator` (e.g. "codex_desktop", "codex_exec",
    # "codex-tui", "codex_sdk_ts"). Distinguishes the Codex client (app vs
    # CLI) which otherwise all collapse into source='codex'. Safe: fixed
    # enum, zero free-text, no path, no identifier. Opt-in surfaced in the
    # audit log "Client" column.
    "originator",
    # Provenance label that traces where the model id came from
    # ("macapp_log" | "logs_2_sqlite_join" | "unknown"). Safe — fixed
    # enum, never a path or identifier.
    "model_source",
    # Billing-side label (e.g. "billing_aggregated") preserved alongside
    # auth_mode so reports can tell a billing snapshot apart from a local
    # api_key event without re-deriving it from source.
    "billing_source",
    # Cost returned by the provider's Admin cost endpoint, prorated by
    # tokens. SHIP does not use it as the canonical billed cost (units /
    # inclusion rules are not stable across providers — Anthropic showed
    # a ~100x divergence with token*pricing in mid-2026), but we keep
    # the field so a future drift analysis can compare.
    "api_reported_cost",
    # Entrypoint captured from Claude Code JSONL (claude-desktop / cli /
    # sdk-cli) — feeds the auth-mode detector at re-read time and is safe
    # because it is a fixed enum string, never a path or identifier.
    "entrypoint",
    # Structures agregees (timeline, stats par modele, ratios)
    "model_stats", "event_timeline", "tool_calls", "split_ratio",
    # Breakdown par NOM de tool ({"Bash": 12, "Read": 30, ...}). Categorique
    # pur : uniquement les noms d'outils (enum-like, ex. Bash/Read/Edit cote
    # Claude Code, shell/apply_patch cote Codex) + un compteur d'appels. JAMAIS
    # les arguments, le contenu de la commande, les paths ou la sortie de
    # l'outil (ceux-la restent dans FORBIDDEN_META_KEYS / non collectes).
    "tool_breakdown",
    # Usage/cost quality metadata (safe structured counters only)
    "usage",
    # Superset normalise des tokens cross-provider (capture exhaustive).
    # Compteurs numeriques + categoriques (provider, service_tier) — aucun contenu.
    "usage_breakdown",
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


def _sanitize_nested_value(value: Any) -> Any:
    """Sanitize nested metadata values while preserving safe counters/maps."""
    if isinstance(value, dict):
        return {
            k: _sanitize_nested_value(v)
            for k, v in value.items()
            if k not in FORBIDDEN_META_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_nested_value(v) for v in value]
    return _anonymize_value(value)


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
        if key in META_KEYS_WITH_PATHS or isinstance(value, (dict, list)):
            clean[key] = _sanitize_nested_value(value)
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
