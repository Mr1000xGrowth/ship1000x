"""Classifier — associe un event / session a un projet avec confidence score.

Hierarchie :
  1. cwd match glob yaml             → 0.95 (override custom)
  2. git remote match yaml           → 0.90
  3. path collection match yaml      → 0.80
  4. keywords titre                  → 0.60
  5. AUTO : .git/ parent + remote    → 0.85 (generique, marche sans yaml)
  6. AUTO : nom du dossier local     → 0.50 (pas de repo git)
  7. fallback                        → 0.0 (unclassified, rarissime avec auto)

`projects.yaml` devient OPTIONNEL : il sert a :
  - Fusionner plusieurs repos sous un meme project_id (ex: my-frontend +
    my-backend = "my-app")
  - Renommer l'affichage
  - Categoriser (produit / infra / r&d)

Sans yaml, chaque repo git est automatiquement 1 projet via son remote URL
normalise (github.com/user/repo) ou son nom de dossier si pas de remote.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass
class ProjectRule:
    id: str
    name: str
    paths: list[str]
    git_remotes: list[str]
    keywords: list[str]
    category: str
    status: str = "active"


# ----------------------------------------------------------------------
# Auto-classification : resolveur generique base sur git
# ----------------------------------------------------------------------
# Transforme n'importe quel chemin en `repo_uid` stable sans yaml :
#   /Users/x/Desktop/my-app/src/foo.ts  →  "github.com/user/my-app"
#   /Users/x/experimental-thing/        →  "local:experimental-thing"
#
# Utilise lru_cache pour eviter de re-executer `git config` 1000x pour
# le meme path dans la meme ingestion.


def _normalize_git_remote(url: str) -> str:
    """Normalise une remote URL git en identifiant canonique.

    git@github.com:user/repo.git   →  github.com/user/repo
    https://github.com/user/repo   →  github.com/user/repo
    ssh://git@gitlab.com:22/u/r.git →  gitlab.com/u/r

    Retourne "" si l'URL est vide/invalide.
    """
    if not url:
        return ""
    s = url.strip().lower()

    # ssh://git@host:port/path
    m = re.match(r"ssh://(?:[^@]+@)?([^:/]+)(?::\d+)?/(.+?)(?:\.git)?/?$", s)
    if m:
        return f"{m.group(1)}/{m.group(2).strip('/')}"

    # git@host:path.git
    m = re.match(r"[^@]+@([^:]+):(.+?)(?:\.git)?/?$", s)
    if m:
        return f"{m.group(1)}/{m.group(2).strip('/')}"

    # https://host/path.git
    m = re.match(r"https?://(?:[^@]+@)?([^/]+)/(.+?)(?:\.git)?/?$", s)
    if m:
        return f"{m.group(1)}/{m.group(2).strip('/')}"

    return s.rstrip("/")


def _safe_expanduser(path_str: str) -> Path | None:
    """`Path(path_str).expanduser()` avec catch de TOUTES les exceptions.

    Retourne None si le path est invalide (ex: `~unknownuser/foo` qui leve
    RuntimeError, path avec null bytes, etc.).
    """
    try:
        return Path(path_str).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


@lru_cache(maxsize=512)
def _find_git_root(path_str: str) -> Path | None:
    """Remonte depuis path jusqu'au premier `.git/` trouve. None si absent."""
    if not path_str:
        return None
    p = _safe_expanduser(path_str)
    if p is None:
        return None
    try:
        resolved = p.resolve()
        if resolved.exists():
            p = resolved
    except (OSError, RuntimeError):
        # Garde p non-resolu
        pass
    # Remonter au .git parent
    for _ in range(30):  # Borne dure anti-loop infinie
        try:
            if (p / ".git").exists():
                return p
        except (OSError, PermissionError, RuntimeError):
            return None
        try:
            parent = p.parent
        except (OSError, RuntimeError):
            return None
        if parent == p:
            return None
        p = parent
    return None


@lru_cache(maxsize=512)
def _git_remote_for_root(root_str: str) -> str:
    """Retourne la remote URL origin d'un repo git. "" si aucune."""
    if not root_str:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", root_str, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return ""


@lru_cache(maxsize=512)
def _first_commit_hash(root_str: str) -> str:
    """Retourne le hash du 1er commit d'un repo (ID stable sans remote)."""
    if not root_str:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", root_str, "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return lines[0][:12] if lines else ""
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return ""


def resolve_repo_uid(path: str | None) -> tuple[str, str, float]:
    """Auto-classification generique d'un path vers (project_id, display_name, conf).

    Hierarchie :
      1. path absolu remonte a un .git/ ET remote existe → ("github.com/u/r", "r", 0.85)
      2. path absolu remonte a un .git/ sans remote → ("local:<hash>", "<dir-name>", 0.75)
      3. path absolu dans un dossier top-level ~/X sans git → ("dir:X", "X", 0.55)
      4. rien → ("", "", 0.0)

    IMPORTANT : on ne traite QUE les paths absolus qui existent sur disque.
    Les paths relatifs (`memory`, `src/foo.tsx`, `charlesgautier`) sont
    IGNORES (retour "") plutot que classes en `dir:xxx`. Raison : un path
    relatif extrait d'un tool_use Claude Code se resout contre le cwd du
    tracker Python → classifications trompeuses.

    Pour l'etape 3 (dossier sans git) on utilise le PREMIER segment sous
    HOME, pas le parent direct. Ca evite les `dir:src`/`dir:memory` mais
    capture correctement les projets Cursor hors git type
    `~/Mission Control Pre-Onboarding/` → `dir:Mission Control Pre-Onboarding`.

    Le caller peut overrider via projects.yaml si besoin (cf classify_session).
    """
    if not path:
        return ("", "", 0.0)

    # Reject paths relatifs : on ne peut pas les resoudre fiablement sans
    # connaitre le cwd original de la session.
    p = _safe_expanduser(path)
    if p is None or not p.is_absolute():
        return ("", "", 0.0)

    # Le path doit pointer vers quelque chose qui existe sur disque
    try:
        if not p.exists():
            return ("", "", 0.0)
    except (OSError, RuntimeError):
        return ("", "", 0.0)

    root = _find_git_root(path)
    if root is not None:
        remote = _git_remote_for_root(str(root))
        if remote:
            uid = _normalize_git_remote(remote)
            display = uid.split("/")[-1] if "/" in uid else uid
            return (uid, display, 0.85)
        # Repo sans remote : hash du 1er commit comme ID stable
        first = _first_commit_hash(str(root))
        if first:
            return (f"local:{first}", root.name, 0.75)
        # Fallback : juste le nom du dossier
        return (f"local:{root.name}", root.name, 0.70)

    # Pas de repo git : tenter le premier segment sous HOME (projet
    # top-level sans git, typique pour Cursor / experimentations).
    # Ex: /Users/x/Mission Control Pre-Onboarding/app/layout.tsx
    #  -> segment apres HOME = "Mission Control Pre-Onboarding"
    try:
        home = Path.home().resolve()
        resolved = p.resolve()
        rel = resolved.relative_to(home)
        parts = rel.parts
        if parts:
            first_segment = parts[0]
            # Skip les dotdirs systeme (.config, .cache, .local, etc.)
            if first_segment.startswith("."):
                return ("", "", 0.0)
            # Skip les dossiers systeme macOS connus
            if first_segment in {"Library", "Applications", "Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Public"}:
                # Desktop / Documents : on descend d'1 niveau pour capturer
                # le vrai nom du projet (ex: Documents/MonProjet/src/foo)
                if len(parts) >= 2 and first_segment in {"Desktop", "Documents"}:
                    return (f"dir:{parts[1]}", parts[1], 0.55)
                return ("", "", 0.0)
            return (f"dir:{first_segment}", first_segment, 0.55)
    except (ValueError, OSError, RuntimeError):
        # RuntimeError : Path.home() peut lever 'Could not determine home directory'
        # si HOME env var est absente (rare mais possible dans certains contextes
        # cron/launchd). On degrade silencieusement en classification vide.
        pass

    return ("", "", 0.0)


class Classifier:
    def __init__(self, project_rules: list[ProjectRule]):
        self.rules = project_rules

    @classmethod
    def from_yaml_config(cls, config: dict[str, Any]) -> Classifier:
        rules = []
        for proj in config.get("projects", []):
            rules.append(ProjectRule(
                id=proj["id"],
                name=proj.get("name", proj["id"]),
                paths=proj.get("paths", []),
                git_remotes=proj.get("git_remotes", []),
                keywords=proj.get("keywords", []),
                category=proj.get("category", "other"),
                status=proj.get("status", "active"),
            ))
        return cls(rules)

    def classify_cwd(self, cwd: str | None) -> tuple[str | None, float]:
        """Match cwd contre paths des projects. Retourne (project_id, confidence)."""
        if not cwd:
            return (None, 0.0)
        for rule in self.rules:
            for pattern in rule.paths:
                if fnmatch.fnmatch(cwd, pattern):
                    return (rule.id, 0.95)
        return (None, 0.0)

    def classify_paths_collection(self, paths: list[str]) -> tuple[str | None, float]:
        """Vote majoritaire sur une liste de paths.

        Utilise quand cwd ne matche rien mais les tools touchent massivement
        un projet (ex: Claude Code dans un parent dir qui edit des fichiers
        d'un sous-projet).
        """
        if not paths:
            return (None, 0.0)

        counts: dict[str, int] = {}
        total_matched = 0
        for path in paths:
            for rule in self.rules:
                for pattern in rule.paths:
                    if fnmatch.fnmatch(path, pattern):
                        counts[rule.id] = counts.get(rule.id, 0) + 1
                        total_matched += 1
                        break

        if not counts or total_matched == 0:
            return (None, 0.0)

        best_id = max(counts, key=counts.get)
        ratio = counts[best_id] / len(paths)
        if ratio >= 0.5:
            return (best_id, 0.80)
        if ratio >= 0.2:
            return (best_id, 0.50)
        return (None, 0.0)

    def paths_distribution(self, paths: list[str]) -> dict[str, float]:
        """Retourne la repartition pondérée des paths par project_id.

        Exemple : si 60 paths matchent my-frontend et 40 matchent my-backend,
        retourne `{"my-frontend": 0.6, "my-backend": 0.4}`.

        Strategie 2-pass pour gerer paths absolus ET paths relatifs :
          1. glob match (pour paths absolus type `/Users/.../my-project/...`)
          2. substring match sur project.id et derniers segments des patterns
             (pour paths relatifs type `src/components/...` ou cwd ambigu
             comme `/Users/charlesgautier/ClaudeCode - Test OpenClaw GHL`)
        """
        counts: dict[str, int] = {}

        # Precompute les markers par rule (id + segment distinctif des patterns)
        markers_by_rule: dict[str, list[str]] = {}
        for rule in self.rules:
            markers: list[str] = [rule.id]
            for pattern in rule.paths:
                # Extrait le segment "nommant" le projet dans le glob
                # ("*/my-project/*" -> "my-project")
                for segment in pattern.split("/"):
                    if segment and segment != "*" and "*" not in segment:
                        if segment not in markers:
                            markers.append(segment)
            markers_by_rule[rule.id] = markers

        for path in paths:
            matched = False
            # Pass 1 : glob strict
            for rule in self.rules:
                if matched:
                    break
                for pattern in rule.paths:
                    if fnmatch.fnmatch(path, pattern):
                        counts[rule.id] = counts.get(rule.id, 0) + 1
                        matched = True
                        break
            if matched:
                continue
            # Pass 2 : substring fallback (paths relatifs, cwd parent)
            path_lower = path.lower()
            for rule_id, markers in markers_by_rule.items():
                if any(m.lower() in path_lower for m in markers if len(m) >= 3):
                    counts[rule_id] = counts.get(rule_id, 0) + 1
                    matched = True
                    break
            if matched:
                continue
            # Pass 3 : auto-resolve generique (.git/ parent + remote URL).
            # Indispensable pour les repos qui ne sont pas dans projects.yaml :
            # sans ce pass, un chemin comme /Users/x/Desktop/foo/src/bar.ts
            # tombe en unclassified. Avec le pass, il remonte a foo/.git/
            # et devient github.com/user/foo.
            uid, _, _conf = resolve_repo_uid(path)
            if uid:
                counts[uid] = counts.get(uid, 0) + 1

        total = sum(counts.values())
        if total == 0:
            return {}
        return {pid: n / total for pid, n in counts.items()}

    def classify_keywords(self, title: str | None) -> tuple[str | None, float]:
        """Fallback keywords sur titre de session."""
        if not title:
            return (None, 0.0)
        title_lower = title.lower()
        for rule in self.rules:
            for kw in rule.keywords:
                if kw.lower() in title_lower:
                    return (rule.id, 0.60)
        return (None, 0.0)

    def classify_session(
        self,
        cwd: str | None = None,
        paths: list[str] | None = None,
        title: str | None = None,
        git_remote: str | None = None,
    ) -> tuple[str | None, float]:
        """Classification combinee avec signaux dans l'ordre de confiance.

        Ordre :
          1-4. Match yaml (cwd / git_remote / path collection / keywords)
          5.   Auto-resolution generique (.git/ parent + remote URL)
          6.   Nom du dossier parent (fallback sans git)

        Avec l'auto-resolution, projects.yaml devient OPTIONNEL : il ne
        sert qu'a fusionner plusieurs repos en 1 projet, renommer, ou
        categoriser. Sans yaml, chaque repo = 1 projet via son remote
        normalise.
        """
        # 1. cwd exact via yaml
        pid, conf = self.classify_cwd(cwd)
        if pid:
            return (pid, conf)

        # 2. git remote via yaml
        if git_remote:
            for rule in self.rules:
                for remote_pattern in rule.git_remotes:
                    if remote_pattern in git_remote:
                        return (rule.id, 0.90)

        # 3. path collection via yaml
        pid, conf = self.classify_paths_collection(paths or [])
        if pid and conf >= 0.5:
            return (pid, conf)

        # 4. keywords titre via yaml
        pid, conf = self.classify_keywords(title)
        if pid:
            return (pid, conf)

        # 5. AUTO : resolveur generique base sur git (marche sans yaml).
        # On essaie cwd en priorite, puis le 1er path touche (plus fiable
        # que paths_distribution car un seul hit de .git/ suffit).
        for candidate in [cwd] + list(paths or []):
            if not candidate:
                continue
            uid, _, auto_conf = resolve_repo_uid(candidate)
            if uid:
                return (uid, auto_conf)

        return (None, 0.0)
