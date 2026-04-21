"""Health scan — scan toutes les sources de traces IA potentielles sur la machine.

Produit un JSON qui liste pour chaque source connue :
  - path scanne
  - statut : tracked (collector actif) | partial | not_tracked | absent
  - last_modified (ISO)
  - size_bytes (taille totale)
  - items_count (nb fichiers ou rows selon le type)
  - notes (contexte humain : "Cline extension, 3 taches actives", etc.)

Ce fichier est pushe a cote des rollups dans S3 :
  s3://<bucket>/health/<user>.json

Le dashboard l'utilise pour afficher un panel "Sources de tracking" avec
le statut de chaque source potentielle (tracee ou pas).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()


@dataclass
class SourceHealth:
    id: str
    label: str
    category: str           # "cli" | "ide_extension" | "desktop_app" | "system" | "manual"
    status: str             # "tracked" | "partial" | "not_tracked" | "absent" | "disabled"
    path: str
    path_exists: bool
    last_modified: str | None   # ISO
    size_bytes: int
    items_count: int | None     # nb files / rows / tasks
    value: str              # "high" | "medium" | "low"
    effort: str             # "done" | "easy" | "medium" | "hard"
    notes: str


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _dir_stats(path: Path, pattern: str | None = None) -> tuple[int, int, datetime | None]:
    """Retourne (total_size_bytes, items_count, last_modified_dt) pour un dossier.

    Si pattern fourni : ne compte que les fichiers matches par glob recursif.
    """
    if not path.exists():
        return (0, 0, None)
    total = 0
    count = 0
    latest: datetime | None = None
    iterator = path.rglob(pattern) if pattern else path.rglob("*")
    for p in iterator:
        if not p.is_file():
            continue
        try:
            st = p.stat()
            total += st.st_size
            count += 1
            mt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            if latest is None or mt > latest:
                latest = mt
        except (OSError, PermissionError):
            continue
    return (total, count, latest)


def _file_stats(path: Path) -> tuple[int, datetime | None]:
    if not path.exists() or not path.is_file():
        return (0, None)
    try:
        st = path.stat()
        return (st.st_size, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
    except (OSError, PermissionError):
        return (0, None)


def _sqlite_rowcount(path: Path, table: str) -> int | None:
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return None


def scan_sources(privacy_config: dict[str, Any] | None = None) -> list[SourceHealth]:
    """Scan toutes les sources connues et retourne leur etat de sante."""
    privacy_config = privacy_config or {}
    enabled_sources = privacy_config.get("sources", {}) or {}
    results: list[SourceHealth] = []

    # 1. Claude Code CLI
    p = HOME / ".claude" / "projects"
    size, count, last = _dir_stats(p, "*.jsonl")
    results.append(SourceHealth(
        id="claude_code",
        label="Claude Code CLI",
        category="cli",
        status="tracked" if enabled_sources.get("claude_code") == "enabled" else "not_tracked",
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size,
        items_count=count,
        value="high",
        effort="done",
        notes=f"{count} fichiers JSONL de sessions." if count else "Aucune session locale.",
    ))

    # 2. Codex CLI
    p = HOME / ".codex" / "sessions"
    size, count, last = _dir_stats(p, "*.jsonl")
    results.append(SourceHealth(
        id="codex",
        label="Codex CLI",
        category="cli",
        status="tracked" if enabled_sources.get("codex") == "enabled" else "not_tracked",
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size,
        items_count=count,
        value="high",
        effort="done",
        notes=f"{count} fichiers rollout JSONL." if count else "Aucune session Codex CLI.",
    ))

    # 3. Codex SQLite (metadata enrichissement)
    p = HOME / ".codex" / "state_5.sqlite"
    size_b, last = _file_stats(p)
    threads = _sqlite_rowcount(p, "threads")
    results.append(SourceHealth(
        id="codex_sqlite",
        label="Codex state SQLite",
        category="cli",
        status="tracked" if enabled_sources.get("codex_sqlite") == "enabled" else "not_tracked",
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size_b,
        items_count=threads,
        value="medium",
        effort="done",
        notes=f"{threads} threads enregistres." if threads else "Base absente/vide.",
    ))

    # 4. Cursor ai-tracking (base SQLite)
    p = HOME / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    size_b, last = _file_stats(p)
    scored = _sqlite_rowcount(p, "scored_commits") or 0
    convos = _sqlite_rowcount(p, "conversation_summaries") or 0
    hashes = _sqlite_rowcount(p, "ai_code_hashes") or 0
    # On lit ai_code_hashes (agrege par jour x fichier) + scored_commits.
    # conversation_summaries reste exploitable si jamais Cursor la remplit.
    cursor_status = (
        "tracked" if enabled_sources.get("cursor") == "enabled"
        else "not_tracked"
    )
    if p.exists() and convos > 0 and enabled_sources.get("cursor") == "enabled":
        # Table conversation_summaries non-vide : signal qu'on pourrait encore
        # l'exploiter (titres + model pour classification fine)
        cursor_status = "partial"
    results.append(SourceHealth(
        id="cursor",
        label="Cursor AI tracking DB",
        category="ide_extension",
        status=cursor_status,
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size_b,
        items_count=scored,
        value="medium",
        effort="done",
        notes=(
            f"{scored} commits scores, {hashes} ai_code_hashes agreges par jour."
            + (f" {convos} conversation_summaries exploitables pour enrichissement." if convos > 0 else " (table conversation_summaries vide chez cet utilisateur.)")
        ) if p.exists() else "Base absente.",
    ))

    # 5. Cline extension (Cursor globalStorage)
    p = (
        HOME / "Library" / "Application Support" / "Cursor"
        / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks"
    )
    size, count, last = _dir_stats(p)
    # Count = nb de tasks (dossiers directs)
    task_count = len([d for d in p.iterdir() if d.is_dir()]) if p.exists() else 0
    results.append(SourceHealth(
        id="cline",
        label="Cline extension (Cursor)",
        category="ide_extension",
        status="tracked" if enabled_sources.get("cline") == "enabled" else "not_tracked",
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size,
        items_count=task_count,
        value="high",
        effort="done",
        notes=(
            f"{task_count} taches agentiques tracees (split par repo touche via "
            "files_in_context). Model + mode act/plan extraits."
        ) if p.exists() else "Extension absente.",
    ))

    # 6. Claude Desktop app (stockage serveur : conversations non extractibles)
    p = HOME / "Library" / "Application Support" / "Claude"
    size, count, last = _dir_stats(p)
    results.append(SourceHealth(
        id="claude_desktop",
        label="Claude Desktop app",
        category="desktop_app",
        status="not_tracked",
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size,
        items_count=None,
        value="low",
        effort="hard",
        notes=(
            "Conversations claude.ai stockees cote serveur Anthropic. "
            "Local = caches/VMs (vm_bundles) + Claude Code CLI bin (claude-code/). "
            "IndexedDB local = 2 MB de state UI seulement. Pas extractible. "
            "Alternative : export manuel Settings > Privacy > Export data "
            "depose dans le drop folder."
        ) if p.exists() else "App non installee.",
    ))

    # 7. Codex Desktop app (stockage serveur : conversations non extractibles)
    p = HOME / "Library" / "Application Support" / "Codex"
    size, count, last = _dir_stats(p)
    results.append(SourceHealth(
        id="codex_desktop",
        label="Codex Desktop app",
        category="desktop_app",
        status="not_tracked",
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size,
        items_count=None,
        value="low",
        effort="hard",
        notes=(
            "Conversations ChatGPT/Codex stockees cote serveur OpenAI. "
            "Local = 3 MB de caches + feature flags Statsig + UI state. "
            "Pour capturer l'activite Codex Desktop voir la source "
            "`codex_desktop_logs` (extrait les sessions + paths des "
            "SSE events dans ~/.codex/state_5.sqlite)."
        ) if p.exists() else "App non installee.",
    ))

    # 7b. Codex Desktop logs (state_5.sqlite) - derive de l'app Desktop
    p = HOME / ".codex" / "state_5.sqlite"
    size_b, last = _file_stats(p)
    log_rows = _sqlite_rowcount(p, "logs") or 0
    results.append(SourceHealth(
        id="codex_desktop_logs",
        label="Codex Desktop logs (state_5.sqlite)",
        category="desktop_app",
        status=(
            "tracked" if enabled_sources.get("codex_desktop") == "enabled" and log_rows > 0
            else "not_tracked"
        ),
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size_b,
        items_count=log_rows,
        value="medium",
        effort="done",
        notes=(
            f"{log_rows} log entries (SSE events backend + tool calls). "
            "Complementaire de codex_macapp (logs frontend). Dedup auto par "
            "(day, project) : si codex_macapp couvre deja un (jour, projet), "
            "codex_desktop skip pour eviter le double compte."
        ) if p.exists() else "Pas de state_5.sqlite (Codex Desktop jamais lance?).",
    ))

    # 7c. Codex.app macOS logs (~/Library/Logs/com.openai.codex/)
    p = HOME / "Library" / "Logs" / "com.openai.codex"
    size, count, last = _dir_stats(p) if p.exists() else (0, 0, None)
    log_files_count = 0
    if p.exists():
        try:
            log_files_count = len(list(p.rglob("*.log")))
        except OSError:
            log_files_count = 0
    results.append(SourceHealth(
        id="codex_macapp",
        label="Codex.app macOS logs (Electron frontend)",
        category="desktop_app",
        status=(
            "tracked" if enabled_sources.get("codex_macapp", "enabled") == "enabled" and log_files_count > 0
            else "not_tracked"
        ),
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size,
        items_count=log_files_count,
        value="high",
        effort="done",
        notes=(
            f"{log_files_count} fichiers log d'app Codex.app native macOS. "
            "Parse method=turn/start (= vrais prompts user) + cwd= pour "
            "classification. Source la plus precise pour Codex.app "
            "(granularite turn-level, pas echantillonnage SSE)."
        ) if p.exists() else (
            "Codex.app jamais lance ou installe ailleurs. "
            "Telecharge depuis https://openai.com/codex si tu utilises l'app native."
        ),
    ))

    # 9. Git commits (collector deja actif via git_multi)
    size, count = 0, 0  # Pas de scan filesystem pour git — info dans rollups
    results.append(SourceHealth(
        id="git",
        label="Git commits multi-repos",
        category="system",
        status="tracked" if enabled_sources.get("git") == "enabled" else "not_tracked",
        path="(multi-repos scan)",
        path_exists=True,
        last_modified=None,
        size_bytes=0,
        items_count=None,
        value="medium",
        effort="done",
        notes="Scan parent + Desktop pour tous les .git. Rend visible l'effort par repo.",
    ))

    # 10. Shell history
    p = HOME / ".zsh_history"
    size_b, last = _file_stats(p)
    has_extended = False
    try:
        zshrc = (HOME / ".zshrc").read_text(errors="ignore") if (HOME / ".zshrc").exists() else ""
        has_extended = "EXTENDED_HISTORY" in zshrc
    except OSError:
        pass
    results.append(SourceHealth(
        id="shell",
        label="zsh history",
        category="system",
        status=("tracked" if enabled_sources.get("shell") == "enabled" and has_extended
                else "partial" if enabled_sources.get("shell") == "enabled"
                else "disabled"),
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size_b,
        items_count=None,
        value="low",
        effort="easy",
        notes=(
            "Active + EXTENDED_HISTORY OK." if enabled_sources.get("shell") == "enabled" and has_extended
            else "Collector active mais EXTENDED_HISTORY pas configure dans .zshrc : "
                 "aucune commande captee. Ajoute 'setopt EXTENDED_HISTORY' dans ~/.zshrc."
            if enabled_sources.get("shell") == "enabled"
            else "Desactive dans privacy.yaml."
        ),
    ))

    # 11. macOS pmset / mac_system
    results.append(SourceHealth(
        id="mac_system",
        label="macOS pmset (wake/sleep)",
        category="system",
        status="disabled" if enabled_sources.get("mac_system") != "enabled" else "tracked",
        path="(pmset -g log, log show)",
        path_exists=True,
        last_modified=None,
        size_bytes=0,
        items_count=None,
        value="low",
        effort="medium",
        notes=(
            "Desactive. La regex pmset n'a pas ete validee sur macOS 15. "
            "Cross-check eveil/sommeil pour confirmer presence machine."
        ),
    ))

    # 12. Drop folder (imports manuels Claude.ai / ChatGPT)
    p = HOME / "ai-time-tracker" / "drop"
    size, count, last = _dir_stats(p) if p.exists() else (0, 0, None)
    results.append(SourceHealth(
        id="web_exports",
        label="Imports manuels (Claude.ai / ChatGPT)",
        category="manual",
        status="tracked" if enabled_sources.get("web_exports") == "enabled" else "not_tracked",
        path=str(p),
        path_exists=p.exists(),
        last_modified=_iso(last),
        size_bytes=size,
        items_count=count,
        value="medium",
        effort="done",
        notes=(
            f"{count} exports deposes." if count
            else "Aucun import. Drag-drop un ZIP Claude.ai Settings -> Export."
        ),
    ))

    return results


def health_payload(user_email: str, sources: list[SourceHealth]) -> dict[str, Any]:
    """Assemble le payload complet a pusher dans S3."""
    return {
        "_meta": True,
        "user_email": user_email,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machine": os.uname().nodename,
        "os": f"{os.uname().sysname} {os.uname().release}",
        "version": "1.0",
        "sources": [asdict(s) for s in sources],
        "summary": {
            "total": len(sources),
            "tracked": sum(1 for s in sources if s.status == "tracked"),
            "partial": sum(1 for s in sources if s.status == "partial"),
            "not_tracked": sum(1 for s in sources if s.status == "not_tracked"),
            "disabled": sum(1 for s in sources if s.status == "disabled"),
            "absent": sum(1 for s in sources if not s.path_exists),
        },
    }
