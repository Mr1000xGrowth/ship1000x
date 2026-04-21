"""Discovery — scan exhaustif du $HOME pour trouver tout emplacement d'outils IA.

Certains users ont des installs Claude Code / Codex / Cursor a des endroits
non-standards : installs reloges, copies historiques, workspaces custom.
Les collectors hardcodent le path par defaut (~/.claude/projects/, ~/.codex/sessions/,
etc.) et ignorent silencieusement le reste.

Ce module scanne le HOME (max depth 4, skip list standard) pour trouver TOUS
les dossiers susceptibles de contenir des donnees IA trackables, et les expose
via `discovered_paths` dans privacy.yaml.

Chaque collector peut ensuite lire cette liste en plus de son path par defaut.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Dossiers a chercher. Chaque entry = (collector_id, match_function).
# Le match_function prend un Path et retourne True si ce dossier est un match
# pour ce collector (= contient des donnees a parser).
DISCOVERABLE_TARGETS: dict[str, callable] = {
    "claude_code": lambda p: p.name == "projects" and p.parent.name == ".claude",
    "codex": lambda p: p.name == "sessions" and p.parent.name == ".codex",
    "cursor_ai_tracking": lambda p: p.name == "ai-tracking" and p.parent.name == ".cursor",
    "claude_dot": lambda p: p.name == ".claude" and p.is_dir(),
    "codex_dot": lambda p: p.name == ".codex" and p.is_dir(),
    "cursor_dot": lambda p: p.name == ".cursor" and p.is_dir(),
}

# Skip list : dossiers jamais scannes pour perf
SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".next",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".git",
    ".DS_Store",
    ".npm",
    ".yarn",
    ".cache",
    ".pytest_cache",
    "Library",  # macOS system dir (hors ~/Library/Logs qu'on scan specifiquement ailleurs)
    ".Trash",
    "vendor",
    "third_party",
    "bower_components",
}

MAX_DEPTH = 4


def walk_home(
    root: Path | None = None, max_depth: int = MAX_DEPTH
) -> Iterable[Path]:
    """Generator qui yield tous les paths sous `root` (defaut HOME) jusqu'a
    max_depth, en skippant les dossiers inutiles.

    Yields : chaque path directorie (pas les fichiers).
    """
    if root is None:
        root = Path.home()

    def _walk(path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            for child in path.iterdir():
                if not child.is_dir():
                    continue
                if child.name in SKIP_DIRS:
                    continue
                if child.name.startswith(".") and depth > 0 and child.name not in {
                    ".claude", ".codex", ".cursor",
                }:
                    # Skip les autres dotdirs (pour eviter .local, .config, etc.)
                    continue
                yield child
                yield from _walk(child, depth + 1)
        except (PermissionError, OSError):
            return

    yield from _walk(root, 0)


def discover_paths(root: Path | None = None) -> dict[str, list[str]]:
    """Scanne HOME et retourne un dict {collector_id: [paths absolus trouves]}.

    Exemple retour :
      {
        "claude_code": ["/Users/charles/.claude/projects"],
        "codex": ["/Users/charles/.codex/sessions"],
        "cursor_ai_tracking": [],
        ...
      }

    Les paths standard (`~/.claude/projects`, etc.) sont inclus dans le retour
    en premiere position si existants. Paths non-standard additionnels suivent.
    """
    results: dict[str, list[str]] = {
        key: [] for key in DISCOVERABLE_TARGETS
    }
    seen: dict[str, set[str]] = {key: set() for key in DISCOVERABLE_TARGETS}

    for path in walk_home(root):
        for target_id, match_fn in DISCOVERABLE_TARGETS.items():
            try:
                if match_fn(path):
                    abs_path = str(path.resolve())
                    if abs_path not in seen[target_id]:
                        results[target_id].append(abs_path)
                        seen[target_id].add(abs_path)
            except (OSError, ValueError):
                continue

    return results
