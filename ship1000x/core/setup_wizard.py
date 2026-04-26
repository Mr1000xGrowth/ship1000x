"""Setup wizard interactif — `ship1000x init`.

Guide l'utilisateur en 5 questions :
  1. Prenom / email (identifiant local)
  2. Consent ecrit pour le partage cloud (aggregated vs private)
  3. Projets auto-detectes confirmer / ajuster
  4. Frequence cron quotidienne
  5. Push S3 on/off (AWS / B2 / R2 / Garage / MinIO)
"""

from __future__ import annotations

import getpass
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt

CONSENT_TEXT = """
═══════════════════════════════════════════════════════════════════════════
  SHIP1000X — CONSENT POUR PARTAGE AGREGE
═══════════════════════════════════════════════════════════════════════════

  Ce tracker collecte UNIQUEMENT des metriques quantitatives (timestamps,
  durees, compteurs). AUCUN contenu (prompts, fichiers, diffs) ne quitte
  votre machine.

  Si tu actives le partage cloud, SEULS les `daily_rollup` agreges
  (date, projet, duree, nb events) sont pushes vers le bucket S3 que tu
  auras configure. Le contenu brut reste 100% local.

  Tu peux :
    - Refuser le partage (tout reste local)
    - Changer d'avis a tout moment via `ship1000x privacy`
    - Demander la suppression de tes donnees du bucket avec
      `ship1000x delete --confirm`

  Le tracking est individuel. Pas de scoring personnel, pas de classement.
═══════════════════════════════════════════════════════════════════════════
"""


def _detect_git_repos(scan_root: Path, max_depth: int = 3) -> list[dict[str, str]]:
    """Detecte les repos git sous un dossier parent, renvoie suggestions projects."""
    repos = []
    exclude = {"node_modules", ".venv", ".next", "dist", "build", ".git", "__pycache__"}

    def walk(path: Path, depth: int):
        if depth > max_depth or not path.exists():
            return
        if path.name in exclude:
            return
        if (path / ".git").exists():
            # Get remote
            try:
                result = subprocess.run(
                    ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                    capture_output=True, text=True, timeout=5,
                )
                remote = result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                remote = ""
            repos.append({"name": path.name, "path": str(path), "remote": remote})
            return
        try:
            for child in path.iterdir():
                if child.is_dir():
                    walk(child, depth + 1)
        except (PermissionError, OSError):
            return

    walk(scan_root, 0)
    return repos


def run_init(repo_root: Path, projects_yaml: Path, privacy_yaml: Path, console: Console) -> None:
    """Point d'entree de `ship1000x init`."""
    console.print("[bold cyan]🚀 Ship1000x — setup initial[/bold cyan]")
    console.print()

    # 1. Identite
    console.print("[bold]1/5 · Identite[/bold]")
    default_user = getpass.getuser()
    display_name = Prompt.ask("  Prenom (affiche dans le dashboard)", default=default_user.capitalize())
    email = Prompt.ask("  Email (identifiant, utilise pour dedup multi-machines)", default=f"{default_user}@local")
    console.print()

    # 2. Consent
    console.print(CONSENT_TEXT)
    consent_given = Confirm.ask(
        "[bold]2/5 · J'ai lu et j'accepte les regles de privacy[/bold]",
        default=True,
    )
    if not consent_given:
        console.print("[yellow]Annule. Rien n'a ete configure.[/yellow]")
        return
    share_cloud = Confirm.ask(
        "  Activer le push des rollups agreges vers un bucket S3 ? (peut etre change plus tard)",
        default=False,
    )
    console.print()

    # 3. Auto-detection projets + selection share level per-project
    console.print("[bold]3/5 · Detection des projets et selection du partage[/bold]")
    scan_root = repo_root.parent  # repo parent = dossier travail
    console.print(f"  Scan de : {scan_root}")
    detected = _detect_git_repos(scan_root)
    if detected:
        console.print(f"  [green]✓[/green] {len(detected)} repos detectes")
    else:
        console.print("  [yellow]Aucun repo git detecte[/yellow]")
    console.print()

    # Si l'utilisateur a active le partage cloud, on lui propose de classifier
    # chaque repo. Sinon on saute (rien ne sortira de toute facon, _default=private).
    project_share_overrides: dict[str, str] = {}
    if detected and share_cloud:
        from ship1000x.core.consent_wizard import (
            collect_detected_repos,
            prompt_share_levels,
        )

        existing_share = (yaml.safe_load(privacy_yaml.read_text()) or {}).get("share", {}) if privacy_yaml.exists() else {}
        existing_share = existing_share or {}
        existing_share.setdefault("_default", "private")

        projects = collect_detected_repos(detected)
        if Confirm.ask(
            f"  Classifier les {len(projects)} projets maintenant ? (sinon : tout reste en `private` par defaut)",
            default=True,
        ):
            project_share_overrides = prompt_share_levels(
                projects, existing_share, console, share_cloud=True
            )
        else:
            console.print(
                "  [dim]OK. Tu pourras classifier plus tard avec "
                "[cyan]ship1000x projects --select[/cyan].[/dim]"
            )
    elif detected:
        console.print(
            "  [dim]Partage cloud desactive : pas besoin de classifier "
            "(rien ne sortira de ta machine).[/dim]"
        )
    console.print(f"  Pour personnaliser les regles de classification, edite : [cyan]{projects_yaml}[/cyan]")
    console.print()

    # 4. Cron
    console.print("[bold]4/5 · Ingestion automatique[/bold]")
    cron_time = Prompt.ask(
        "  Heure de l'ingestion quotidienne (format HH:MM)",
        default="03:00",
    )
    console.print(f"  Tu pourras installer le cron avec : [cyan]ship1000x install-scheduler --time {cron_time}[/cyan]")
    console.print()

    # 5. Push S3 (si share_cloud)
    bucket = ""
    endpoint = ""
    if share_cloud:
        console.print("[bold]5/5 · Configuration du bucket S3[/bold]")
        console.print("  [dim]Compatible AWS S3, Backblaze B2, Cloudflare R2, Garage, MinIO.[/dim]")
        bucket = Prompt.ask("  Nom du bucket", default="")
        endpoint = Prompt.ask(
            "  Endpoint (laisse vide pour AWS S3, sinon URL complete)",
            default="",
        )
        console.print()
    else:
        console.print("[bold]5/5 · Pas de push cloud (mode 100% local)[/bold]")
        console.print()

    # Ecriture privacy.yaml
    if privacy_yaml.exists():
        config = yaml.safe_load(privacy_yaml.read_text()) or {}
    else:
        config = {}

    config["consent"] = {
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "user_email": email,
        "display_name": display_name,
        "version": "1.0",
        "share_cloud": share_cloud,
    }
    if share_cloud:
        config.setdefault("cloud", {})
        config["cloud"]["provider"] = "s3"
        config["cloud"]["bucket"] = bucket
        config["cloud"]["endpoint"] = endpoint
        config["cloud"]["push_enabled"] = True
        config["cloud"]["push_time"] = cron_time
        config["cloud"]["retention_days"] = 365

    # Si le user a explicitement choisi des share levels via le wizard, on les
    # applique avant la migration. Sinon `migrate_privacy_config` mettrait tous
    # les repos detectes en `aggregated` automatiquement (legacy V1).
    if project_share_overrides:
        config["share"] = project_share_overrides

    # Migration unifiee : applique sources + share._default + retention + defaults
    # pour garantir que tout est coherent quel que soit le point d'entree
    # (init initial, re-init sur yaml existant, autre version CLI). On ne passe
    # pas `detected_repos` car le wizard interactif a deja gere le `share` map.
    from ship1000x.core.config_migration import migrate_privacy_config
    repos_for_migration = None if project_share_overrides else detected
    config, _ = migrate_privacy_config(config, detected_repos=repos_for_migration)

    privacy_yaml.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True))

    console.print("[bold green]✓ Configuration enregistree[/bold green]")
    console.print(f"  privacy.yaml : {privacy_yaml}")
    console.print()
    console.print("[bold]Prochaines etapes :[/bold]")
    console.print("  1. [cyan]ship1000x ingest[/cyan]        — premiere collecte")
    console.print("  2. [cyan]ship1000x week[/cyan]          — voir ton activite 7 jours")
    console.print("  3. [cyan]ship1000x project <id>[/cyan]  — drill-down par projet")
    if share_cloud:
        console.print("  4. [cyan]ship1000x push[/cyan]          — push rollups vers bucket configure")
