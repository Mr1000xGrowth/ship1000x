#!/usr/bin/env python3
"""Ship1000x — CLI entrypoint.

Local-first AI dev productivity tracker.

Usage:
    tracker ingest                      # collect from all sources
    tracker today                       # today's summary
    tracker today --compare-modes       # compare 5 active-time modes
    tracker week                        # last 7 days
    tracker project <id>                # project detail
    tracker project <id> --since 30d    # custom window
    tracker calibrate                   # personal cadence profile (P95 threshold)
    tracker init                        # interactive setup wizard
    tracker privacy                     # show privacy config
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

# Permet d'importer core/ / collectors/ depuis la racine du projet
sys.path.insert(0, str(Path(__file__).parent))

from ship1000x.core.classifier import Classifier
from ship1000x.core.storage import Storage

REPO_ROOT = Path(__file__).parent
DB_PATH = REPO_ROOT / "db" / "tracker.sqlite"
PROJECTS_CONFIG = REPO_ROOT / "config" / "projects.yaml"
PRIVACY_CONFIG = REPO_ROOT / "config" / "privacy.yaml"

console = Console()


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get_storage() -> Storage:
    storage = Storage(DB_PATH)
    storage.init_schema()
    return storage


def _get_classifier() -> Classifier:
    config = _load_yaml(PROJECTS_CONFIG)
    return Classifier.from_yaml_config(config)


def _parse_since(since: str | None) -> datetime | None:
    """Parse une duree relative : '7d', '30d', '12h'."""
    if not since:
        return None
    unit = since[-1]
    try:
        n = int(since[:-1])
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    if unit == "d":
        return now - timedelta(days=n)
    if unit == "h":
        return now - timedelta(hours=n)
    if unit == "w":
        return now - timedelta(weeks=n)
    return None


def _fmt_duration(sec: int) -> str:
    if not sec:
        return "0m"
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _get_user_email() -> str | None:
    """Lit le user_email depuis privacy.yaml (consent.user_email).

    Necessaire pour la calibration cadence (P95 personnel) et l'attribution
    des metriques unifiees. Returns None si privacy.yaml absent ou champ vide.
    """
    cfg = _load_yaml(PRIVACY_CONFIG)
    return (cfg.get("consent") or {}).get("user_email") or None


@click.group()
def cli():
    """Ship1000x — local-first AI dev productivity tracker."""
    pass


@cli.command()
@click.option("--source", default="all", help="Source specifique ou 'all'")
def ingest(source: str):
    """Collecte les events depuis toutes les sources activees."""
    storage = _get_storage()
    classifier = _get_classifier()
    privacy_config = _load_yaml(PRIVACY_CONFIG)

    sources_enabled = privacy_config.get("sources", {})

    total_stats = {"sessions_ingested": 0, "events_ingested": 0, "files_seen": 0, "skipped": 0}

    # Defaults par source : 'enabled' sauf shell/mac_system qui requierent
    # une config manuelle (EXTENDED_HISTORY, permissions pmset). Ce default
    # s'applique quand privacy.yaml n'a pas de section `sources` — cas des
    # setups via wizard < 2026-04-20 qui n'ecrivait pas cette clef.
    _DEFAULT_ENABLED = "enabled"
    _DEFAULT_DISABLED = "disabled"

    def _src_enabled(name: str, default: str = _DEFAULT_ENABLED) -> bool:
        return sources_enabled.get(name, default) == "enabled"

    if source in ("all", "claude_code") and _src_enabled("claude_code"):
        console.print("[cyan]Collecting Claude Code sessions...[/cyan]")
        from collectors import claude_code
        stats = claude_code.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats['sessions_ingested']} sessions, {stats['files_seen']} fichiers scannes")

    if source in ("all", "openclaw") and _src_enabled("openclaw"):
        console.print("[cyan]Collecting OpenClaw gateway sessions...[/cyan]")
        from collectors import openclaw
        stats = openclaw.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats['sessions_ingested']} sessions OpenClaw "
            f"({stats['files_parsed']}/{stats['files_seen']} fichiers parses)"
        )

    if source in ("all", "anthropic_usage") and _src_enabled("anthropic_usage", _DEFAULT_DISABLED):
        console.print("[cyan]Fetching Anthropic billing usage (Admin API)...[/cyan]")
        from collectors import anthropic_usage
        stats = anthropic_usage.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats['events_ingested']} events Anthropic billing "
            f"({stats['files_parsed']}/{stats['files_seen']} buckets)"
        )

    if source in ("all", "openai_usage") and _src_enabled("openai_usage", _DEFAULT_DISABLED):
        console.print("[cyan]Fetching OpenAI billing usage (Admin API)...[/cyan]")
        from collectors import openai_usage
        stats = openai_usage.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats['events_ingested']} events OpenAI billing "
            f"({stats['files_parsed']}/{stats['files_seen']} buckets)"
        )

    if source in ("all", "codex") and _src_enabled("codex"):
        console.print("[cyan]Collecting Codex sessions...[/cyan]")
        from collectors import codex
        stats = codex.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats['sessions_ingested']} sessions, {stats['files_seen']} fichiers scannes")

    # Cursor : collector deferre V1.1.
    # state.vscdb fait ~10 GB et le parsing complet (composer bubbles + tokens)
    # exigerait ~1j de dev pour gain marginal. Le collector existe (collectors/cursor.py)
    # mais n'est pas wire dans `tracker ingest` par defaut. Activable explicitement
    # via privacy.yaml: sources.cursor.enabled = true (advanced users).
    if source == "cursor":
        console.print("[yellow]Cursor collector deferre V1.1[/yellow] — see docs/COVERAGE.md")
        console.print("[dim]  Activable via privacy.yaml: sources.cursor.enabled = true[/dim]")
    elif source == "all" and _src_enabled("cursor", _DEFAULT_DISABLED):
        console.print("[cyan]Collecting Cursor (advanced opt-in)...[/cyan]")
        from collectors import cursor
        stats = cursor.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats['sessions_ingested']} commits scored, {stats['files_seen']} fichiers scannes")

    if source in ("all", "git") and _src_enabled("git"):
        console.print("[cyan]Collecting Git logs...[/cyan]")
        from collectors import git_multi
        stats = git_multi.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats['events_ingested']} commits, {stats['files_seen']} repos scannes")

    if source in ("all", "codex_sqlite") and _src_enabled("codex_sqlite"):
        console.print("[cyan]Enriching Codex threads meta...[/cyan]")
        from collectors import codex_sqlite
        stats = codex_sqlite.collect(storage, classifier, privacy_config)
        console.print(f"  → {stats.get('threads_seen', 0)} threads, {stats.get('reclassified', 0)} reclassifies")

    if source in ("all", "shell") and _src_enabled("shell", _DEFAULT_DISABLED):
        console.print("[cyan]Collecting shell history...[/cyan]")
        from collectors import shell as shell_collector
        stats = shell_collector.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats.get('events_ingested', 0)} commandes shell")

    if source in ("all", "mac_system") and _src_enabled("mac_system", _DEFAULT_DISABLED):
        console.print("[cyan]Collecting macOS pmset...[/cyan]")
        from collectors import mac_system
        stats = mac_system.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats.get('events_ingested', 0)} wake/sleep events")

    if source in ("all", "web_exports") and _src_enabled("web_exports"):
        console.print("[cyan]Collecting web exports (drop folder)...[/cyan]")
        from collectors import web_exports
        stats = web_exports.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats.get('events_ingested', 0)} conversations web")

    if source in ("all", "cline") and _src_enabled("cline"):
        console.print("[cyan]Collecting Cline tasks (Cursor/VS Code extension)...[/cyan]")
        from collectors import cline
        stats = cline.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats.get('sessions_ingested', 0)} taches Cline ingerees")

    # IMPORTANT : codex_macapp DOIT tourner AVANT codex_desktop pour que la
    # dedup (day, project) fonctionne. codex_desktop skip les (day, project)
    # deja couverts par codex_macapp (plus precis).
    if source in ("all", "codex_macapp") and _src_enabled("codex_macapp"):
        console.print("[cyan]Collecting Codex App macOS logs (~/Library/Logs/com.openai.codex)...[/cyan]")
        from collectors import codex_macapp
        stats = codex_macapp.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats.get('sessions_ingested', 0)} sessions Codex App "
            f"({stats.get('files_parsed', 0)} logs parsed, "
            f"{stats.get('events_ingested', 0)} events)"
        )

    if source in ("all", "codex_desktop") and _src_enabled("codex_desktop"):
        console.print("[cyan]Collecting Codex Desktop logs (state_5.sqlite)...[/cyan]")
        from collectors import codex_desktop
        stats = codex_desktop.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats.get('sessions_ingested', 0)} sessions Codex Desktop "
            f"(+{stats.get('skipped', 0)} (day, project) skip via codex_macapp dedup)"
        )

    console.print()
    console.print("[green]✓[/green] Ingestion terminee")
    console.print(f"  Sessions : {total_stats['sessions_ingested']}")
    console.print(f"  Events   : {total_stats['events_ingested']}")
    if total_stats["skipped"]:
        console.print(f"  Skipped  : {total_stats['skipped']} (excluded paths)")


@cli.command()
@click.option("--compare-modes", is_flag=True,
              help="Compare les 5 modes de mesure du temps actif (strict/auto P95/loose + agent IA + wall-clock)")
def today(compare_modes: bool):
    """Resume du jour : heures par projet."""
    storage = _get_storage()
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    if compare_modes:
        _print_compare_modes(storage, today_start.date().isoformat())
        return

    rows = storage.query(
        """
        SELECT
            COALESCE(project_id, 'unclassified') AS project,
            SUM(duration_sec) AS active_sec,
            COUNT(*) AS sessions,
            SUM(token_input + token_output) AS tokens,
            SUM(cost_estimated) AS cost
        FROM events
        WHERE started_at >= ?
        GROUP BY project
        ORDER BY active_sec DESC
        """,
        (today_start.isoformat(),),
    )

    if not rows:
        console.print("[yellow]Aucune activite trackee aujourd'hui.[/yellow]")
        console.print("  Lance [cyan]tracker ingest[/cyan] pour collecter les sessions.")
        return

    table = Table(title=f"Aujourd'hui ({today_start.date()})", show_header=True)
    table.add_column("Projet", style="cyan")
    table.add_column("Temps actif", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cout $", justify="right")

    total_sec = 0
    total_cost = 0.0
    for r in rows:
        total_sec += r["active_sec"] or 0
        total_cost += r["cost"] or 0.0
        table.add_row(
            r["project"],
            _fmt_duration(r["active_sec"] or 0),
            str(r["sessions"]),
            f"{r['tokens'] or 0:,}",
            f"{r['cost'] or 0:.2f}",
        )

    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{_fmt_duration(total_sec)}[/bold]",
        "",
        "",
        f"[bold]{total_cost:.2f}[/bold]",
    )

    console.print(table)


def _print_compare_modes(storage: Storage, day: str) -> None:
    """Affiche les 5 modes de mesure cote-a-cote pour 1 journee."""
    from ship1000x.core.intervals import get_daily_unified

    unified = get_daily_unified(storage, day)
    if not unified:
        console.print(f"[yellow]Aucune metrique unifiee pour {day}.[/yellow]")
        console.print("  Lance [cyan]tracker rollup --since 7d[/cyan] pour recalculer.")
        return

    threshold = unified["threshold_used_sec"]
    threshold_min = threshold / 60
    is_fallback = threshold == 5 * 60 and unified["sample_size"] < 100

    console.print()
    console.print(f"[bold]Modes compares — {day}[/bold]")
    console.print(f"[dim]({unified['sample_size']} events humains, "
                  f"{unified['sources_count']} source(s) distincte(s), "
                  f"machine={unified['machine_id']})[/dim]")
    console.print()

    table = Table(show_header=True, show_lines=False)
    table.add_column("Mode", style="cyan")
    table.add_column("Threshold", justify="right")
    table.add_column("Duree", justify="right", style="bold")
    table.add_column("Note")

    table.add_section()
    table.add_row("[dim]ACTIF HUMAIN[/dim]", "", "", "")
    table.add_row("  strict (5min)", "5.0 min",
                  _fmt_duration(unified["active_sec_strict"]),
                  "[dim]conservateur, hardcode[/dim]")
    p95_label = "auto P95"
    if is_fallback:
        p95_label += " [yellow](fallback strict)[/yellow]"
    table.add_row(f"  {p95_label}", f"{threshold_min:.1f} min",
                  _fmt_duration(unified["active_sec_p95"]),
                  "[green]applique[/green]" if not is_fallback else "[yellow]calibration en cours[/yellow]")
    table.add_row("  loose (15min)", "15.0 min",
                  _fmt_duration(unified["active_sec_loose"]),
                  "[dim]genereux[/dim]")

    table.add_section()
    table.add_row("[dim]AGENT IA (estime)[/dim]", "", "", "")
    table.add_row("  travail autonome IA", "—",
                  _fmt_duration(unified["agent_sec_estimated"]),
                  "[dim]wall - actif humain auto[/dim]")

    table.add_section()
    table.add_row("[dim]TOTAL[/dim]", "", "", "")
    table.add_row("  wall-clock", "—",
                  _fmt_duration(unified["wall_clock_sec"]),
                  "[dim]premier → dernier event[/dim]")

    console.print(table)

    # Verification arithmetique
    expected_wall = unified["active_sec_p95"] + unified["agent_sec_estimated"]
    delta = unified["wall_clock_sec"] - expected_wall
    if abs(delta) <= 1:
        console.print("[green]Verification :[/green] actif humain auto + agent IA = wall-clock ✓")
    else:
        console.print(f"[yellow]Verification :[/yellow] ecart {delta} sec entre somme et wall-clock")

    if is_fallback:
        console.print()
        console.print("[yellow]Threshold P95 non calibre[/yellow] — utilise le fallback strict (5min).")
        console.print(f"  Lance [cyan]tracker calibrate[/cyan] quand tu auras 100+ events humains "
                      f"(actuellement {unified['sample_size']}).")


@cli.command()
@click.option("--window", default=14, type=int,
              help="Fenetre de calibration en jours (defaut 14)")
@click.option("--user", default=None, help="user_email (defaut : lu depuis privacy.yaml)")
def calibrate(window: int, user: str | None):
    """Calibre le profil de cadence personnel (percentiles P50-P99 du user).

    Utilise par le mode AUTO P95 dans le calcul du temps actif unifie.
    Adaptatif : un dev calme aura un threshold ~5 min, un power user
    multi-agents un threshold ~10-15 min — chacun vu correctement.
    """
    from ship1000x.core.cadence import refresh_user_cadence

    user_email = user or _get_user_email()
    if not user_email:
        console.print("[red]user_email manquant.[/red]")
        console.print("  Defini dans config/privacy.yaml :")
        console.print("    consent:")
        console.print("      user_email: ton@email.com")
        console.print("  Ou utilise --user ton@email.com")
        return

    storage = _get_storage()
    console.print(f"[cyan]Calibration[/cyan] pour {user_email} sur {window} jours...")
    profile = refresh_user_cadence(storage, user_email, window_days=window)

    if not profile:
        console.print("[yellow]Pas assez de data pour calibrer[/yellow] (< 50 intervalles).")
        console.print("  Lance [cyan]tracker daily[/cyan] regulierement pour accumuler.")
        return

    console.print()
    table = Table(title=f"Profil cadence — {user_email}", show_header=True)
    table.add_column("Percentile", style="cyan")
    table.add_column("Valeur", justify="right", style="bold")
    table.add_column("Interpretation")

    rows = [
        ("P50 (mediane)", profile["p50"], "moitie de tes intervalles font <= ca"),
        ("P75",            profile["p75"], "75% des intervalles"),
        ("P90",            profile["p90"], "90% des intervalles"),
        ("P95",            profile["p95"], "[bold green]threshold AUTO applique[/bold green]"),
        ("P99",            profile["p99"], "vraies pauses au-dela"),
    ]
    for label, val, note in rows:
        mins = val / 60
        table.add_row(label, f"{val} sec ({mins:.1f} min)", note)
    console.print(table)

    console.print()
    console.print(f"[dim]Sample size : {profile['sample_size']} intervalles "
                  f"sur {profile['window_days']} jours[/dim]")
    console.print(f"[dim]Calcule a {profile['computed_at']}[/dim]")
    console.print()

    # Recommandations contextuelles selon le profil
    p95_min = profile["p95"] / 60
    if p95_min < 4:
        console.print("[bold]Profil[/bold] : dev intensif/concentre (intervalles courts entre prompts)")
    elif p95_min < 8:
        console.print("[bold]Profil[/bold] : dev classique (rythme regulier)")
    elif p95_min < 15:
        console.print("[bold]Profil[/bold] : power user multi-agents (intervalles longs entre interactions)")
    else:
        console.print("[bold]Profil[/bold] : sessions tres etalees (pauses cafe naturelles incluses)")

    console.print()
    console.print(f"  Le threshold P95 ({p95_min:.1f} min) sera utilise dans les modes :")
    console.print("    [cyan]tracker today --compare-modes[/cyan]")
    console.print("    [cyan]tracker rollup[/cyan] (rebuild des daily_unified)")
    console.print()
    console.print("  Override possible :")
    console.print("    [cyan]tracker today --compare-modes[/cyan] (voir les 5 modes cote-a-cote)")


@cli.command()
def week():
    """Resume 7 derniers jours."""
    storage = _get_storage()
    week_start = datetime.now() - timedelta(days=7)

    rows = storage.query(
        """
        SELECT
            COALESCE(project_id, 'unclassified') AS project,
            SUM(duration_sec) AS active_sec,
            COUNT(*) AS sessions,
            SUM(cost_estimated) AS cost
        FROM events
        WHERE started_at >= ?
        GROUP BY project
        ORDER BY active_sec DESC
        """,
        (week_start.isoformat(),),
    )

    if not rows:
        console.print("[yellow]Aucune activite trackee sur 7 jours.[/yellow]")
        return

    table = Table(title=f"7 derniers jours ({week_start.date()} → aujourd'hui)", show_header=True)
    table.add_column("Projet", style="cyan")
    table.add_column("Temps actif", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Cout $", justify="right")

    total_sec = 0
    total_cost = 0.0
    for r in rows:
        total_sec += r["active_sec"] or 0
        total_cost += r["cost"] or 0.0
        table.add_row(
            r["project"],
            _fmt_duration(r["active_sec"] or 0),
            str(r["sessions"]),
            f"{r['cost'] or 0:.2f}",
        )

    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{_fmt_duration(total_sec)}[/bold]",
        "",
        f"[bold]{total_cost:.2f}[/bold]",
    )

    console.print(table)


@cli.command()
@click.argument("project_id")
@click.option("--since", default="30d", help="Fenetre temporelle : 7d, 30d, 12h, 2w")
def project(project_id: str, since: str):
    """Drill-down sur un projet specifique."""
    storage = _get_storage()
    cutoff = _parse_since(since) or (datetime.now() - timedelta(days=30))

    rows = storage.query(
        """
        SELECT
            DATE(started_at) AS day,
            SUM(duration_sec) AS active_sec,
            COUNT(*) AS sessions,
            SUM(token_input + token_output) AS tokens,
            SUM(cost_estimated) AS cost
        FROM events
        WHERE project_id = ? AND started_at >= ?
        GROUP BY day
        ORDER BY day DESC
        """,
        (project_id, cutoff.isoformat()),
    )

    if not rows:
        console.print(f"[yellow]Aucune activite pour '{project_id}' sur {since}.[/yellow]")
        return

    table = Table(title=f"Projet {project_id} — {since}", show_header=True)
    table.add_column("Jour", style="cyan")
    table.add_column("Temps actif", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cout $", justify="right")

    total_sec = 0
    total_cost = 0.0
    for r in rows:
        total_sec += r["active_sec"] or 0
        total_cost += r["cost"] or 0.0
        table.add_row(
            r["day"],
            _fmt_duration(r["active_sec"] or 0),
            str(r["sessions"]),
            f"{r['tokens'] or 0:,}",
            f"{r['cost'] or 0:.2f}",
        )

    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{_fmt_duration(total_sec)}[/bold]",
        "",
        "",
        f"[bold]{total_cost:.2f}[/bold]",
    )

    console.print(table)


@cli.command()
@click.option(
    "--select",
    "select_mode",
    is_flag=True,
    help="Mode interactif : reconfigurer le share level (aggregated/private/disabled) par projet.",
)
def projects(select_mode: bool):
    """Liste les projets configures (et leur share level si --select)."""
    if select_mode:
        _run_projects_select()
        return

    classifier = _get_classifier()
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    share = privacy_config.get("share") or {}
    default_level = share.get("_default", "private")

    table = Table(title="Projets configures", show_header=True)
    table.add_column("ID", style="cyan")
    table.add_column("Nom")
    table.add_column("Category")
    table.add_column("Paths patterns")
    table.add_column("Partage")

    for rule in classifier.rules:
        level = share.get(rule.id, default_level)
        table.add_row(
            rule.id,
            rule.name,
            rule.category,
            ", ".join(rule.paths[:2]),
            level,
        )
    console.print(table)


def _run_projects_select() -> None:
    """Mode interactif : reconfigure le share level de chaque projet connu.

    Source des projets : DB locale (via collect_db_projects) + repos git
    detectes sous le HOME (via setup_wizard._detect_git_repos). Ecrit le
    nouveau `share` map dans privacy.yaml.
    """
    from ship1000x.core.consent_wizard import (
        collect_db_projects,
        collect_detected_repos,
        merge_project_lists,
        prompt_share_levels,
    )
    from ship1000x.core.setup_wizard import _detect_git_repos

    privacy_config = _load_yaml(PRIVACY_CONFIG)
    consent = privacy_config.get("consent") or {}
    if not consent.get("signed_at"):
        console.print(
            "[red]✗[/red] Consent non signe. Lance [cyan]tracker init[/cyan] d'abord."
        )
        return

    cloud_sync = bool(consent.get("cloud_sync", False))
    current_share = privacy_config.get("share") or {}
    current_share.setdefault("_default", "aggregated" if cloud_sync else "private")

    storage = _get_storage()
    db_projects = collect_db_projects(storage)
    scan_root = REPO_ROOT.parent
    detected_projects = collect_detected_repos(_detect_git_repos(scan_root))
    projects_list = merge_project_lists(db_projects, detected_projects)

    if not projects_list:
        console.print(
            "[yellow]Aucun projet connu. Lance [cyan]tracker ingest[/cyan] d'abord.[/yellow]"
        )
        return

    new_share = prompt_share_levels(
        projects_list, current_share, console, share_cloud=cloud_sync
    )

    privacy_config["share"] = new_share
    PRIVACY_CONFIG.write_text(
        yaml.dump(privacy_config, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )
    console.print()
    console.print(f"[green]✓[/green] Configuration mise a jour : {PRIVACY_CONFIG}")


@cli.command()
def init():
    """Setup initial interactif (consent + config + projets auto-detectes)."""
    from ship1000x.core.setup_wizard import run_init
    run_init(REPO_ROOT, PROJECTS_CONFIG, PRIVACY_CONFIG, console)


@cli.command()
@click.option(
    "--skip-push",
    is_flag=True,
    help="Ne pousse pas vers le cloud (install de test, pas de credentials S3).",
)
@click.pass_context
def setup(ctx: click.Context, skip_push: bool):
    """Installation en un appel : init (si necessaire) + ingest + rollup + push.

    Idempotent : relancer la commande est sans risque. Les etapes deja
    effectuees (consent signe) sont skippees. Utile pour onboarding
    rapide et copy-paste en un bloc sans commentaires shell.
    """
    console.print("[bold cyan]═══ tracker setup ═══[/bold cyan]")
    console.print()

    # Étape 0 — Auto-migration du privacy.yaml existant (silencieux, idempotent)
    # Corrige les sections manquantes (sources, share._default, etc.) pour les
    # installs anterieures aux fixes 2026-04-20. Les nouvelles installs sont
    # no-op (rien a migrer car wizard ecrit tout correctement).
    if PRIVACY_CONFIG.exists():
        from ship1000x.core.config_migration import run_auto_migration
        migration_changes = run_auto_migration(PRIVACY_CONFIG)
        if migration_changes:
            console.print("[yellow]⚙[/yellow]  Migration automatique de privacy.yaml :")
            for change in migration_changes:
                console.print(f"    - {change}")
            console.print()

    # Étape 1 — init si consent pas encore signe
    privacy_config = _load_yaml(PRIVACY_CONFIG) if PRIVACY_CONFIG.exists() else {}
    consent = privacy_config.get("consent") or {}
    if consent.get("signed_at"):
        console.print(
            f"[green]✓[/green] [1/4] Consent deja signe "
            f"({consent.get('user_email', '?')}) — init skip"
        )
    else:
        console.print("[cyan][1/4] Init : wizard consent + config...[/cyan]")
        ctx.invoke(init)
        console.print("[green]✓[/green] [1/4] Init OK")
    console.print()

    # Étape 2 — ingest
    console.print("[cyan][2/4] Ingest : premiere collecte multi-sources...[/cyan]")
    try:
        ctx.invoke(ingest, source="all")
        console.print("[green]✓[/green] [2/4] Ingest OK")
    except Exception as e:
        console.print(f"[red]✗[/red] [2/4] Ingest a echoue : {e}")
        console.print("[yellow]Setup stoppe. Corrige puis relance tracker setup.[/yellow]")
        return
    console.print()

    # Étape 3 — rollup
    console.print("[cyan][3/4] Rollup : agregation jour x projet x source...[/cyan]")
    try:
        ctx.invoke(rollup, since="180d")
        console.print("[green]✓[/green] [3/4] Rollup OK")
    except Exception as e:
        console.print(f"[red]✗[/red] [3/4] Rollup a echoue : {e}")
        return
    console.print()

    # Étape 4 — push (skip si --skip-push ou pas de consent partage)
    cloud = privacy_config.get("cloud") or {}
    if skip_push:
        console.print("[yellow]⊘[/yellow] [4/4] Push skip (flag --skip-push)")
    elif not (consent.get("cloud_sync") and cloud.get("push_enabled")):
        # Re-lire privacy au cas ou init vient de le mettre a jour
        privacy_config = _load_yaml(PRIVACY_CONFIG) if PRIVACY_CONFIG.exists() else {}
        consent2 = privacy_config.get("consent") or {}
        cloud2 = privacy_config.get("cloud") or {}
        if consent2.get("cloud_sync") and cloud2.get("push_enabled"):
            console.print("[cyan][4/4] Push : rollups vers cloud bucket...[/cyan]")
            try:
                ctx.invoke(push, since=None, dry_run=False)
                console.print("[green]✓[/green] [4/4] Push OK")
            except Exception as e:
                console.print(f"[red]✗[/red] [4/4] Push a echoue : {e}")
                console.print(
                    "[yellow]Verifie AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.[/yellow]"
                )
                return
        else:
            console.print(
                "[yellow]⊘[/yellow] [4/4] Push skip "
                "(cloud sync desactive dans privacy.yaml)"
            )
    else:
        console.print("[cyan][4/4] Push : rollups vers cloud bucket...[/cyan]")
        try:
            ctx.invoke(push, since=None, dry_run=False)
            console.print("[green]✓[/green] [4/4] Push OK")
        except Exception as e:
            console.print(f"[red]✗[/red] [4/4] Push a echoue : {e}")
            console.print(
                "[yellow]Verifie AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.[/yellow]"
            )
            return

    console.print()
    console.print("[bold green]═══ setup termine ═══[/bold green]")
    console.print(
        "Prochaine etape recommandee : "
        "[cyan]tracker install-scheduler[/cyan] pour automatiser le daily push."
    )


@cli.command()
def privacy():
    """Affiche l'etat de partage par projet (lecture seule V1)."""
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    consent = privacy_config.get("consent", {})
    share = privacy_config.get("share", {}) or {}
    cloud = privacy_config.get("cloud", {}) or {}

    console.print("[bold cyan]Etat privacy[/bold cyan]")
    console.print()
    if consent.get("signed_at"):
        console.print(f"  [green]✓[/green] Consent signe : {consent.get('user_email', '?')} le {consent['signed_at'][:10]}")
        console.print(f"    Cloud sync : {'[green]oui[/green]' if consent.get('cloud_sync') else '[yellow]non[/yellow]'}")
    else:
        console.print("  [yellow]⚠[/yellow]  Consent non signe. Lance [cyan]tracker init[/cyan].")
    console.print()

    console.print("[bold]Partage par projet[/bold]")
    if not share:
        console.print("  [yellow]Tous les projets sont en mode private par defaut[/yellow]")
    else:
        for project_id, level in share.items():
            if project_id == "_default":
                continue
            color = "green" if level == "aggregated" else "yellow"
            console.print(f"  [{color}]{level:12s}[/{color}]  {project_id}")
        default_level = share.get("_default", "private")
        console.print(f"  [dim]default       [/dim]  [dim]{default_level}[/dim]")
    console.print()

    console.print("[bold]Cloud bucket[/bold]")
    if cloud.get("push_enabled"):
        console.print(f"  [green]✓[/green] {cloud.get('provider', '?')} : {cloud.get('bucket', '?')}")
        console.print(f"    Push quotidien {cloud.get('push_time', '?')} UTC")
    else:
        console.print("  [yellow]Push desactive[/yellow]")

    console.print()
    console.print(f"Pour editer : [cyan]{PRIVACY_CONFIG}[/cyan]")


@cli.command()
@click.option("--since", default="30d", help="Fenetre temporelle")
@click.option("--output", "-o", default=None, help="Chemin fichier de sortie (defaut : stdout)")
def export(since: str, output: str | None):
    """Genere un rapport Markdown case study pour pitch commercial."""
    from ship1000x.exporters.markdown_report import generate_report
    storage = _get_storage()
    cutoff = _parse_since(since) or (datetime.now() - timedelta(days=30))
    report = generate_report(storage, cutoff, since_label=since)
    if output:
        Path(output).write_text(report)
        console.print(f"[green]✓[/green] Rapport ecrit : {output}")
    else:
        console.print(report)


@cli.command()
@click.option("--since", default="180d", help="Fenetre recalcul rollups")
def rollup(since: str):
    """(Re)calcule les daily_rollup agreges depuis les events."""
    from ship1000x.core.cadence import refresh_user_cadence
    from ship1000x.core.rollup import rebuild_rollups
    storage = _get_storage()
    cutoff = _parse_since(since) or (datetime.now() - timedelta(days=180))
    stats = rebuild_rollups(storage, cutoff)
    console.print(f"[green]✓[/green] Rollups : {stats['rollups_created']} lignes sur {stats['days']} jours")

    # Refresh du profil de cadence (distribution des deltas perso) — sert au
    # cap auto par-user cote dashboard. Decision Charles 2026-04-25.
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    consent = (privacy_config.get("consent") or {})
    user_email = consent.get("user_email", "unknown@local")
    profile = refresh_user_cadence(storage, user_email, window_days=14)
    if profile:
        console.print(
            f"[green]✓[/green] Cadence : p50={profile['p50']//60}min "
            f"p75={profile['p75']//60}min p90={profile['p90']//60}min "
            f"p95={profile['p95']//60}min p99={profile['p99']//60}min "
            f"(n={profile['sample_size']})"
        )
    else:
        console.print(
            "[yellow]⚠[/yellow]  Cadence non calculee : pas assez de data "
            "(< 50 transitions inter-prompts dans la fenetre)"
        )


@cli.command("backfill-machine-id")
@click.pass_context
def backfill_machine_id_cmd(ctx: click.Context):
    """Remplit `machine_id` sur les events legacy (avant V2 multi-Mac).

    Avant le 2026-04-21, la table events n'avait pas de colonne machine_id.
    Les events historiques sont stockes avec NULL → se retrouvent dans le
    bucket "unknown-machine" du dashboard.

    Cette commande fait un UPDATE sur les events de CETTE machine uniquement
    (platform.node()) : on pose l'hypothese raisonnable que les events
    locaux ont ete collectes par la machine courante. Idempotent.

    A lancer 1 fois apres upgrade V2, sur CHAQUE machine. Puis relancer
    `tracker rollup` + `tracker push` pour propager vers le dashboard.
    """
    from ship1000x.core.storage import _current_machine_id

    storage = _get_storage()
    current_machine = _current_machine_id()

    console.print(f"[bold cyan]═══ Backfill machine_id = '{current_machine}' ═══[/bold cyan]")
    console.print()

    with storage.conn() as c:
        # Compter avant
        before = c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE machine_id IS NULL"
        ).fetchone()["n"]
        console.print(f"  Events avec machine_id NULL : {before:,}")

        if before == 0:
            console.print("[green]✓[/green] Deja tout backfilled. Rien a faire.")
            return

        # UPDATE via cursor pour rowcount
        cur = c.execute(
            "UPDATE events SET machine_id = ? WHERE machine_id IS NULL",
            (current_machine,),
        )
        updated = cur.rowcount
        console.print(f"[green]✓[/green] {updated:,} events taggues avec '{current_machine}'")

    console.print()
    console.print("[bold]Prochaines etapes :[/bold]")
    console.print("  1. [cyan]tracker rollup[/cyan]              — recalcule les daily_rollup avec machine_id")
    console.print("  2. [cyan]tracker push[/cyan]                — re-upload les rollups vers S3 (ecrase les fichiers pre-V2)")
    console.print(f"  3. Dashboard → Sync → tu verras '{current_machine}' avec les vraies heures")


@cli.command()
@click.option("--since", default="180d", help="Fenetre a reclasser (ex: 30d, 90d, 365d)")
@click.pass_context
def reclassify(ctx: click.Context, since: str):
    """Reclasse les commits git historiques avec le classifier courant.

    Workflow :
      1. Reset l'offset d'ingestion git (force re-parse de tous les repos)
      2. Re-run le collector git avec le classifier ligne (real/seed/vendored/generated)
      3. Rebuild les rollups sur la fenetre demandee

    A lancer apres :
      - mise a jour du CLI (nouveaux patterns dans line_classification.yaml)
      - edition de config/line_classification.local.yaml
      - changement de regles seed_threshold
    """
    console.print("[bold cyan]═══ tracker reclassify ═══[/bold cyan]")
    console.print()

    storage = _get_storage()

    # 1. Reset TOUS les offsets (pas juste git) pour forcer re-parse de toutes
    # les sources. Sinon les events Claude Code / Codex historiques conservent
    # leur ancien project_id malgre la nouvelle classification.
    console.print("[cyan][1/3][/cyan] Reset de tous les offsets d'ingestion...")
    with storage.conn() as c:
        cur = c.execute("DELETE FROM ingestion_state")
        deleted = cur.rowcount
    console.print(f"  [green]✓[/green] {deleted} offset(s) resette(s) (toutes sources)")
    console.print()

    # 2. Re-ingest toutes les sources avec le classifier a jour
    console.print("[cyan][2/3][/cyan] Re-parse de tous les events avec le classifier...")
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    classifier = _get_classifier()
    cutoff = _parse_since(since) or (datetime.now() - timedelta(days=180))

    # On retire TOUS les events sur la fenetre (pas juste git) pour que le
    # upsert OR IGNORE re-insere tout avec le bon project_id courant.
    with storage.conn() as c:
        cur = c.execute(
            "DELETE FROM events WHERE started_at >= ?",
            (cutoff.isoformat(),),
        )
        deleted_events = cur.rowcount
    console.print(f"  [dim]→ {deleted_events} events purges pour la fenetre[/dim]")

    # Re-run toutes les sources (idem que `tracker ingest`). Les collectors
    # vont relire les fichiers source (Claude Code JSONL, Codex sessions,
    # git log) et les inserer avec le classifier V2.
    sources_enabled = privacy_config.get("sources", {})

    def _src_enabled(name: str, default: str = "enabled") -> bool:
        return sources_enabled.get(name, default) == "enabled"

    total_ingested = 0
    for collector_name, enabled_key in [
        ("claude_code", "claude_code"),
        ("openclaw", "openclaw"),
        ("anthropic_usage", "anthropic_usage"),
        ("openai_usage", "openai_usage"),
        ("codex", "codex"),
        ("cursor", "cursor"),
        ("git_multi", "git"),
        ("codex_sqlite", "codex_sqlite"),
        ("cline", "cline"),
        ("codex_macapp", "codex_macapp"),
        ("codex_desktop", "codex_desktop"),
        ("web_exports", "web_exports"),
    ]:
        if not _src_enabled(enabled_key):
            continue
        try:
            mod = __import__(f"collectors.{collector_name}", fromlist=[collector_name])
            stats = mod.collect(storage, classifier, privacy_config)
            ingested = stats.get("events_ingested", 0) + stats.get("sessions_ingested", 0)
            total_ingested += ingested
            console.print(f"  [dim]→ {collector_name}: {ingested} events/sessions[/dim]")
        except Exception as e:
            console.print(f"  [yellow]⚠[/yellow] {collector_name}: {e}")

    console.print(
        f"  [green]✓[/green] {total_ingested} events re-classifies au total"
    )
    console.print()

    # 3. Rebuild rollups
    console.print("[cyan][3/3][/cyan] Rebuild des rollups...")
    from ship1000x.core.rollup import rebuild_rollups
    stats = rebuild_rollups(storage, cutoff)
    console.print(
        f"  [green]✓[/green] Rollups : {stats['rollups_created']} lignes sur {stats['days']} jours"
    )
    console.print()

    # Resume breakdown par categorie
    with storage.conn() as c:
        r = c.execute(
            """
            SELECT
                SUM(lines_real_added) AS r,
                SUM(lines_seed_added) AS s,
                SUM(lines_vendored_added) AS v,
                SUM(lines_generated_added) AS g,
                SUM(lines_added) AS total
            FROM daily_rollup
            WHERE date >= ?
            """,
            (cutoff.date().isoformat(),),
        ).fetchone()

    total = r["total"] or 0
    if total > 0:
        console.print("[bold]Repartition des lignes ajoutees sur la fenetre :[/bold]")
        pct_real = (r["r"] or 0) * 100 / total
        pct_seed = (r["s"] or 0) * 100 / total
        pct_vend = (r["v"] or 0) * 100 / total
        pct_gen = (r["g"] or 0) * 100 / total
        console.print(f"  real      : {r['r'] or 0:>10,} ({pct_real:5.1f}%)  ← code ecrit")
        console.print(f"  seed      : {r['s'] or 0:>10,} ({pct_seed:5.1f}%)  ← imports / scaffolds")
        console.print(f"  vendored  : {r['v'] or 0:>10,} ({pct_vend:5.1f}%)  ← code tiers")
        console.print(f"  generated : {r['g'] or 0:>10,} ({pct_gen:5.1f}%)  ← lockfiles / builds")
        console.print(f"  [dim]total     : {total:>10,}[/dim]")
    console.print()
    console.print("[green]═══ reclassify termine ═══[/green]")


@cli.command()
@click.option("--since", default=None, help="Date YYYY-MM-DD (defaut : debut du mois)")
@click.option("--dry-run", is_flag=True, help="Affiche le plan sans uploader")
def push(since: str | None, dry_run: bool):
    """Push les rollups agreges vers Garage S3 (opt-in)."""
    from ship1000x.core.rollup import get_rollups_for_push
    from ship1000x.exporters.s3_push import push_to_s3

    privacy_config = _load_yaml(PRIVACY_CONFIG)
    consent = privacy_config.get("consent") or {}
    if not consent.get("signed_at"):
        console.print("[red]✗[/red] Consent non signe. Lance [cyan]tracker init[/cyan] d'abord.")
        return
    if not consent.get("cloud_sync") and not dry_run:
        console.print("[yellow]Cloud sync desactive. Rien a pousser.[/yellow]")
        return

    cloud = privacy_config.get("cloud") or {}
    if not cloud.get("push_enabled") and not dry_run:
        console.print("[yellow]cloud.push_enabled = false. Rien a pousser.[/yellow]")
        return

    storage = _get_storage()
    share = privacy_config.get("share") or {}
    rollups = get_rollups_for_push(storage, since_date=since, share_config=share)

    if not rollups:
        console.print("[yellow]Aucun rollup eligible au partage.[/yellow]")
        console.print(
            "  Lance [cyan]tracker doctor --fix[/cyan] pour auto-corriger "
            "la config `share` dans privacy.yaml."
        )
        return

    user_email = consent.get("user_email", "unknown@local")
    import platform
    machine_id = platform.node()

    try:
        result = push_to_s3(
            rollups=rollups,
            cloud_config=cloud,
            user_email=user_email,
            machine_id=machine_id,
            dry_run=dry_run,
        )
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]✗ Echec push : {e}[/red]")
        return
    except Exception as e:
        # Cas NoCredentialsError (botocore), NetworkError, etc.
        # Message humain + pointeur vers doctor --fix plutot que traceback brut.
        err_name = type(e).__name__
        if "Credentials" in err_name or "NoCredentials" in err_name:
            console.print("[red]✗[/red] Credentials AWS/Garage S3 absents.")
            console.print("  Deux options :")
            console.print(
                "  1. [cyan]tracker doctor --fix[/cyan]  — prompt interactif, "
                "ecrit dans ~/.aws/credentials (recommande)"
            )
            console.print(
                "  2. Manuel : [cyan]export AWS_ACCESS_KEY_ID=... && "
                "export AWS_SECRET_ACCESS_KEY=...[/cyan] dans ~/.zshrc"
            )
            console.print(
                "  3. Or contact your bucket admin if you use a shared "
                "Garage S3 instance."
            )
            return
        console.print(f"[red]✗ Echec push ({err_name}) : {e}[/red]")
        return

    prefix = "[DRY RUN] " if result["dry_run"] else ""
    console.print(f"[green]✓[/green] {prefix}{len(result['objects'])} objets S3 :")
    for obj in result["objects"]:
        console.print(
            f"  {obj['key']} · {obj['rollup_count']} rollups · "
            f"{obj['size_bytes']/1024:.1f} KB"
        )

    # Push aussi le profil de cadence (cap auto par-user, V6 2026-04-25)
    from ship1000x.core.cadence import get_cadence_profile
    from ship1000x.exporters.s3_push import push_cadence_to_s3
    cadence_profile = get_cadence_profile(storage, user_email)
    if cadence_profile:
        try:
            cad_result = push_cadence_to_s3(
                profile=cadence_profile,
                cloud_config=cloud,
                user_email=user_email,
                dry_run=dry_run,
            )
            if cad_result.get("uploaded") or cad_result.get("dry_run"):
                console.print(
                    f"[green]✓[/green] {prefix}Cadence : {cad_result['key']} · "
                    f"{cad_result.get('size_bytes', 0)} bytes"
                )
        except (ValueError, RuntimeError) as e:
            console.print(f"[yellow]⚠[/yellow]  Cadence push echec : {e}")


@cli.command()
def health():
    """Scan toutes les sources IA potentielles sur la machine + statut tracker."""
    from ship1000x.core.health import health_payload, scan_sources
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    sources = scan_sources(privacy_config)

    table = Table(title="Sources de tracking — etat de sante", show_lines=False)
    table.add_column("Source", style="cyan")
    table.add_column("Statut")
    table.add_column("Volume", justify="right")
    table.add_column("Items", justify="right")
    table.add_column("Derniere activite")
    table.add_column("Valeur")
    table.add_column("Notes", overflow="fold")

    status_color = {
        "tracked": "[green]✓ trace[/green]",
        "partial": "[yellow]◐ partiel[/yellow]",
        "not_tracked": "[red]✗ pas trace[/red]",
        "disabled": "[dim]○ desactive[/dim]",
    }
    value_color = {
        "high": "[bold]⭐⭐⭐[/bold]",
        "medium": "⭐⭐",
        "low": "⭐",
    }

    for s in sources:
        if not s.path_exists and s.status != "tracked":
            status_display = "[dim]absent[/dim]"
        else:
            status_display = status_color.get(s.status, s.status)

        size_display = f"{s.size_bytes / 1_048_576:.1f} MB" if s.size_bytes else "—"
        items_display = str(s.items_count) if s.items_count is not None else "—"
        last_display = s.last_modified[:10] if s.last_modified else "—"
        notes_trunc = s.notes[:80] + "..." if len(s.notes) > 80 else s.notes

        table.add_row(
            s.label,
            status_display,
            size_display,
            items_display,
            last_display,
            value_color.get(s.value, s.value),
            notes_trunc,
        )

    console.print(table)

    consent = privacy_config.get("consent") or {}
    user_email = consent.get("user_email", "unknown@local")
    payload = health_payload(user_email, sources)
    summary = payload["summary"]
    console.print(
        f"\n[bold]Resume :[/bold] {summary['tracked']} traces, "
        f"{summary['partial']} partiels, {summary['not_tracked']} non-traces, "
        f"{summary['disabled']} desactives."
    )


@cli.command("push-health")
@click.option("--dry-run", is_flag=True, help="Affiche le JSON sans uploader")
def push_health_cmd(dry_run: bool):
    """Push le scan sante des sources vers Garage S3 (s3://<bucket>/health/<user>.json)."""
    import json

    from ship1000x.core.health import health_payload, scan_sources

    privacy_config = _load_yaml(PRIVACY_CONFIG)
    consent = privacy_config.get("consent") or {}
    if not consent.get("signed_at"):
        console.print("[red]✗[/red] Consent non signe.")
        return
    if not consent.get("cloud_sync") and not dry_run:
        console.print("[yellow]Cloud sync desactive.[/yellow]")
        return

    cloud = privacy_config.get("cloud") or {}
    if not cloud.get("push_enabled") and not dry_run:
        console.print("[yellow]cloud.push_enabled = false.[/yellow]")
        return

    user_email = consent.get("user_email", "unknown@local")
    sources = scan_sources(privacy_config)
    payload = health_payload(user_email, sources)
    raw = json.dumps(payload, indent=2).encode("utf-8")

    if dry_run:
        console.print(f"[yellow][DRY RUN][/yellow] size={len(raw)} bytes")
        console.print(json.dumps(payload, indent=2))
        return

    try:
        import os

        import boto3
        from botocore.config import Config
    except ImportError:
        console.print("[red]✗ boto3 requis.[/red]")
        return

    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")

    bucket = cloud.get("bucket")
    endpoint = cloud.get("endpoint")
    region = cloud.get("region", "garage")
    import platform

    from ship1000x.exporters.s3_push import _slugify
    machine_id = platform.node()
    user_slug = user_email.replace("@", "-at-").replace(".", "-")
    machine_slug = _slugify(machine_id)
    key = f"health/{user_slug}/{machine_slug}.json"

    s3_kwargs = {"region_name": region}
    if endpoint:
        s3_kwargs["endpoint_url"] = endpoint
    s3_kwargs["config"] = Config(
        s3={"addressing_style": "path"},
        connect_timeout=15, read_timeout=30, retries={"max_attempts": 3},
    )
    client = boto3.client("s3", **s3_kwargs)
    client.put_object(
        Bucket=bucket, Key=key, Body=raw,
        ContentType="application/json",
    )
    console.print(f"[green]✓[/green] Health pushe : s3://{bucket}/{key} ({len(raw)} B)")


@cli.command()
@click.pass_context
def daily(ctx: click.Context):
    """Pipeline quotidien : ingest + rollup + push. Utilise par le cron launchd."""
    console.print("[cyan][daily][/cyan] Ingest...")
    ctx.invoke(ingest, source="all")
    console.print("[cyan][daily][/cyan] Rollup...")
    ctx.invoke(rollup, since="180d")
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    consent = privacy_config.get("consent") or {}
    cloud = privacy_config.get("cloud") or {}

    # Avant le push, signaler les projets nouveaux apparus en DB sans entree
    # explicite dans `share` map. Ils heritent de `_default` (private par
    # defaut → safe), mais on previent l'utilisateur pour qu'il les classifie
    # consciemment via `tracker projects --select`. En mode cron (non-TTY)
    # on log juste le warning, pas de prompt bloquant.
    if consent.get("cloud_sync"):
        from ship1000x.core.consent_wizard import (
            collect_db_projects,
            find_unclassified_projects,
        )
        storage = _get_storage()
        db_projects = collect_db_projects(storage)
        share_config = privacy_config.get("share") or {}
        unclassified = find_unclassified_projects(
            [p.project_id for p in db_projects], share_config
        )
        if unclassified:
            default_level = share_config.get("_default", "private")
            console.print(
                f"[yellow]⚠[/yellow]  [daily] {len(unclassified)} projet(s) non-classifie(s) "
                f"dans privacy.yaml (heritent `_default` = {default_level}) :"
            )
            for pid in unclassified[:5]:
                console.print(f"    - {pid}")
            if len(unclassified) > 5:
                console.print(f"    ... et {len(unclassified) - 5} autres")
            console.print(
                "    [dim]Lance [cyan]tracker projects --select[/cyan] "
                "pour les classifier explicitement.[/dim]"
            )

    if consent.get("cloud_sync") and cloud.get("push_enabled"):
        console.print("[cyan][daily][/cyan] Push rollups...")
        ctx.invoke(push, since=None, dry_run=False)
        console.print("[cyan][daily][/cyan] Push insights...")
        ctx.invoke(push_insights_cmd, since="30d", tjm=None, value=None, dry_run=False)
        console.print("[cyan][daily][/cyan] Push health scan...")
        ctx.invoke(push_health_cmd, dry_run=False)
    else:
        console.print("[dim][daily][/dim] Push skip (partage desactive)")


@cli.command("install-scheduler")
@click.option("--time", "time_str", default="03:00", help="Heure HH:MM (defaut 03:00)")
def install_scheduler_cmd(time_str: str):
    """Installe le cron launchd pour tracker daily (3h du matin par defaut)."""
    from ship1000x.core.scheduler import install as scheduler_install
    try:
        hour, minute = map(int, time_str.split(":"))
    except ValueError:
        console.print(f"[red]✗[/red] Format invalide : {time_str}. Attendu HH:MM")
        return

    try:
        plist = scheduler_install(REPO_ROOT, hour, minute)
        console.print(f"[green]✓[/green] Scheduler installe : {plist}")
        console.print(f"  Prochain run : chaque jour a {time_str}")
        console.print(f"  Logs : {REPO_ROOT}/db/cron.log et cron.err.log")
    except RuntimeError as e:
        console.print(f"[red]✗[/red] {e}")


@cli.command("uninstall-scheduler")
def uninstall_scheduler_cmd():
    """Desinstalle le cron launchd."""
    from ship1000x.core.scheduler import uninstall as scheduler_uninstall
    if scheduler_uninstall():
        console.print("[green]✓[/green] Scheduler desinstalle")
    else:
        console.print("[yellow]Aucun scheduler installe[/yellow]")


@cli.command("scheduler-status")
def scheduler_status_cmd():
    """Etat du cron launchd."""
    from ship1000x.core.scheduler import status as sched_status
    s = sched_status()
    if not s["installed"]:
        console.print("[yellow]Scheduler non installe[/yellow]")
        console.print("  Installe avec : [cyan]tracker install-scheduler[/cyan]")
        return
    console.print(f"[green]✓[/green] Plist : {s['plist_path']}")
    console.print(f"  Loaded : {'[green]oui[/green]' if s['loaded'] else '[yellow]non[/yellow]'}")


@cli.command()
@click.option("--confirm", is_flag=True, help="Confirme la suppression (requis)")
@click.option("--keep-cloud", is_flag=True, help="Ne supprime PAS les rollups cloud")
def delete(confirm: bool, keep_cloud: bool):
    """Supprime toutes les donnees locales + rollups cloud (IRREVERSIBLE)."""
    if not confirm:
        console.print("[yellow]Cette commande supprime :[/yellow]")
        console.print("  - La DB locale (db/tracker.sqlite)")
        console.print("  - Les logs cron")
        console.print("  - Tous tes rollups dans le cloud bucket (sauf si --keep-cloud)")
        console.print("")
        console.print("Relance avec [cyan]--confirm[/cyan] pour executer.")
        return

    if DB_PATH.exists():
        DB_PATH.unlink()
        console.print(f"[green]✓[/green] DB supprimee : {DB_PATH}")
    for suffix in ("-wal", "-shm"):
        p = DB_PATH.with_suffix(f".sqlite{suffix}")
        if p.exists():
            p.unlink()

    if not keep_cloud:
        privacy_config = _load_yaml(PRIVACY_CONFIG)
        consent = privacy_config.get("consent") or {}
        cloud = privacy_config.get("cloud") or {}
        user_email = consent.get("user_email")
        bucket = cloud.get("bucket")
        endpoint = cloud.get("endpoint")

        if user_email and bucket and endpoint:
            try:
                import os

                import boto3
                from botocore.config import Config
                os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
                os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
                client = boto3.client(
                    "s3",
                    endpoint_url=endpoint,
                    region_name=cloud.get("region", "garage"),
                    config=Config(s3={"addressing_style": "path"}, connect_timeout=15, read_timeout=30),
                )
                user_slug = user_email.replace("@", "-at-").replace(".", "-")
                r = client.list_objects_v2(Bucket=bucket, Prefix="rollups/")
                deleted = 0
                for obj in r.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith(f"/{user_slug}.jsonl.gz"):
                        client.delete_object(Bucket=bucket, Key=key)
                        deleted += 1
                console.print(f"[green]✓[/green] {deleted} rollups cloud supprimes")
            except Exception as e:
                console.print(f"[yellow]⚠  Echec suppression cloud : {e}[/yellow]")
                console.print("  Supprime manuellement via : /garage bucket info")
        else:
            console.print("[dim]Pas de config cloud trouvee, skip suppression cloud[/dim]")

    console.print("[bold green]Donnees supprimees.[/bold green]")


@cli.command()
def status():
    """Etat du tracker : DB, derniere ingestion, volume."""
    storage = _get_storage()
    total_events = storage.query("SELECT COUNT(*) AS n FROM events")[0]["n"]
    total_sessions = storage.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"]
    db_size_mb = DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0

    last = storage.query(
        "SELECT MAX(last_ingested_at) AS last FROM ingestion_state"
    )
    last_ts = last[0]["last"] if last else None

    console.print("[bold]Ship1000x[/bold]")
    console.print(f"  DB path         : {DB_PATH}")
    console.print(f"  DB size         : {db_size_mb:.2f} MB")
    console.print(f"  Events          : {total_events:,}")
    console.print(f"  Sessions        : {total_sessions:,}")
    console.print(f"  Last ingestion  : {last_ts or 'jamais'}")


# ───────────────────────────────────────────────────────────────────────
# Insights commands (D1-D6)
# ───────────────────────────────────────────────────────────────────────

def _fmt_num(n: float | int | None, unit: str = "") -> str:
    if n is None:
        return "—"
    if isinstance(n, float):
        if abs(n) >= 1000:
            return f"{n:,.0f}{unit}".replace(",", " ")
        if abs(n) >= 10:
            return f"{n:.1f}{unit}"
        return f"{n:.2f}{unit}"
    return f"{n:,}{unit}".replace(",", " ")


def _parse_since_days(since: str) -> int:
    """Parse '30d' -> 30 (jours). Tolerant."""
    if not since:
        return 30
    unit = since[-1].lower()
    try:
        n = int(since[:-1])
    except ValueError:
        return 30
    if unit == "d":
        return n
    if unit == "w":
        return n * 7
    if unit == "h":
        return max(1, n // 24)
    return 30


@cli.command()
@click.option("--since", default="30d", help="Fenetre ex: 7d, 30d, 90d, 2w")
@click.option("--project", default=None, help="Filtre par project_id (optionnel)")
def insights(since: str, project: str | None):
    """Vue synthetique : overview + ratios + multiplicateur + signaux."""
    from ship1000x.insights.engine import compute_overview, make_window
    from ship1000x.insights.multiplier import compute_multiplier
    from ship1000x.insights.signals import compute_all_signals

    days = _parse_since_days(since)
    window = make_window(since_days=days, project=project)
    storage = _get_storage()

    overview = compute_overview(storage, window)
    mult = compute_multiplier(storage, window)
    signals = compute_all_signals(storage, window)

    t = overview["totals"]
    r = overview["ratios"]
    title = f"Insights {project or 'global'} | {since}"
    console.print()
    console.print(f"[bold cyan]═══ {title} ═══[/bold cyan]")
    console.print(f"  {t['active_hours']:.1f}h actives · {t['typed']} typed · "
                  f"{t['commits']} commits · +{t['lines_added']:,} lignes · ${t['cost']:.2f}"
                  .replace(",", " "))
    console.print()

    # Efficience
    console.print("[bold]Efficience IA-native[/bold]")
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="white")
    table.add_column(justify="right", style="cyan")
    table.add_column(style="dim")
    table.add_row("Output", _fmt_num(r["lines_per_hour"]), "lignes / h active")
    table.add_row("Prompts humain", _fmt_num(r["typed_per_hour"]), "typed / h")
    table.add_row("Tokens brasses", _fmt_num(r["tokens_per_hour"]), "tokens / h")
    table.add_row("Commits", _fmt_num(r["commits_per_hour"]), "commits / h")
    table.add_row("Lignes / prompt", _fmt_num(r["lines_per_typed"]), "amplification par prompt")
    table.add_row("Tool / typed", _fmt_num(r["tool_per_typed"]), "outils par prompt (agent mode)")
    console.print(table)
    console.print()

    # Multiplicateur
    console.print("[bold]Multiplicateur IA-native[/bold]")
    out = mult["output"]
    v = mult["value"]
    c = mult["cost"]
    console.print(f"  Facteur vs senior : [cyan]x{out['factor_vs_senior_mid']}[/cyan] "
                  f"(range x{out['factor_vs_senior_low']}-x{out['factor_vs_senior_high']}) "
                  f"vs {out['benchmark_senior_low']}-{out['benchmark_senior_high']} lignes/h sans IA")
    console.print(f"  Temps converti   : {v['active_hours']}h = {v['days_equivalent']}j · "
                  f"TJM equivalent [cyan]{v['tjm_equivalent_eur']:,.0f} EUR[/cyan]"
                  .replace(",", " "))
    if v["value_time_ratio"]:
        console.print(f"  Valeur produit   : {v['value_produit_eur']:,.0f} EUR · "
                      f"ratio valeur/temps [cyan]x{v['value_time_ratio']}[/cyan]"
                      .replace(",", " "))
    console.print(f"  Cout IA          : ${c['total_usd']} · ${c['per_commit_usd'] or 0:.2f}/commit · "
                  f"${c['per_line_net_usd'] or 0:.4f}/ligne nette")
    console.print()

    # Signaux
    if signals:
        console.print("[bold]Signaux[/bold]")
        for s in signals:
            icon = {"critical": "[red]⚠[/red]", "warning": "[yellow]⚠[/yellow]", "info": "[cyan]ℹ[/cyan]"}.get(s["level"], "·")
            console.print(f"  {icon} [{s['level']}] {s['description']}")
    else:
        console.print("[bold]Signaux[/bold]  [green]✓ Rien a signaler[/green]")
    console.print()

    # Trust Score per source + global composite
    from ship1000x.insights.trust_score import (
        compute_global_score,
        get_all_source_scores,
        get_score_label,
    )

    user_email = _get_user_email()
    source_scores = get_all_source_scores(storage, window_days=days)
    global_score = compute_global_score(storage, window_days=days, user_email=user_email)

    if source_scores:
        console.print("[bold]Trust Score[/bold]  [dim](confidence per metric, see docs/TRUST_SCORE.md)[/dim]")
        ts_table = Table(show_header=True, box=None, padding=(0, 2))
        ts_table.add_column("Source", style="cyan")
        ts_table.add_column("Events", justify="right")
        ts_table.add_column("Score", justify="right")
        ts_table.add_column("Level")
        for src, info in sorted(source_scores.items(), key=lambda x: -x[1]["score"]):
            label, color = get_score_label(info["score"])
            ts_table.add_row(
                src,
                f"{info['event_count']:,}".replace(",", " "),
                f"{info['score']}/100",
                f"[{color}]{label}[/{color}]",
            )
        ts_table.add_section()
        glabel, gcolor = get_score_label(global_score["score"])
        ts_table.add_row(
            "[bold]GLOBAL[/bold]",
            "",
            f"[bold]{global_score['score']}/100[/bold]",
            f"[bold {gcolor}]{glabel}[/bold {gcolor}]",
        )
        console.print(ts_table)

        # Bonus / penalty breakdown si pertinent
        details: list[str] = []
        details.extend(global_score["bonus_reasons"])
        details.extend(global_score["penalty_reasons"])
        if details:
            console.print(f"  [dim]composite: base {global_score['base']} "
                          f"{' '.join(details)}[/dim]")
        console.print()


@cli.command()
@click.option("--since", default="30d", help="Fenetre ex: 7d, 30d")
@click.option("--project", default=None)
def ratios(since: str, project: str | None):
    """Focus ratios efficience detailles."""
    from ship1000x.insights.engine import compute_overview, make_window
    days = _parse_since_days(since)
    window = make_window(since_days=days, project=project)
    storage = _get_storage()
    overview = compute_overview(storage, window)
    t = overview["totals"]
    r = overview["ratios"]

    title = f"Ratios {project or 'global'} | {since} | {t['active_hours']:.1f}h"
    table = Table(title=title, show_header=True)
    table.add_column("Metrique", style="cyan")
    table.add_column("Valeur", justify="right")
    table.add_column("Unite", style="dim")
    table.add_row("Output", _fmt_num(r["lines_per_hour"]), "lignes / h")
    table.add_row("Prompts", _fmt_num(r["typed_per_hour"]), "typed / h")
    table.add_row("Tokens", _fmt_num(r["tokens_per_hour"]), "tokens / h")
    table.add_row("Commits", _fmt_num(r["commits_per_hour"]), "commits / h")
    table.add_row("Lignes/prompt", _fmt_num(r["lines_per_typed"]), "lignes / typed")
    table.add_row("Tool/typed", _fmt_num(r["tool_per_typed"]), "tool calls / typed")
    table.add_row(
        "Ratio approvals",
        f"{(r['approval_ratio'] or 0) * 100:.1f}%" if r["approval_ratio"] else "—",
        "approvals / (typed+approval)",
    )
    table.add_row("Cout / commit", f"${r['cost_per_commit']:.2f}" if r["cost_per_commit"] else "—", "USD")
    table.add_row("Cout / ligne nette", f"${r['cost_per_line_net']:.4f}" if r["cost_per_line_net"] else "—", "USD")
    table.add_row("Cout / heure", f"${r['cost_per_hour']:.2f}" if r["cost_per_hour"] else "—", "USD")
    console.print(table)


@cli.command()
@click.option("--since", default="30d")
@click.option("--project", default=None)
@click.option("--tjm", default=None, type=float, help="TJM senior EUR/jour (defaut : benchmark)")
@click.option("--value", default=None, type=float, help="Valeur produit livre EUR (defaut : benchmark)")
def multiplier(since: str, project: str | None, tjm: float | None, value: float | None):
    """Calcule les facteurs multiplicateurs IA-native (pitch commercial)."""
    from ship1000x.insights.engine import make_window
    from ship1000x.insights.multiplier import compute_multiplier
    days = _parse_since_days(since)
    window = make_window(since_days=days, project=project)
    storage = _get_storage()
    m = compute_multiplier(storage, window, tjm_eur_per_day=tjm, value_produit_eur=value)
    out = m["output"]
    v = m["value"]
    c = m["cost"]

    console.print()
    console.print(f"[bold cyan]═══ Multiplicateur IA-native {project or 'global'} | {since} ═══[/bold cyan]")
    console.print()
    console.print("[bold]Production[/bold]")
    console.print(f"  Output reel      : {out['lines_per_hour']} lignes/h")
    console.print(f"  Benchmark senior : {out['benchmark_senior_low']}-{out['benchmark_senior_high']} lignes/h (sans IA)")
    console.print(f"  Facteur          : [cyan]x{out['factor_vs_senior_low']} → x{out['factor_vs_senior_high']}[/cyan] "
                  f"(mid x{out['factor_vs_senior_mid']})")
    console.print()
    console.print(f"[bold]Valeur temps (TJM {m['inputs']['tjm_eur_per_day']} EUR/j, {m['inputs']['workday_hours']}h/j)[/bold]")
    console.print(f"  Temps actif      : {v['active_hours']}h = {v['days_equivalent']}j-equivalents")
    console.print(f"  Valeur TJM       : {v['tjm_equivalent_eur']:,.0f} EUR".replace(",", " "))
    if v["value_time_ratio"]:
        console.print(f"  Valeur produit   : {v['value_produit_eur']:,.0f} EUR (benchmark agence Tier-1)".replace(",", " "))
        console.print(f"  Ratio v/t        : [cyan]x{v['value_time_ratio']}[/cyan] "
                      f"(valeur produit / cout-temps senior)")
    console.print()
    console.print("[bold]Cout IA (LLM)[/bold]")
    console.print(f"  Total            : ${c['total_usd']}")
    console.print(f"  Par heure        : ${c['per_hour_usd'] or 0:.2f}/h")
    console.print(f"  Par commit       : ${c['per_commit_usd'] or 0:.2f}")
    console.print(f"  Par ligne nette  : ${c['per_line_net_usd'] or 0:.4f}")


@cli.command()
@click.option("--since", default="30d")
@click.option("--project", default=None)
def signals(since: str, project: str | None):
    """Alertes actives : burnout, derives projet, blocages."""
    from ship1000x.insights.engine import make_window
    from ship1000x.insights.signals import compute_all_signals
    days = _parse_since_days(since)
    window = make_window(since_days=days, project=project)
    storage = _get_storage()
    sigs = compute_all_signals(storage, window)

    if not sigs:
        console.print("[green]✓ Aucun signal actif[/green]")
        return

    for s in sigs:
        icon = {"critical": "[red]⚠ CRIT[/red]", "warning": "[yellow]⚠ WARN[/yellow]", "info": "[cyan]ℹ INFO[/cyan]"}.get(s["level"], "·")
        console.print()
        console.print(f"{icon} [{s['category']}/{s['type']}] confidence={s['confidence']}")
        console.print(f"  {s['description']}")
        if s.get("data"):
            import json
            console.print(f"  [dim]data : {json.dumps(s['data'], default=str)[:200]}[/dim]")


@cli.command()
@click.option("--since", default="30d")
@click.option("--project", default=None)
def profile(since: str, project: str | None):
    """Profil d'usage individuel : heatmap, sessions, journees mono-tache."""
    from ship1000x.insights.engine import make_window
    from ship1000x.insights.profile import compute_profile
    days = _parse_since_days(since)
    window = make_window(since_days=days, project=project)
    storage = _get_storage()
    p = compute_profile(storage, window)

    # Heatmap compacte : intensite par (jour_semaine, heure)
    dow_labels = ["Dim", "Lun", "Mar", "Mer", "Jeu", "Ven", "Sam"]
    console.print()
    console.print(f"[bold cyan]═══ Profil d'usage {project or 'global'} | {since} ═══[/bold cyan]")
    console.print()
    console.print(f"[bold]Couverture[/bold]  {p['coverage']['active_days']}/{p['coverage']['window_days']} jours actifs "
                  f"({p['coverage']['active_ratio']*100:.0f}%)")
    console.print()

    # Heatmap ASCII
    console.print("[bold]Heatmap 7x24 (intensite)[/bold]")
    max_sec = max(
        (s for day_hours in p["heatmap_dow_hour"].values() for s in day_hours.values()),
        default=1,
    )
    levels = " ░▒▓█"
    header = "       " + " ".join(f"{h:02}" for h in range(24))
    console.print(f"[dim]{header}[/dim]")
    for dow in [1, 2, 3, 4, 5, 6, 0]:  # Lun-Dim (Mo-Su)
        line = f"  {dow_labels[dow]}  "
        for h in range(24):
            s = p["heatmap_dow_hour"][dow][h]
            if max_sec == 0:
                idx = 0
            else:
                idx = min(len(levels) - 1, int((s / max_sec) * (len(levels) - 1)))
            line += " " + levels[idx] + " "
        console.print(line)
    console.print()

    # Sessions
    b = p["session_duration_buckets"]
    total_sess = sum(b.values())
    console.print(f"[bold]Distribution duree sessions[/bold] ({total_sess} sessions)")
    console.print(f"  < 1h    : {b['lt_1h']}")
    console.print(f"  1-3h    : {b['1_3h']}")
    console.print(f"  3-6h    : {b['3_6h']}")
    console.print(f"  > 6h    : {b['gt_6h']}  (focus long)")
    console.print()

    # Mono/multi
    s = p["switch_days"]
    if (s["mono"] + s["multi"]) > 0:
        mono_pct = s["mono"] / (s["mono"] + s["multi"]) * 100
        console.print(f"[bold]Journees mono-tache[/bold] : {s['mono']}/{s['mono']+s['multi']} ({mono_pct:.0f}%)")
        console.print("  (un projet > 70% du temps du jour)")
        console.print()

    # Wordcount
    wc = p["wordcount_buckets"]
    total_wc = sum(wc.values())
    if total_wc:
        console.print(f"[bold]Style de formulation[/bold] ({total_wc} sessions analysees)")
        console.print(f"  Tape court (<50 mots)   : {wc['typed_short']} ({wc['typed_short']/total_wc*100:.0f}%)")
        console.print(f"  Mixte (50-200 mots)     : {wc['mixed']} ({wc['mixed']/total_wc*100:.0f}%)")
        console.print(f"  Dictee vocale (>200)    : {wc['voice_dictation']} ({wc['voice_dictation']/total_wc*100:.0f}%)")


@cli.command("push-insights")
@click.option("--since", default="30d", help="Fenetre analyse (defaut 30j)")
@click.option("--tjm", default=None, type=float)
@click.option("--value", default=None, type=float)
@click.option("--dry-run", is_flag=True)
def push_insights_cmd(since: str, tjm: float | None, value: float | None, dry_run: bool):
    """Compute insights and push JSON to Garage S3 (advanced — for optional cloud sync)."""
    from ship1000x.exporters.insights_push import build_insights_payload, push_insights_to_s3
    from ship1000x.insights.engine import make_window

    privacy_config = _load_yaml(PRIVACY_CONFIG)
    consent = privacy_config.get("consent") or {}
    if not consent.get("signed_at"):
        console.print("[red]✗[/red] Consent non signe. Lance [cyan]tracker init[/cyan] d'abord.")
        return
    if not consent.get("cloud_sync") and not dry_run:
        console.print("[yellow]Cloud sync desactive. Rien a pousser.[/yellow]")
        return
    cloud = privacy_config.get("cloud") or {}
    if not cloud.get("push_enabled") and not dry_run:
        console.print("[yellow]cloud.push_enabled = false. Rien a pousser.[/yellow]")
        return

    days = _parse_since_days(since)
    window = make_window(since_days=days)
    storage = _get_storage()
    user_email = consent.get("user_email", "unknown@local")
    import platform
    machine_id = platform.node()

    payload = build_insights_payload(
        storage, window, user_email, machine_id,
        tjm_eur_per_day=tjm, value_produit_eur=value,
    )
    try:
        result = push_insights_to_s3(
            payload, cloud, user_email, machine_id=machine_id, dry_run=dry_run
        )
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]✗ Echec push insights : {e}[/red]")
        return

    prefix = "[DRY RUN] " if result["dry_run"] else ""
    console.print(f"[green]✓[/green] {prefix}insights : {result['key']} ({result['size_bytes']/1024:.1f} KB)")
    console.print(f"  Projets : {len(payload['by_project'])} · Signaux : {len(payload['global']['signals'])}")


@cli.command()
@click.argument("project_a")
@click.argument("project_b")
@click.option("--since", default="30d")
def compare(project_a: str, project_b: str, since: str):
    """Compare 2 projets sur la meme fenetre temporelle."""
    from ship1000x.insights.compare import compare_projects
    days = _parse_since_days(since)
    storage = _get_storage()
    c = compare_projects(storage, project_a, project_b, since_days=days)

    table = Table(title=f"Comparaison {project_a} vs {project_b} | {since}", show_header=True)
    table.add_column("Metrique", style="cyan")
    table.add_column(project_a, justify="right")
    table.add_column(project_b, justify="right")
    table.add_column("Delta", justify="right", style="dim")

    def _row(label, a, b, fmt="{:.1f}"):
        if a is None or b is None:
            table.add_row(label, "—", "—", "—")
            return
        delta_pct = ((a - b) / b * 100) if b else None
        delta_str = f"{delta_pct:+.0f}%" if delta_pct is not None else ""
        table.add_row(label, fmt.format(a), fmt.format(b), delta_str)

    ta = c["a"]["totals"]
    tb = c["b"]["totals"]
    ra = c["a"]["ratios"]
    rb = c["b"]["ratios"]

    _row("Heures actives", ta["active_hours"], tb["active_hours"])
    _row("Typed prompts", ta["typed"], tb["typed"], "{:.0f}")
    _row("Commits", ta["commits"], tb["commits"], "{:.0f}")
    _row("Lignes ajoutees", ta["lines_added"], tb["lines_added"], "{:.0f}")
    _row("Cout ($)", ta["cost"], tb["cost"], "${:.2f}")
    _row("Lignes/h", ra["lines_per_hour"], rb["lines_per_hour"])
    _row("Prompts/h", ra["typed_per_hour"], rb["typed_per_hour"])
    _row("Lignes/prompt", ra["lines_per_typed"], rb["lines_per_typed"])
    _row("Tool/prompt", ra["tool_per_typed"], rb["tool_per_typed"])
    console.print(table)


@cli.command()
@click.option("--project", default=None)
@click.option("--window", "window_days", default=7, type=int, help="Taille fenetre en jours")
@click.option("--offset", "offset_days", default=7, type=int, help="Decalage fenetre precedente")
def trend(project: str | None, window_days: int, offset_days: int):
    """Compare fenetre actuelle vs precedente (ex: semaine vs semaine)."""
    from ship1000x.insights.compare import compare_periods
    storage = _get_storage()
    c = compare_periods(storage, project, window_days=window_days, offset_days=offset_days)
    t_cur = c["current"]["totals"]
    t_prev = c["previous"]["totals"]
    d = c["deltas"]

    console.print()
    console.print(f"[bold cyan]═══ Tendance {project or 'global'} "
                  f"({window_days}j vs {window_days}j -{offset_days}j) ═══[/bold cyan]")
    console.print()

    def _fmt_delta(delta):
        if delta["pct"] is None:
            return "—"
        sign = "+" if delta["abs"] > 0 else ""
        color = "green" if delta["direction"] == "up" else ("red" if delta["direction"] == "down" else "dim")
        return f"[{color}]{sign}{delta['pct']:.0f}%[/{color}]"

    table = Table(show_header=True)
    table.add_column("Metrique", style="cyan")
    table.add_column("Actuel", justify="right")
    table.add_column("Precedent", justify="right")
    table.add_column("Delta", justify="right")
    table.add_row("Heures", f"{t_cur['active_hours']:.1f}h", f"{t_prev['active_hours']:.1f}h", _fmt_delta(d["active_hours"]))
    table.add_row("Typed", f"{t_cur['typed']}", f"{t_prev['typed']}", _fmt_delta(d["typed"]))
    table.add_row("Commits", f"{t_cur['commits']}", f"{t_prev['commits']}", _fmt_delta(d["commits"]))
    table.add_row("Lignes+", f"{t_cur['lines_added']:,}".replace(",", " "), f"{t_prev['lines_added']:,}".replace(",", " "), _fmt_delta(d["lines_added"]))
    table.add_row("Cout", f"${t_cur['cost']:.2f}", f"${t_prev['cost']:.2f}", _fmt_delta(d["cost"]))
    r_cur = c["current"]["ratios"]
    r_prev = c["previous"]["ratios"]
    if r_cur.get("lines_per_hour") and r_prev.get("lines_per_hour"):
        table.add_row("Lignes/h", f"{r_cur['lines_per_hour']:.0f}", f"{r_prev['lines_per_hour']:.0f}", _fmt_delta(d["lines_per_hour"]))
    if r_cur.get("tool_per_typed") and r_prev.get("tool_per_typed"):
        table.add_row("Tool/typed", f"{r_cur['tool_per_typed']:.1f}", f"{r_prev['tool_per_typed']:.1f}", _fmt_delta(d["tool_per_typed"]))
    console.print(table)


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def drop(path: Path):
    """Ingeste un export Claude.ai (ZIP) / ChatGPT (JSON) / dossier contenant plusieurs."""
    from collectors import web_exports
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    storage = _get_storage()
    classifier = _get_classifier()
    stats = web_exports.ingest_path(storage, classifier, path, privacy_config)
    console.print(f"[green]✓[/green] {stats['events_ingested']} conversations ingerees "
                  f"(fichiers scannes : {stats['files_seen']}, skipped : {stats['skipped']})")


@cli.command("check-shell-config")
def check_shell_config_cmd():
    """Verifie que zsh EXTENDED_HISTORY est actif (prerequis collector shell)."""
    from ship1000x.collectors.shell import check_extended_history
    r = check_extended_history()
    console.print()
    console.print("[bold cyan]Zsh history config[/bold cyan]")
    console.print(f"  History file     : {r['history_path']}")
    console.print(f"  Existe           : {'✓' if r['history_exists'] else '✗'}")
    console.print(f"  A des timestamps : {'✓' if r['history_has_timestamps'] else '✗'}")
    console.print(f"  .zshrc configure : {'✓' if r['zshrc_configured'] else '✗'}")
    console.print()
    if not (r['history_has_timestamps'] and r['zshrc_configured']):
        console.print("[yellow]Pour activer :[/yellow]")
        console.print("  echo 'setopt EXTENDED_HISTORY' >> ~/.zshrc")
        console.print("  echo 'setopt HIST_FIND_NO_DUPS' >> ~/.zshrc")
        console.print("  source ~/.zshrc")
    else:
        console.print("[green]✓ Config OK — collector shell utilisable[/green]")


@cli.command("check-mac-permissions")
def check_mac_permissions_cmd():
    """Verifie si pmset + log show fonctionnent (prerequis collector mac_system)."""
    from ship1000x.collectors.mac_system import check_permissions
    r = check_permissions()
    console.print()
    console.print("[bold cyan]macOS system access[/bold cyan]")
    console.print(f"  pmset available    : {'✓' if r['pmset_available'] else '✗'}")
    console.print(f"  log show available : {'✓' if r['log_show_available'] else '✗'}")
    if r["errors"]:
        console.print()
        console.print("[yellow]Erreurs rencontrees :[/yellow]")
        for e in r["errors"]:
            console.print(f"  - {e}")
    console.print()
    if not r["log_show_available"]:
        console.print("[yellow]Si log show echoue, donne Full Disk Access a Terminal.app[/yellow]")
        console.print("  System Settings → Privacy & Security → Full Disk Access → ajouter Terminal")


@cli.command()
@click.option("--tjm", default=None, type=float, help="Recalcule avec un TJM custom")
def benchmark(tjm: float | None):
    """Affiche les benchmarks de reference utilises pour les calculs."""
    from ship1000x.insights.benchmarks import load_benchmarks
    b = load_benchmarks()
    console.print()
    console.print("[bold cyan]Benchmarks utilises[/bold cyan]")
    console.print()
    console.print("[bold]TJM senior dev (EUR/jour)[/bold]")
    console.print(f"  Low  : {b['tjm_senior_low']} EUR")
    console.print(f"  Mid  : {b['tjm_senior_mid']} EUR  {' (TJM override actif)' if tjm else ''}")
    console.print(f"  High : {b['tjm_senior_high']} EUR")
    console.print()
    console.print("[bold]Output senior sans IA (lignes/h)[/bold]")
    console.print(f"  Low  : {b['lines_per_hour_no_ai_low']} l/h")
    console.print(f"  Mid  : {b['lines_per_hour_no_ai_mid']} l/h")
    console.print(f"  High : {b['lines_per_hour_no_ai_high']} l/h")
    console.print()
    console.print("[bold]Valeur MVP SaaS V1 agence Tier-1 (EUR)[/bold]")
    console.print(f"  Low  : {b['value_mvp_saas_low']:,} EUR".replace(",", " "))
    console.print(f"  Mid  : {b['value_mvp_saas_mid']:,} EUR".replace(",", " "))
    console.print(f"  High : {b['value_mvp_saas_high']:,} EUR".replace(",", " "))
    console.print()
    console.print("[bold]Seuils signaux[/bold]")
    console.print(f"  Session longue  : > {b['burnout_long_session_h']}h  (alerte si >= {b['burnout_long_session_count_7d']}/periode)")
    console.print(f"  Heures nuit     : {b['burnout_night_hour_start']}h-{b['burnout_night_hour_end']}h  (alerte si > {b['burnout_night_ratio_pct']}%)")
    console.print(f"  Jours consec.   : alerte si > {b['burnout_consecutive_days']}j consecutifs")
    console.print(f"  Derive projet   : alerte si delta estime/reel > {b['derive_estimate_delta_pct']}%")
    console.print()
    console.print("[dim]Override via config/benchmarks.yaml (merge avec les defauts)[/dim]")


@cli.command()
@click.option("--since", default="30d", help="Fenetre d'audit (defaut 30j)")
def audit(since: str):
    """Audit qualite du tracking : detecte les gaps de mesure.

    Compare jour par jour les commits git vs le temps actif tracke. Un jour
    avec commits mais 0h active = possible session IA non-tracee (source
    manquante, classifier rate, app externe non connectee).
    """
    storage = _get_storage()
    cutoff = _parse_since(since)
    if cutoff is None:
        console.print("[red]Format --since invalide (ex: 7d, 30d)[/red]")
        return
    cutoff_str = cutoff.date().isoformat()

    with storage.conn() as c:
        # Par jour x projet : commits vs active_sec
        rows = c.execute("""
            SELECT
                DATE(started_at) AS d,
                COALESCE(project_id, 'unclassified') AS project_id,
                SUM(CASE WHEN source = 'git' THEN 1 ELSE 0 END) AS commits,
                ROUND(SUM(CASE WHEN source != 'git' THEN duration_sec ELSE 0 END)/3600.0, 2) AS active_h,
                ROUND(SUM(CASE WHEN source != 'git' THEN wall_clock_sec ELSE 0 END)/3600.0, 2) AS wall_h,
                GROUP_CONCAT(DISTINCT source) AS sources
            FROM events
            WHERE DATE(started_at) >= ?
            GROUP BY d, project_id
            HAVING commits > 0
            ORDER BY d DESC, project_id
        """, (cutoff_str,)).fetchall()

    # Detection gaps : commits >= 3 mais active_h == 0 = suspect
    gaps = [r for r in rows if r["commits"] >= 3 and (r["active_h"] or 0) == 0]
    ok_rows = [r for r in rows if r not in gaps]

    # Resume global
    total_commits = sum(r["commits"] for r in rows)
    total_active = sum(r["active_h"] or 0 for r in rows)
    total_wall = sum(r["wall_h"] or 0 for r in rows)
    gap_commits = sum(r["commits"] for r in gaps)

    console.print()
    console.print(f"[bold cyan]AUDIT QUALITE TRACKER — {since}[/bold cyan]")
    console.print()
    console.print(f"Total commits   : {total_commits}")
    console.print(f"Temps actif     : {total_active:.1f}h")
    console.print(f"Temps session   : {total_wall:.1f}h")
    console.print(f"Ratio actif/session : {100*total_active/total_wall:.0f}%" if total_wall else "—")
    console.print()

    if gaps:
        console.print(f"[yellow]⚠ {len(gaps)} gaps detectes[/yellow] "
                      f"({gap_commits} commits sans session IA associee)")
        console.print("  -> possibles sources non-tracees (autre Mac, outil IA non connecte, classifier rate)")
        console.print()
        table = Table(title="Gaps : jours/projets avec commits mais 0h active", show_lines=False)
        table.add_column("Date")
        table.add_column("Projet")
        table.add_column("Commits", justify="right")
        table.add_column("Wall (session)", justify="right")
        table.add_column("Sources presentes", overflow="fold")
        for r in gaps[:20]:
            table.add_row(
                r["d"], r["project_id"],
                str(r["commits"]),
                f"{r['wall_h'] or 0:.1f}h" if r['wall_h'] else "—",
                r["sources"] or "",
            )
        console.print(table)
    else:
        console.print("[green]✓ Aucun gap detecte — commits et sessions sont alignes[/green]")
    console.print()

    # Top 10 jours/projets OK
    console.print("[bold]Top 10 jours/projets les mieux traces (commits + active)[/bold]")
    ok_sorted = sorted(ok_rows, key=lambda r: -(r["active_h"] or 0))[:10]
    table = Table(show_lines=False)
    table.add_column("Date")
    table.add_column("Projet")
    table.add_column("Commits", justify="right")
    table.add_column("Actif", justify="right")
    table.add_column("Session", justify="right")
    table.add_column("Sources")
    for r in ok_sorted:
        table.add_row(
            r["d"], r["project_id"],
            str(r["commits"]),
            f"{r['active_h']:.1f}h",
            f"{r['wall_h']:.1f}h" if r['wall_h'] else "—",
            (r["sources"] or "").replace(",", " + "),
        )
    console.print(table)

    # Coverage par projet
    console.print()
    console.print("[bold]Confiance par projet[/bold]")
    with storage.conn() as c:
        proj_rows = c.execute("""
            SELECT COALESCE(project_id, 'unclassified') AS project_id,
                   COUNT(DISTINCT DATE(started_at)) AS days,
                   SUM(CASE WHEN source = 'git' THEN 1 ELSE 0 END) AS commits,
                   ROUND(SUM(CASE WHEN source != 'git' THEN duration_sec ELSE 0 END)/3600.0, 1) AS active_h,
                   COUNT(DISTINCT CASE WHEN source != 'git' THEN source END) AS ai_sources
            FROM events WHERE DATE(started_at) >= ?
            GROUP BY project_id ORDER BY active_h DESC
        """, (cutoff_str,)).fetchall()

    table = Table(show_lines=False)
    table.add_column("Projet")
    table.add_column("Jours actifs", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("Actif", justify="right")
    table.add_column("Src IA", justify="right")
    table.add_column("Confiance")
    for r in proj_rows:
        # Heuristique confiance : commits vs active + nb sources IA
        if r["commits"] >= 5 and (r["active_h"] or 0) > 0 and r["ai_sources"] >= 1:
            conf = "[green]high[/green]"
        elif r["commits"] >= 3 and (r["active_h"] or 0) == 0:
            conf = "[red]low (gap)[/red]"
        elif (r["active_h"] or 0) > 0:
            conf = "[yellow]medium[/yellow]"
        else:
            conf = "[dim]—[/dim]"
        table.add_row(
            r["project_id"],
            str(r["days"]),
            str(r["commits"]),
            f"{r['active_h'] or 0:.1f}h",
            str(r["ai_sources"]),
            conf,
        )
    console.print(table)


@cli.command()
@click.option(
    "--save",
    is_flag=True,
    help="Ecrit les paths decouverts dans privacy.yaml (section discovered_paths).",
)
def discover(save: bool):
    """Scan HOME pour trouver tous les emplacements d'outils IA (Claude Code, Codex, Cursor).

    Utile si tu as des installs dans des dossiers non-standard. Par defaut
    les collectors ne scannent que ~/.claude/projects, ~/.codex/sessions,
    ~/.cursor/ai-tracking. Cette commande detecte les copies/reloges.

    Avec --save, ajoute les paths trouves a privacy.yaml → pris en compte
    au prochain `tracker ingest`.
    """
    import yaml as _yaml

    from ship1000x.core.discovery import discover_paths

    console.print("[bold cyan]═══ Discovery ═══[/bold cyan]")
    console.print(f"Scan de [cyan]{Path.home()}[/cyan] (max depth 4)...")
    console.print()

    results = discover_paths()

    total_found = sum(len(v) for v in results.values())
    if total_found == 0:
        console.print("[yellow]Aucun emplacement IA trouve sous HOME.[/yellow]")
        return

    console.print("[bold]Resultats par outil :[/bold]")
    for target_id, paths in results.items():
        if not paths:
            continue
        console.print(f"  [cyan]{target_id}[/cyan] ({len(paths)} path(s)) :")
        for p in paths:
            console.print(f"    - {p}")
    console.print()

    if save:
        if PRIVACY_CONFIG.exists():
            config = _yaml.safe_load(PRIVACY_CONFIG.read_text()) or {}
        else:
            config = {}
        config["discovered_paths"] = results
        PRIVACY_CONFIG.write_text(
            _yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )
        console.print(f"[green]✓[/green] Paths sauvegardes dans [cyan]{PRIVACY_CONFIG}[/cyan]")
        console.print(
            "  Lance [cyan]tracker ingest[/cyan] pour que les collectors les utilisent."
        )
    else:
        console.print(
            "[dim]Relance avec [cyan]--save[/cyan] pour enregistrer dans privacy.yaml.[/dim]"
        )


@cli.command()
@click.option(
    "--fix",
    is_flag=True,
    help="Auto-corrige les problemes detectes (migration privacy.yaml, prompt credentials S3).",
)
def doctor(fix: bool):
    """Diagnostic complet du tracker : config, sources, coverage, suggestions.

    Avec --fix : applique les corrections automatiques (migration yaml silencieuse,
    prompt interactif pour credentials S3 manquants, ecriture dans ~/.aws/credentials).
    """
    import os

    from ship1000x.core.config_migration import (
        check_aws_credentials,
        run_auto_migration,
        write_aws_credentials,
    )
    from ship1000x.core.health import scan_sources

    # Étape 0 — Migration auto silencieuse si --fix
    if fix and PRIVACY_CONFIG.exists():
        migration_changes = run_auto_migration(PRIVACY_CONFIG)
        if migration_changes:
            console.print("[yellow]⚙[/yellow]  Migration privacy.yaml :")
            for change in migration_changes:
                console.print(f"    - {change}")
            console.print()

    privacy_config = _load_yaml(PRIVACY_CONFIG)
    sources = scan_sources(privacy_config)
    consent = privacy_config.get("consent") or {}
    cloud = privacy_config.get("cloud") or {}

    console.print()
    console.print("[bold cyan]DIAGNOSTIC TRACKER[/bold cyan]")
    console.print()

    # Étape credentials S3 avec prompt si --fix
    if fix and cloud.get("push_enabled"):
        creds_info = check_aws_credentials()
        if not creds_info["found"]:
            from rich.prompt import Confirm, Prompt

            from ship1000x.core.config_migration import (
                format_secret_preview,
                validate_aws_access_key,
                validate_aws_secret,
            )

            def _print_fallback():
                """Affiche la commande fallback cat > EOF a copier."""
                console.print()
                console.print("[yellow]Fallback : cree le fichier manuellement[/yellow]")
                console.print("[dim]Colle ce bloc dans ton terminal apres avoir remplace les valeurs :[/dim]")
                console.print()
                console.print("[cyan]cat > ~/.aws/credentials << 'EOF'[/cyan]")
                console.print("[cyan][default][/cyan]")
                console.print("[cyan]aws_access_key_id=<colle ton access key>[/cyan]")
                console.print("[cyan]aws_secret_access_key=<colle ton secret>[/cyan]")
                console.print("[cyan]EOF[/cyan]")
                console.print("[cyan]chmod 600 ~/.aws/credentials[/cyan]")

            console.print("[yellow]⚠[/yellow]  Credentials AWS/Garage S3 absents.")
            console.print(
                "[dim]  Note : la saisie est [bold]visible a l'ecran[/bold] (pas masquee). "
                "C'est intentionnel — le masquage casse le paste depuis certains terminaux macOS. "
                "Ferme le terminal ou utilise `clear` a la fin si tu veux nettoyer l'historique.[/dim]"
            )
            console.print()
            ok = Confirm.ask(
                "  Les saisir maintenant et les ecrire dans ~/.aws/credentials ?",
                default=True,
            )
            if not ok:
                _print_fallback()
            else:
                # Access key : validation format + retry
                ak = ""
                ak_valid = False
                for attempt in range(3):
                    raw = Prompt.ask("  AWS_ACCESS_KEY_ID").strip()
                    v_ok, v_msg = validate_aws_access_key(raw)
                    if v_ok:
                        ak = raw
                        ak_valid = True
                        console.print(
                            f"  [green]→[/green] saisi : [dim]{format_secret_preview(ak)}[/dim]"
                        )
                        break
                    console.print(f"  [red]✗[/red] {v_msg}")
                if not ak_valid:
                    console.print(
                        "  [red]Access key invalide apres 3 essais. Abandon.[/red]"
                    )
                    _print_fallback()
                else:
                    # Secret : idem validation format + retry
                    sk = ""
                    sk_valid = False
                    for attempt in range(3):
                        raw = Prompt.ask("  AWS_SECRET_ACCESS_KEY").strip()
                        v_ok, v_msg = validate_aws_secret(raw)
                        if v_ok:
                            sk = raw
                            sk_valid = True
                            console.print(
                                f"  [green]→[/green] saisi : [dim]{format_secret_preview(sk)}[/dim]"
                            )
                            break
                        console.print(f"  [red]✗[/red] {v_msg}")
                    if not sk_valid:
                        console.print(
                            "  [red]Secret invalide apres 3 essais. Abandon.[/red]"
                        )
                        _print_fallback()
                    else:
                        # Ecriture + relecture pour confirmer
                        path = write_aws_credentials(ak, sk)
                        # Relecture pour valider l'ecriture
                        import configparser
                        check_cp = configparser.ConfigParser()
                        check_cp.read(path)
                        read_ak = check_cp.get("default", "aws_access_key_id", fallback="")
                        read_sk = check_cp.get("default", "aws_secret_access_key", fallback="")
                        if read_ak == ak and read_sk == sk:
                            console.print(
                                f"  [green]✓[/green] Ecrit dans {path} (mode 600)"
                            )
                            console.print(
                                f"  [green]✓[/green] Relecture verifiee : access_key {format_secret_preview(read_ak)}, "
                                f"secret {format_secret_preview(read_sk)}"
                            )
                            console.print(
                                "  Les prochains [cyan]tracker push[/cyan] utiliseront ces credentials."
                            )
                        else:
                            console.print(
                                f"  [red]✗ Ecriture echouee : {path} ne contient pas les bonnes valeurs[/red]"
                            )
                            _print_fallback()
            console.print()

    # 1. Identity
    console.print("[bold]1. Identite[/bold]")
    if consent.get("signed_at"):
        console.print(f"  [green]v[/green] Consent signe pour {consent.get('user_email')} le {consent['signed_at'][:10]}")
        console.print(f"  [green]v[/green] display_name = {consent.get('display_name', '-')}")
        share = consent.get('cloud_sync')
        console.print(f"  {'[green]v[/green]' if share else '[yellow]o[/yellow]'} cloud_sync = {share}")
    else:
        console.print("  [red]x[/red] Consent non signe. Lance [cyan]tracker init[/cyan]")
        return
    console.print()

    # 2. Cloud S3
    console.print("[bold]2. Push S3 (Garage)[/bold]")
    if cloud.get("push_enabled") and cloud.get("bucket") and cloud.get("endpoint"):
        console.print(f"  [green]v[/green] bucket = {cloud['bucket']}")
        console.print(f"  [green]v[/green] endpoint = {cloud['endpoint']}")
        ak = os.environ.get("AWS_ACCESS_KEY_ID")
        sk = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if ak and sk:
            console.print("  [green]v[/green] AWS credentials presentes")
        else:
            console.print("  [yellow]![/yellow] AWS credentials absentes de l'env. Push manuel requiert export AWS_*")
    else:
        console.print("  [yellow]o[/yellow] Push desactive")
    console.print()

    # 3. Sources
    console.print("[bold]3. Sources[/bold]")
    tracked = [s for s in sources if s.status == "tracked"]
    partial = [s for s in sources if s.status == "partial"]
    absent_installed = [s for s in sources if s.status == "not_tracked" and s.path_exists]
    disabled = [s for s in sources if s.status == "disabled"]
    console.print(f"  [green]v[/green] {len(tracked)} tracees actives")
    if partial:
        console.print(f"  [yellow]![/yellow] {len(partial)} partielles:")
        for s in partial:
            console.print(f"      - {s.label}")
    if disabled:
        console.print(f"  [dim]o[/dim] {len(disabled)} desactivees dans privacy.yaml:")
        for s in disabled:
            console.print(f"      - {s.label}")
    if absent_installed:
        console.print(f"  [red]x[/red] {len(absent_installed)} installees mais pas tracees:")
        for s in absent_installed:
            console.print(f"      - {s.label}")
    console.print()

    # 4. Coverage 7j
    storage = _get_storage()
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    cutoff_7 = (_dt.utcnow() - _td(days=7)).isoformat()
    with storage.conn() as c:
        r = c.execute("""
            SELECT COUNT(DISTINCT DATE(started_at)) AS d_cov,
                   COUNT(*) AS n, ROUND(SUM(duration_sec)/3600.0,1) AS h,
                   ROUND(SUM(wall_clock_sec)/3600.0,1) AS wh,
                   COUNT(DISTINCT source) AS srcs
            FROM events WHERE started_at >= ?
        """, (cutoff_7,)).fetchone()
    console.print("[bold]4. Coverage 7j[/bold]")
    console.print(f"  Jours avec event  : {r['d_cov']} / 7")
    console.print(f"  Events            : {r['n']}")
    console.print(f"  Temps actif       : {r['h'] or 0}h")
    console.print(f"  Temps session     : {r['wh'] or 0}h")
    console.print(f"  Sources distinctes: {r['srcs']}")
    if r['d_cov'] == 7:
        console.print("  [green]v[/green] Couverture complete")
    elif r['d_cov'] >= 5:
        console.print("  [yellow]![/yellow] Quelques jours sans event (weekend?)")
    else:
        console.print("  [red]x[/red] Trous de tracking. Verifier que le cron tourne.")
    console.print()

    # 5. Scheduler
    console.print("[bold]5. Scheduler (cron launchd)[/bold]")
    try:
        from core import scheduler as _sched
        status = _sched.status()
        if isinstance(status, dict) and status.get("installed"):
            console.print(f"  [green]v[/green] Cron installe a {status.get('time', '-')}")
        else:
            console.print("  [yellow]o[/yellow] Pas de cron installe. Lance [cyan]tracker install-scheduler[/cyan]")
    except Exception:
        console.print("  [dim](scheduler.status() non dispo)[/dim]")
    console.print()

    # 6. Suggestions
    console.print("[bold]6. Suggestions[/bold]")
    suggestions = []
    for s in sources:
        if s.id == "shell" and s.status != "tracked":
            suggestions.append("  - Activer EXTENDED_HISTORY dans ~/.zshrc (tracker check-shell-config)")
        if s.id == "mac_system" and s.status == "disabled":
            suggestions.append("  - Optionnel: activer mac_system dans privacy.yaml pour wake/sleep cross-check")
        if s.id == "claude_desktop" and s.path_exists and s.status == "not_tracked":
            suggestions.append("  - Exporter claude.ai: Settings > Privacy > Export data dans drop folder")
        if s.id == "codex_desktop" and s.path_exists and s.status == "not_tracked":
            suggestions.append("  - Codex Desktop: les logs state_5.sqlite sont captures via codex_desktop_logs")
    suggestions.append("  - Multi-Mac: installer tracker sur chaque machine avec meme user_email")
    suggestions.append("  - Lance [cyan]tracker audit --since 30d[/cyan] pour voir les gaps commits vs sessions")
    for line in suggestions:
        console.print(line)
    console.print()

    # 7. Resume
    total = len(sources)
    ok = len(tracked)
    pct = 100 * ok / total if total else 0
    if pct >= 60:
        grade = "[green]Bon[/green]"
    elif pct >= 30:
        grade = "[yellow]Moyen[/yellow]"
    else:
        grade = "[red]Faible[/red]"
    console.print("[bold]7. Resume[/bold]")
    console.print(f"  Tracees : {ok}/{total} = {pct:.0f}%  -> {grade}")
    console.print()


def main() -> None:
    """Entry point wrapper for pyproject.toml [project.scripts]."""
    cli()


if __name__ == "__main__":
    main()
