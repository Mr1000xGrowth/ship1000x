"""Collector Git multi-repos.

Scanne les repos sous le parent dir du tracker (ou chemins donnes par config)
et ingere `git log --numstat --pretty=format:"%H|%aI|%an|%s" --since=...`.

Classification via path du repo (cwd) + remote origin.
Privacy : commitMessage stocke uniquement si le privacy_level du projet l'autorise.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Scan racine : TOUT le HOME par defaut (generique pour tous les users).
# Avant : hardcode sur ~/Desktop + ~/ClaudeCode - Test OpenClaw GHL,
# ce qui manquait les repos dans ~/Documents, ~/Projects, ~/Code,
# ~/openclaw, etc. Maintenant on walk tout le HOME avec une skip list
# agressive pour la perf (pas de node_modules, pas de Library macOS, etc.).
DEFAULT_SCAN_ROOTS = [
    Path.home(),
]
DEFAULT_SINCE_DAYS = 365

# Dossiers a ne jamais scanner (perf + bruit). Ces dossiers peuvent
# contenir des milliers de .git/ internes (dependencies, node_modules,
# caches) qui ne sont pas des projets de l'utilisateur.
SKIP_DIRS = frozenset({
    # Deps / caches
    "node_modules", ".venv", "venv", "env", ".env",
    ".cache", ".npm", ".yarn", ".pnpm-store", ".pip", ".cargo",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox",
    # Build outputs
    "dist", "build", "out", ".next", ".nuxt", "target",
    # Vendor / third-party
    "vendor", "bower_components", "third_party", "vendor_imports",
    # OS / app
    "Library",  # macOS, enorme
    ".Trash", ".DS_Store",
    # IDE caches
    ".vscode-server", ".idea",
    # Heavy app data
    "Applications",
    # Specific to dev tooling
    ".gradle", ".m2", ".ivy2",
    # Version managers
    ".rbenv", ".pyenv", ".nodenv", ".nvm",
    # Docker / VM
    ".docker", ".local/share/containers",
    # Dotdirs d'outils IA : contiennent des caches/plugins/vendor_imports
    # clones depuis upstream (pas des projets user).
    ".claude", ".codex", ".cursor",
})


def _run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _find_git_repos(roots: list[Path], max_depth: int = 6) -> Iterator[Path]:
    """Trouve les repos git sous les roots (hors node_modules / .venv / etc.).

    Particularite : continue de walker meme dans les repos git pour capturer
    les sous-repos INDEPENDANTS (pas sub-modules) qui ont leur propre
    remote.origin.url distinct. Cas d'usage : ~/Desktop/<wrapper-repo>/
    contient un .git/ mais ses enfants <project>/ sont aussi des repos
    independants avec .git/ separe et remotes differents.

    On filtre les sub-modules declares via `.gitmodules` du parent pour
    eviter les doubles comptes.

    Scan agressif mais skip list complete (SKIP_DIRS) pour la perf :
    node_modules, Library, .venv, .cache, etc. sont evites.
    """
    seen = set()
    # .git : on skip le contenu interne mais pas le dossier parent (c'est
    # justement comme ca qu'on reconnait un repo git).
    exclude_dirs = SKIP_DIRS | {".git"}

    def _load_submodules(repo: Path) -> set[Path]:
        """Lit .gitmodules et retourne les paths des submodules (resolus)."""
        gm = repo / ".gitmodules"
        if not gm.exists():
            return set()
        paths: set[Path] = set()
        try:
            text = gm.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("path"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        rel = parts[1].strip()
                        if rel:
                            paths.add((repo / rel).resolve())
        except OSError:
            pass
        return paths

    def walk(path: Path, depth: int, parent_submodules: set[Path]):
        if depth > max_depth or not path.exists() or not path.is_dir():
            return
        if path.name in exclude_dirs:
            return

        resolved = path.resolve()
        is_repo = (path / ".git").exists()

        # Skip si c'est un submodule declare par un parent (evite double compte)
        if is_repo and resolved in parent_submodules:
            return

        # Yield si repo git et pas deja vu
        new_submodules = parent_submodules
        if is_repo and resolved not in seen:
            seen.add(resolved)
            yield resolved
            # Charge les submodules de ce repo pour filtrer les enfants
            own_submodules = _load_submodules(path)
            new_submodules = parent_submodules | own_submodules

        # Continue de walker dans les sous-dossiers (meme si is_repo=True)
        # pour capturer les sous-repos independants au parent
        try:
            for child in path.iterdir():
                if child.is_dir() and child.name not in exclude_dirs:
                    yield from walk(child, depth + 1, new_submodules)
        except (PermissionError, OSError):
            return

    for root in roots:
        yield from walk(root, 0, set())


def _parse_git_log(
    repo: Path,
    since: datetime,
) -> list[dict[str, Any]]:
    """Parse git log --numstat -M -C avec un separateur robuste.

    Les flags `-M` et `-C` activent la detection de renames (≥50%) et
    copies (≥50%), reduisant le bruit "add + delete massif" quand des
    fichiers sont simplement deplaces.

    Retourne chaque commit avec sa liste detaillee `files` pour que le
    classifier de lignes puisse categoriser file-par-file (real/seed/
    vendored/generated).
    """
    output = _run_git(
        repo,
        "log",
        f"--since={since.isoformat()}",
        "--numstat",
        "-M",  # detect renames
        "-C",  # detect copies
        "--pretty=format:---COMMIT---%H|%aI|%an|%ae|%s",
    )
    if not output:
        return []

    commits = []
    current = None
    for line in output.splitlines():
        if line.startswith("---COMMIT---"):
            if current:
                commits.append(current)
            parts = line.replace("---COMMIT---", "").split("|", 4)
            if len(parts) < 5:
                current = None
                continue
            h, ts, author, email, subject = parts
            current = {
                "hash": h,
                "timestamp": ts,
                "author": author,
                "email": email,
                "subject": subject,
                "lines_added": 0,
                "lines_deleted": 0,
                "files_changed": 0,
                # Detail par fichier pour la classification real/seed/vendored/generated
                "files": [],  # list[(path, added, deleted)]
            }
        elif line.strip() and current is not None:
            # Format numstat : "added\tdeleted\tpath" (binaires: - -)
            parts = line.split("\t")
            if len(parts) >= 3:
                added_str = parts[0]
                deleted_str = parts[1]
                # Le path peut contenir des tabs/rename syntax "old => new"
                path = "\t".join(parts[2:]).strip()
                # git avec -M -C utilise "{old => new}" ou "old => new" pour
                # les renames — on garde le "new" (dest) pour la classification
                if " => " in path:
                    path = _extract_rename_dest(path)
                try:
                    added = int(added_str) if added_str != "-" else 0
                    deleted = int(deleted_str) if deleted_str != "-" else 0
                except ValueError:
                    continue
                current["lines_added"] += added
                current["lines_deleted"] += deleted
                current["files_changed"] += 1
                current["files"].append((path, added, deleted))

    if current:
        commits.append(current)

    return commits


def _extract_rename_dest(path: str) -> str:
    """Pour un rename "src/{old => new}.ts" ou "src/old => src/new", retourne le dest."""
    # Cas "dir/{old => new}"
    if "{" in path and " => " in path:
        # Ex: "src/{old.ts => new.ts}" → "src/new.ts"
        before_brace, rest = path.split("{", 1)
        inside, after_brace = rest.split("}", 1)
        if " => " in inside:
            _, new_name = inside.split(" => ", 1)
            return f"{before_brace}{new_name}{after_brace}"
    # Cas simple "old => new"
    if " => " in path:
        _, new_name = path.split(" => ", 1)
        return new_name.strip()
    return path.strip()


def _is_first_commit(repo: Path, commit_hash: str) -> bool:
    """True si le commit est un root commit (aucun parent)."""
    roots_output = _run_git(repo, "rev-list", "--max-parents=0", "HEAD")
    roots = {line.strip() for line in roots_output.splitlines() if line.strip()}
    return commit_hash in roots


def _get_remote_url(repo: Path) -> str:
    return _run_git(repo, "config", "--get", "remote.origin.url").strip()


def _stable_event_id(repo_path: str, commit_hash: str) -> str:
    raw = f"git|{repo_path}|{commit_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    from ship1000x.core.line_classifier import (
        classify_commit_lines,
        parse_gitattributes,
    )
    from ship1000x.core.line_classifier import (
        load_config as load_line_config,
    )
    from ship1000x.core.privacy import anonymize_path, is_excluded_path, sanitize_event

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    exclude_paths = privacy_config.get("exclude_paths", []) or []

    since = datetime.utcnow() - timedelta(days=DEFAULT_SINCE_DAYS)

    # Charge la config de classification (versionned + override local si present)
    line_config_base = Path(__file__).parent.parent / "config" / "line_classification.yaml"
    line_config_local = Path(__file__).parent.parent / "config" / "line_classification.local.yaml"
    line_config = load_line_config(line_config_base, line_config_local)

    for repo in _find_git_repos(DEFAULT_SCAN_ROOTS):
        stats["files_seen"] += 1
        repo_str = str(repo)

        if is_excluded_path(repo_str, exclude_paths):
            stats["skipped"] += 1
            continue

        # Dedup : check si on a deja ingere ce repo + HEAD
        head = _run_git(repo, "rev-parse", "HEAD").strip()
        if not head:
            continue
        repo_key = anonymize_path(repo_str)
        last_head_hash = storage.get_ingestion_offset("git", repo_key)
        # On utilise un hash numerique derive du HEAD
        head_numeric = int(head[:8], 16) if len(head) >= 8 else 0
        if head_numeric == last_head_hash:
            continue

        # Classification du repo via path + remote
        remote = _get_remote_url(repo)
        project_id, conf = classifier.classify_session(
            cwd=repo_str,
            git_remote=remote,
        )

        # Parse git log (avec -M -C pour detection rename/copy)
        commits = _parse_git_log(repo, since)

        # Charge une fois les root commits du repo (optimisation)
        roots_output = _run_git(repo, "rev-list", "--max-parents=0", "HEAD")
        root_commits = {line.strip() for line in roots_output.splitlines() if line.strip()}

        # Charge .gitattributes une fois par repo (pour linguist-generated/vendored)
        gitattr_rules = parse_gitattributes(repo)

        for commit in commits:
            # Classification fichier par fichier : real / seed / vendored / generated
            categories = classify_commit_lines(
                commit_message=commit["subject"],
                files_with_stats=commit["files"],
                config=line_config,
                is_first_commit=commit["hash"] in root_commits,
                gitattributes_rules=gitattr_rules,
            )

            event = {
                "id": _stable_event_id(repo_key, commit["hash"]),
                "source": "git",
                "event_type": "commit",
                "started_at": commit["timestamp"],
                "ended_at": commit["timestamp"],
                "duration_sec": 0,
                "cwd": repo_str,
                "project_id": project_id,
                "project_conf": conf,
                "tool_or_action": "commit",
                "token_input": 0,
                "token_output": 0,
                "cost_estimated": 0.0,
                "user_msg_type": None,
                "wordcount": 0,
                "confidence_flag": "high" if conf >= 0.8 else "medium",
                "raw_meta": json.dumps({
                    # V2 multi-Mac : commit_hash explicite pour dedup cross-machines
                    # cote rollup (GROUP_CONCAT DISTINCT) et cote dashboard (set dedup)
                    "commit_hash": commit["hash"],
                    # Totaux (retrocompat avec l'ancien format)
                    "lines_added": commit["lines_added"],
                    "lines_deleted": commit["lines_deleted"],
                    "files_changed": commit["files_changed"],
                    # Nouvelles donnees : breakdown par categorie
                    "lines_real_added": categories["real"]["lines_added"],
                    "lines_real_deleted": categories["real"]["lines_deleted"],
                    "lines_seed_added": categories["seed"]["lines_added"],
                    "lines_seed_deleted": categories["seed"]["lines_deleted"],
                    "lines_vendored_added": categories["vendored"]["lines_added"],
                    "lines_vendored_deleted": categories["vendored"]["lines_deleted"],
                    "lines_generated_added": categories["generated"]["lines_added"],
                    "lines_generated_deleted": categories["generated"]["lines_deleted"],
                    "is_seed_commit": commit["hash"] in root_commits,
                }),
            }
            safe = sanitize_event(event)
            storage.upsert_event(safe)
            stats["events_ingested"] += 1

        storage.set_ingestion_offset("git", repo_key, head_numeric, datetime.utcnow().isoformat())

    return stats
