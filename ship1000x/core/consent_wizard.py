"""Consent wizard interactif — selection per-project du niveau de partage.

Utilise par `ship1000x init` (au setup initial) et `ship1000x projects --select`
(reconfiguration apres usage). Permet a l'utilisateur de classer chaque projet
detecte en `aggregated` (rollups partages cloud), `private` (local-only) ou
`disabled` (pas du tout scanne).

Source de verite : la `share` map dans `~/.config/ship1000x/privacy.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

SHARE_LEVELS = ("aggregated", "private", "disabled")

LEVEL_LABELS = {
    "aggregated": "[green]aggregated[/green]",
    "private": "[yellow]private[/yellow]",
    "disabled": "[red]disabled[/red]",
}

LEVEL_DESCRIPTIONS = {
    "aggregated": "rollups journaliers partages au cloud (durees, compteurs, jamais de contenu)",
    "private": "scanne localement mais rien ne quitte ta machine",
    "disabled": "pas du tout scanne, ignore par tous les collectors",
}


@dataclass(frozen=True)
class ProjectInfo:
    """Info per-project utilisee dans le wizard."""

    project_id: str
    detection: str  # "git_remote" | "local_git" | "dir" | "db_known"
    sample_path: str | None = None
    sessions: int = 0
    commits: int = 0


def suggest_default_level(project: ProjectInfo, share_cloud: bool) -> str:
    """Suggere un share level par defaut.

    Regle :
      - Si share_cloud=False (user solo, local-only) : tout en `private`
      - Si share_cloud=True : `aggregated` pour les projets a remote git public
        (le user a explicitement choisi de partager + le projet est deja public),
        `private` pour les projets locaux/sans remote (souvent code client/perso)
    """
    if not share_cloud:
        return "private"
    if project.detection == "git_remote":
        return "aggregated"
    return "private"


def find_unclassified_projects(
    known_project_ids: list[str],
    share_config: dict[str, str],
) -> list[str]:
    """Retourne les project_ids presents en DB mais absents du share map.

    Sert au check post-ingest : on previent l'utilisateur des nouveaux projets
    detectes pour qu'il les classifie explicitement avant le prochain push.
    `_default` ne compte pas comme une cle de projet.
    """
    known_keys = {k for k in share_config.keys() if not k.startswith("_")}
    return sorted(
        p for p in known_project_ids
        if p and not p.startswith("_") and p not in known_keys
    )


def render_projects_table(
    projects: list[ProjectInfo],
    current_share: dict[str, str],
    title: str = "Projets detectes",
) -> Table:
    """Construit une table rich des projets avec leur share level actuel."""
    table = Table(title=title, show_header=True, show_lines=False)
    table.add_column("Projet", style="cyan", no_wrap=True)
    table.add_column("Detection")
    table.add_column("Sessions", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("Partage actuel")

    default_level = current_share.get("_default", "private")
    for p in projects:
        level = current_share.get(p.project_id, default_level)
        display_level = LEVEL_LABELS.get(level, level)
        if p.project_id not in current_share:
            display_level += " [dim](herite _default)[/dim]"
        table.add_row(
            p.project_id,
            p.detection,
            str(p.sessions) if p.sessions else "-",
            str(p.commits) if p.commits else "-",
            display_level,
        )
    return table


def prompt_share_levels(
    projects: list[ProjectInfo],
    current_share: dict[str, str],
    console: Console,
    share_cloud: bool = False,
) -> dict[str, str]:
    """Boucle interactive : demande a l'utilisateur le share level de chaque projet.

    Retourne le nouveau share map (incluant `_default`). Preserve `_default`
    de current_share.
    """
    if not projects:
        console.print("[yellow]Aucun projet a configurer.[/yellow]")
        return dict(current_share)

    console.print(render_projects_table(projects, current_share))
    console.print()
    console.print(
        "[dim]Pour chaque projet, choisis : "
        + " / ".join(LEVEL_LABELS.get(level, level) for level in SHARE_LEVELS)
        + "[/dim]"
    )
    for level, desc in LEVEL_DESCRIPTIONS.items():
        console.print(f"  - {LEVEL_LABELS[level]} : {desc}")
    console.print()

    new_share = dict(current_share)
    default_level = current_share.get("_default", "private")
    new_share.setdefault("_default", default_level)

    for p in projects:
        current = current_share.get(p.project_id, default_level)
        suggested = suggest_default_level(p, share_cloud) if p.project_id not in current_share else current
        chosen = Prompt.ask(
            f"  [cyan]{p.project_id}[/cyan]",
            choices=list(SHARE_LEVELS),
            default=suggested,
            show_choices=False,
        )
        new_share[p.project_id] = chosen

    return new_share


def collect_db_projects(storage: Any) -> list[ProjectInfo]:
    """Lit la DB pour extraire les project_ids distincts avec stats basiques.

    Reutilise par `ship1000x projects --select` apres le 1er ingest. Retourne
    une liste vide si la DB n'a pas encore ete utilisee.
    """
    try:
        rows = storage.query(
            """
            SELECT project_id,
                   COUNT(DISTINCT id) AS event_count,
                   COUNT(DISTINCT CASE WHEN source = 'git' THEN id END) AS commit_count
            FROM events
            WHERE project_id IS NOT NULL AND project_id != ''
            GROUP BY project_id
            ORDER BY event_count DESC
            """,
            (),
        )
    except Exception:
        return []

    import re
    git_remote_pattern = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}/[^/]+/[^/]+$")
    projects: list[ProjectInfo] = []
    for r in rows:
        pid = r["project_id"]
        # Format canonique classifier : `host/owner/repo` lowercase pour
        # repos avec remote, `local:name` ou `dir:name` sinon.
        if pid.startswith("local:"):
            detection = "local_git"
        elif pid.startswith("dir:"):
            detection = "dir"
        elif git_remote_pattern.match(pid):
            detection = "git_remote"
        else:
            detection = "db_known"
        projects.append(
            ProjectInfo(
                project_id=pid,
                detection=detection,
                sessions=int(r["event_count"] or 0),
                commits=int(r["commit_count"] or 0),
            )
        )
    return projects


def collect_detected_repos(detected: list[dict[str, str]]) -> list[ProjectInfo]:
    """Convertit la liste retournee par setup_wizard._detect_git_repos en ProjectInfo.

    Aligne sur le format canonique du classifier (cf core/classifier.py
    `_normalize_git_remote`) pour que les project_ids des repos detectes
    matchent ceux ecrits en DB lors de l'ingest. Sinon `merge_project_lists`
    creerait des doublons cosmetiques (ex: `gh:Owner/Repo` cote scan vs
    `github.com/owner/repo` cote DB).

    Format produit :
      - Avec remote git (any host) : `<host>/<owner>/<repo>` lowercase
        (ex: `github.com/user/repo`, `gitlab.com/user/repo`)
      - Sans remote : `local:<dir-name>` (preserve le nom du dossier)
    """
    from ship1000x.core.classifier import _normalize_git_remote

    projects: list[ProjectInfo] = []
    for repo in detected:
        name = repo.get("name") or ""
        remote = repo.get("remote") or ""
        normalized = _normalize_git_remote(remote)
        if normalized:
            project_id = normalized
            detection = "git_remote"
        else:
            project_id = f"local:{name}"
            detection = "dir"
        projects.append(
            ProjectInfo(
                project_id=project_id,
                detection=detection,
                sample_path=repo.get("path"),
            )
        )
    return projects


def merge_project_lists(*lists: list[ProjectInfo]) -> list[ProjectInfo]:
    """Fusionne plusieurs listes de ProjectInfo en deduplicant sur project_id.

    En cas de doublon, garde la version avec le plus de sessions/commits
    (typiquement la version DB plutot que la detection git).
    """
    by_id: dict[str, ProjectInfo] = {}
    for lst in lists:
        for p in lst:
            existing = by_id.get(p.project_id)
            if existing is None:
                by_id[p.project_id] = p
                continue
            if (p.sessions + p.commits) > (existing.sessions + existing.commits):
                by_id[p.project_id] = p
    return sorted(by_id.values(), key=lambda x: (-x.sessions - x.commits, x.project_id))
