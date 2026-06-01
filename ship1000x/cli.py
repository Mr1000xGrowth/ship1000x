#!/usr/bin/env python3
"""Ship1000x — CLI entrypoint.

Local-first AI dev productivity tracker.

Usage:
    ship1000x ingest                      # collect from all sources
    ship1000x today                       # today's summary
    ship1000x today --compare-modes       # compare 5 active-time modes
    ship1000x week                        # last 7 days
    ship1000x project <id>                # project detail
    ship1000x project <id> --since 30d    # custom window
    ship1000x calibrate                   # personal cadence profile (P95 threshold)
    ship1000x init                        # interactive setup wizard
    ship1000x privacy                     # show privacy config
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

# User-facing paths follow the XDG Base Directory spec (Linux) / match
# equivalents on macOS. Config and data always live in the user home,
# never inside the installed package (critical for `pip install` users).
import os as _os

from ship1000x.core.classifier import Classifier
from ship1000x.core.cost_truth import api_equivalent_cost_from_row
from ship1000x.core.source_inventory import INGEST_SOURCE_NAMES, RECLASSIFY_COLLECTORS
from ship1000x.core.storage import Storage

XDG_CONFIG_HOME = Path(_os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
XDG_DATA_HOME = Path(_os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
CONFIG_DIR = XDG_CONFIG_HOME / "ship1000x"
DATA_DIR = XDG_DATA_HOME / "ship1000x"

REPO_ROOT = Path(__file__).parent  # kept for internal file lookups (bundled templates)
DB_PATH = DATA_DIR / "tracker.sqlite"
PROJECTS_CONFIG = CONFIG_DIR / "projects.yaml"
PRIVACY_CONFIG = CONFIG_DIR / "privacy.yaml"

# Ensure user config/data dirs exist before any command runs.
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

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


def _event_api_equivalent_cost(row: dict) -> float:
    return api_equivalent_cost_from_row(row)


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


def _since_to_days(since: str) -> int | None:
    """Convertit une fenetre relative simple en jours pour les rapports audit."""
    if _parse_since(since) is None:
        return None
    unit = since[-1]
    amount = int(since[:-1])
    if unit == "d":
        return amount
    if unit == "w":
        return amount * 7
    return max(1, (amount + 23) // 24)


def _apply_source_quality_gate(
    report: dict,
    *,
    fail_under: float | None,
    fail_on_risk: set[str],
    fail_on_collector_stage: set[str] | None = None,
) -> list[str]:
    """Attach a source-quality gate result and return failure messages."""
    fail_on_collector_stage = fail_on_collector_stage or set()
    observed_rows = [row for row in report["rows"] if row["events"] > 0]
    failures: list[str] = []
    average_score = report["summary"].get("average_observed_quality_score")
    if fail_under is not None and (average_score is None or float(average_score) < fail_under):
        rendered_score = "not measured" if average_score is None else f"{float(average_score):.2f}"
        failures.append(f"average observed quality score {rendered_score} is below {fail_under:.2f}")
    for row in observed_rows:
        if row["risk"] in fail_on_risk:
            failures.append(f"{row['source']} risk is {row['risk']}")
        if row.get("collector_stage") in fail_on_collector_stage:
            failures.append(f"{row['source']} collector_stage is {row['collector_stage']}")
    report["gate"] = {
        "passed": not failures,
        "fail_under": fail_under,
        "fail_on_risk": sorted(fail_on_risk),
        "fail_on_collector_stage": sorted(fail_on_collector_stage),
        "failures": failures,
    }
    return failures


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
    source = source.strip()
    if source not in INGEST_SOURCE_NAMES:
        console.print(f"[red]Source inconnue pour ingest: {source!r}[/red]")
        console.print("Sources connues: " + ", ".join(INGEST_SOURCE_NAMES))
        raise click.exceptions.Exit(2)

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

    def _explicit_or_enabled(name: str, default: str = _DEFAULT_ENABLED) -> bool:
        return source == name or (source == "all" and _src_enabled(name, default))

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
    # mais n'est pas wire dans `ship1000x ingest` par defaut. Activable explicitement
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

    if source in ("all", "roo_kilo_code") and _src_enabled("roo_kilo_code"):
        console.print("[cyan]Collecting Roo/Kilo Code tasks...[/cyan]")
        from collectors import roo_kilo_code
        stats = roo_kilo_code.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats.get('sessions_ingested', 0)} taches Roo/Kilo ingerees "
            f"({stats.get('events_ingested', 0)} events)"
        )

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

    if source in ("all", "claude_desktop") and _src_enabled("claude_desktop"):
        console.print("[cyan]Collecting Claude Desktop session sidecars...[/cyan]")
        from collectors import claude_desktop_sessions
        stats = claude_desktop_sessions.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats.get('sessions_ingested', 0)} Claude Desktop sessions "
            f"({stats.get('files_parsed', 0)} sidecars parsed, "
            f"{stats.get('events_ingested', 0)} events; metadata only, "
            f"tokens/cost stay on the claude_code source)"
        )

    if _explicit_or_enabled("claude_statusline", _DEFAULT_DISABLED):
        console.print("[cyan]Collecting Claude Code statusline drops...[/cyan]")
        from collectors import claude_statusline
        stats = claude_statusline.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats.get('events_ingested', 0)} statusline ticks agreges")

    if _explicit_or_enabled("agent_runtime", _DEFAULT_DISABLED):
        console.print("[cyan]Collecting agent runtime drops...[/cyan]")
        from collectors import agent_runtime
        stats = agent_runtime.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(
            f"  → {stats.get('events_ingested', 0)} runtime events "
            f"({stats.get('ticks_aggregated', 0)} ticks agreges)"
        )

    if _explicit_or_enabled("trace", _DEFAULT_DISABLED):
        console.print("[cyan]Collecting usage-proxy drops...[/cyan]")
        from collectors import trace
        stats = trace.collect(storage, classifier, privacy_config)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        console.print(f"  → {stats.get('events_ingested', 0)} usage-proxy api_call events")

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
            duration_sec,
            token_input,
            token_output,
            cost_estimated,
            raw_meta
        FROM events
        WHERE started_at >= ?
        """,
        (today_start.isoformat(),),
    )

    if not rows:
        console.print("[yellow]Aucune activite trackee aujourd'hui.[/yellow]")
        console.print("  Lance [cyan]ship1000x ingest[/cyan] pour collecter les sessions.")
        return

    table = Table(title=f"Aujourd'hui ({today_start.date()})", show_header=True)
    table.add_column("Projet", style="cyan")
    table.add_column("Temps actif", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cout API-eq $", justify="right")

    total_sec = 0
    total_cost = 0.0
    by_project: dict[str, dict] = {}
    for r in rows:
        project = r["project"]
        bucket = by_project.setdefault(
            project,
            {"active_sec": 0, "sessions": 0, "tokens": 0, "cost": 0.0},
        )
        bucket["active_sec"] += r["duration_sec"] or 0
        bucket["sessions"] += 1
        bucket["tokens"] += (r["token_input"] or 0) + (r["token_output"] or 0)
        bucket["cost"] += _event_api_equivalent_cost(r)
    for project, r in sorted(by_project.items(), key=lambda item: -item[1]["active_sec"]):
        total_sec += r["active_sec"] or 0
        total_cost += r["cost"] or 0.0
        table.add_row(
            project,
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
    from ship1000x.core.unified_metrics import get_daily_unified

    unified = get_daily_unified(storage, day)
    if not unified:
        console.print(f"[yellow]Aucune metrique unifiee pour {day}.[/yellow]")
        console.print("  Lance [cyan]ship1000x rollup --since 7d[/cyan] pour recalculer.")
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
    additive = unified.get("agent_sec_additive") or 0
    human_p95 = unified["active_sec_p95"] or 0
    ratio = (additive / human_p95) if human_p95 else 0
    ratio_note = f"[dim]somme sessions //, peut > 24h · x{ratio:.1f} vs humain[/dim]"
    table.add_row("  debit cumule (//)", "—",
                  _fmt_duration(additive), ratio_note)

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
        console.print(f"  Lance [cyan]ship1000x calibrate[/cyan] quand tu auras 100+ events humains "
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
        console.print("  Lance [cyan]ship1000x daily[/cyan] regulierement pour accumuler.")
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
    console.print("    [cyan]ship1000x today --compare-modes[/cyan]")
    console.print("    [cyan]ship1000x rollup[/cyan] (rebuild des daily_unified)")
    console.print()
    console.print("  Override possible :")
    console.print("    [cyan]ship1000x today --compare-modes[/cyan] (voir les 5 modes cote-a-cote)")


@cli.command()
def week():
    """Resume 7 derniers jours."""
    storage = _get_storage()
    week_start = datetime.now() - timedelta(days=7)

    rows = storage.query(
        """
        SELECT
            COALESCE(project_id, 'unclassified') AS project,
            duration_sec,
            cost_estimated,
            raw_meta
        FROM events
        WHERE started_at >= ?
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
    table.add_column("Cout API-eq $", justify="right")

    total_sec = 0
    total_cost = 0.0
    by_project: dict[str, dict] = {}
    for r in rows:
        project = r["project"]
        bucket = by_project.setdefault(project, {"active_sec": 0, "sessions": 0, "cost": 0.0})
        bucket["active_sec"] += r["duration_sec"] or 0
        bucket["sessions"] += 1
        bucket["cost"] += _event_api_equivalent_cost(r)
    for project, r in sorted(by_project.items(), key=lambda item: -item[1]["active_sec"]):
        total_sec += r["active_sec"] or 0
        total_cost += r["cost"] or 0.0
        table.add_row(
            project,
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
@click.option("--since", default="30d", help="Window: 7d, 30d, 90d, 365d")
@click.option("--client", default=None, help="Filter by client tag (from projects.yaml)")
@click.option("--top", default=20, type=int, help="Top N projects to show (default 20)")
def summary(since: str, client: str | None, top: int):
    """Cross-tabulated view : per-project breakdown by tool (Claude Code,
    Codex, Cursor, OpenClaw, git, etc.) + total time + API-equivalent cost + dominant tool.

    The single-pane view : everything you need to know about your projects
    in one table.
    """
    storage = _get_storage()
    days = _parse_since_days(since)
    cutoff = datetime.now() - timedelta(days=days)

    # Optional filter by client tag from projects.yaml
    project_to_client: dict[str, str] = {}
    if client:
        projects_cfg = _load_yaml(PROJECTS_CONFIG)
        for p in projects_cfg.get("projects") or []:
            if p.get("client"):
                project_to_client[p["id"]] = p["client"]

    rows = storage.query(
        """
        SELECT
            COALESCE(project_id, 'unclassified') AS project,
            source,
            duration_sec,
            token_input,
            token_output,
            cost_estimated,
            raw_meta
        FROM events
        WHERE started_at >= ?
        ORDER BY project, source
        """,
        (cutoff.isoformat(),),
    )

    if not rows:
        console.print(f"[yellow]Aucune activite sur {since}.[/yellow]")
        return

    # Aggregate by project, tracking source breakdown
    by_project: dict[str, dict] = {}
    all_sources: set[str] = set()
    for r in rows:
        pid = r["project"]
        if client:
            if project_to_client.get(pid) != client:
                continue
        if pid not in by_project:
            by_project[pid] = {
                "total_sec": 0, "total_tokens": 0, "total_cost": 0.0,
                "events": 0, "sources": {},
            }
        p = by_project[pid]
        sec = r["duration_sec"] or 0
        p["total_sec"] += sec
        p["total_tokens"] += (r["token_input"] or 0) + (r["token_output"] or 0)
        p["total_cost"] += _event_api_equivalent_cost(r)
        p["events"] += 1
        prev_sec, prev_events = p["sources"].get(r["source"], (0, 0))
        p["sources"][r["source"]] = (prev_sec + sec, prev_events + 1)
        all_sources.add(r["source"])

    # Sort projects by total time desc, take top N
    sorted_projects = sorted(by_project.items(), key=lambda x: -x[1]["total_sec"])[:top]

    title = f"Project summary — {since}"
    if client:
        title += f" — client: {client}"
    table = Table(title=title, show_header=True)
    table.add_column("Projet", style="cyan", no_wrap=False)
    table.add_column("Actif", justify="right")
    table.add_column("Outil dominant", style="magenta")
    table.add_column("Sessions IA", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("API-eq cost $", justify="right")

    grand_sec = 0
    grand_cost = 0.0
    grand_sessions_ia = 0
    grand_commits = 0
    for pid, info in sorted_projects:
        # Find dominant tool by active time (excluding git which has 0 active)
        sources_ia = {s: v for s, v in info["sources"].items() if s != "git"}
        if sources_ia:
            dom_source = max(sources_ia.items(), key=lambda x: x[1][0])
            dom_label = dom_source[0]
            dom_sec = dom_source[1][0]
            dom_pct = (dom_sec / info["total_sec"] * 100) if info["total_sec"] else 0
            dom_str = f"{dom_label} ({dom_pct:.0f}%)" if dom_pct >= 1 else "—"
        else:
            dom_str = "git only"

        sessions_ia = sum(v[1] for s, v in info["sources"].items() if s != "git")
        commits = info["sources"].get("git", (0, 0))[1]

        grand_sec += info["total_sec"]
        grand_cost += info["total_cost"]
        grand_sessions_ia += sessions_ia
        grand_commits += commits

        # Truncate long project_ids for readability
        pid_disp = pid if len(pid) <= 60 else pid[:57] + "..."

        table.add_row(
            pid_disp,
            _fmt_duration(info["total_sec"]),
            dom_str,
            str(sessions_ia),
            str(commits),
            f"{info['total_cost']:.2f}",
        )

    table.add_section()
    table.add_row(
        f"[bold]TOTAL ({len(sorted_projects)} projets)[/bold]",
        f"[bold]{_fmt_duration(grand_sec)}[/bold]",
        "",
        f"[bold]{grand_sessions_ia}[/bold]",
        f"[bold]{grand_commits}[/bold]",
        f"[bold]{grand_cost:.2f}[/bold]",
    )

    console.print(table)
    console.print()

    # Per-source breakdown summary (informational footer)
    sources_global: dict[str, int] = {}
    for info in by_project.values():
        for src, (sec, _) in info["sources"].items():
            sources_global[src] = sources_global.get(src, 0) + sec

    console.print("[bold]Time per source (across all projects)[/bold]")
    for src, sec in sorted(sources_global.items(), key=lambda x: -x[1]):
        if sec > 0:
            pct = (sec / grand_sec * 100) if grand_sec else 0
            console.print(f"  {src:<20} {_fmt_duration(sec):>8}  ({pct:.1f}%)")


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
            source,
            duration_sec,
            token_input,
            token_output,
            cost_estimated,
            raw_meta
        FROM events
        WHERE project_id = ? AND started_at >= ?
        ORDER BY day DESC
        """,
        (project_id, cutoff.isoformat()),
    )

    if not rows:
        console.print(f"[yellow]Aucune activite pour '{project_id}' sur {since}.[/yellow]")
        return

    table = Table(title=f"Projet {project_id} — {since}", show_header=True)
    table.add_column("Jour", style="cyan")
    table.add_column("Actif IA", justify="right")
    table.add_column("Sessions IA", justify="right")
    table.add_column("Commits git", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cout API-eq $", justify="right")

    total_sec = 0
    total_cost = 0.0
    total_sessions = 0
    total_commits = 0
    by_day: dict[str, dict] = {}
    for r in rows:
        day = r["day"]
        bucket = by_day.setdefault(
            day,
            {"active_sec": 0, "sessions_ia": 0, "commits_git": 0, "tokens": 0, "cost": 0.0},
        )
        bucket["active_sec"] += r["duration_sec"] or 0
        if r["source"] == "git":
            bucket["commits_git"] += 1
        else:
            bucket["sessions_ia"] += 1
        bucket["tokens"] += (r["token_input"] or 0) + (r["token_output"] or 0)
        bucket["cost"] += _event_api_equivalent_cost(r)
    for day, r in sorted(by_day.items(), reverse=True):
        total_sec += r["active_sec"] or 0
        total_cost += r["cost"] or 0.0
        total_sessions += r["sessions_ia"] or 0
        total_commits += r["commits_git"] or 0
        table.add_row(
            day,
            _fmt_duration(r["active_sec"] or 0),
            str(r["sessions_ia"] or 0),
            str(r["commits_git"] or 0),
            f"{r['tokens'] or 0:,}",
            f"{r['cost'] or 0:.2f}",
        )

    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{_fmt_duration(total_sec)}[/bold]",
        f"[bold]{total_sessions}[/bold]",
        f"[bold]{total_commits}[/bold]",
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
            "[red]✗[/red] Consent non signe. Lance [cyan]ship1000x init[/cyan] d'abord."
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
            "[yellow]Aucun projet connu. Lance [cyan]ship1000x ingest[/cyan] d'abord.[/yellow]"
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
@click.pass_context
def init(ctx: click.Context):
    """Setup initial interactif (consent + config + auto-detection projets).

    Apres la configuration : lance automatiquement une premiere ingestion
    et affiche les Highlights pour donner immediatement de la valeur a
    l'utilisateur. Pas besoin de chercher quelle commande lancer.
    """
    from rich.prompt import Confirm

    from ship1000x.core.setup_wizard import run_init
    run_init(REPO_ROOT, PROJECTS_CONFIG, PRIVACY_CONFIG, console)

    # First-launch experience : auto-ingest + highlights
    console.print()
    if Confirm.ask("[bold]Lancer la premiere collecte maintenant ?[/bold] (recommande)", default=True):
        console.print()
        console.print("[cyan]→ Premiere ingestion en cours...[/cyan]")
        try:
            ctx.invoke(ingest, source="all")
        except Exception as e:
            console.print(f"[yellow]Ingestion partielle : {e}[/yellow]")

        console.print()
        console.print("[cyan]→ Calcul des rollups...[/cyan]")
        try:
            ctx.invoke(rollup, since="30d")
        except Exception as e:
            console.print(f"[yellow]Rollup partiel : {e}[/yellow]")

        console.print()
        console.print("[cyan]→ Calibration de ton profil cadence...[/cyan]")
        try:
            ctx.invoke(calibrate, window=14, user=None)
        except Exception as e:
            console.print(f"[yellow]Calibration : {e}[/yellow]")

        console.print()
        console.print("[bold green]🎉 Setup termine ! Voici tes premiers Highlights :[/bold green]")
        try:
            ctx.invoke(highlights, since="30d")
        except Exception as e:
            console.print(f"[yellow]Highlights indisponibles : {e}[/yellow]")
            console.print("[dim]Lance manuellement : [cyan]ship1000x highlights[/cyan][/dim]")
    else:
        console.print()
        console.print("[dim]OK. Tu peux lancer plus tard :[/dim]")
        console.print("  [cyan]ship1000x ingest[/cyan]        # collecte")
        console.print("  [cyan]ship1000x calibrate[/cyan]     # ton profil cadence")
        console.print("  [cyan]ship1000x highlights[/cyan]    # le showcase")


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
    console.print("[bold cyan]═══ ship1000x setup ═══[/bold cyan]")
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
        console.print("[yellow]Setup stoppe. Corrige puis relance ship1000x setup.[/yellow]")
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
        "[cyan]ship1000x install-scheduler[/cyan] pour automatiser le daily push."
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
        console.print("  [yellow]⚠[/yellow]  Consent non signe. Lance [cyan]ship1000x init[/cyan].")
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
    from ship1000x.core.unified_metrics import rebuild_unified_metrics
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

    # daily_unified : temps actif unifie cross-sources (union d'intervalles +
    # ecarts). Source de verite du graphe "Daily activity" du dashboard.
    # Apres refresh cadence pour que le threshold P95 soit a jour.
    u_stats = rebuild_unified_metrics(storage, cutoff, user_email=user_email)
    console.print(
        f"[green]✓[/green] Unified : {u_stats['unified_rows']} lignes "
        f"sur {u_stats['days']} jours"
    )

    # daily_model_usage : cout + tokens par (jour, source, modele). Alimente
    # la vue d'observabilite cout du dashboard. Agrege usage_breakdown x pricing.
    from ship1000x.core.model_usage import rebuild_model_usage
    m_stats = rebuild_model_usage(storage, cutoff)
    console.print(
        f"[green]✓[/green] Model usage : {m_stats['model_rows']} lignes "
        f"sur {m_stats['days']} jours"
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
    `ship1000x rollup` + `ship1000x push` pour propager vers le dashboard.
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
    console.print("  1. [cyan]ship1000x rollup[/cyan]              — recalcule les daily_rollup avec machine_id")
    console.print("  2. [cyan]ship1000x push[/cyan]                — re-upload les rollups vers S3 (ecrase les fichiers pre-V2)")
    console.print(f"  3. Dashboard → Sync → tu verras '{current_machine}' avec les vraies heures")


@cli.command()
@click.option("--since", default="180d", help="Fenetre a reclasser (ex: 30d, 90d, 365d)")
@click.option("--dry-run", is_flag=True, help="Affiche le plan d'impact sans modifier la DB.")
@click.option("--json", "json_output", is_flag=True, help="Avec --dry-run, affiche le plan JSON.")
@click.pass_context
def reclassify(ctx: click.Context, since: str, dry_run: bool, json_output: bool):
    """Reclasse les evenements historiques avec le classifier courant.

    Workflow :
      1. Reset les offsets d'ingestion des sources activees
      2. Re-run les collectors actives avec le classifier courant
      3. Rebuild les rollups sur la fenetre demandee

    A lancer apres :
      - mise a jour du CLI (nouveaux patterns dans line_classification.yaml)
      - edition de config/line_classification.local.yaml
      - changement de regles seed_threshold
    """
    storage = _get_storage()
    privacy_config = _load_yaml(PRIVACY_CONFIG)
    cutoff = _parse_since(since) or (datetime.now() - timedelta(days=180))

    if json_output and not dry_run:
        console.print("[red]--json est disponible uniquement avec --dry-run[/red]")
        raise click.exceptions.Exit(2)

    from ship1000x.core.reclassify_plan import build_reclassify_plan

    if dry_run:
        import json as _json

        plan = build_reclassify_plan(
            storage,
            since=since,
            cutoff=cutoff,
            sources_enabled=privacy_config.get("sources", {}),
            window_days=_since_to_days(since) or 180,
        )
        if json_output:
            click.echo(_json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False))
            return

        impact = plan["impact"]
        console.print("[yellow]DRY RUN — aucune modification appliquee[/yellow]")
        console.print()
        console.print(f"Cutoff                  : {plan['cutoff']}")
        console.print(f"Ingestion offsets reset : {impact['ingestion_offsets_to_reset']}")
        console.print(f"Events purges           : {impact['events_to_delete']}")
        console.print(f"Rollup rows rebuild     : {impact['daily_rollup_rows_to_rebuild']}")
        console.print(f"Collectors enabled      : {impact['enabled_collectors']}")
        console.print(f"Collectors disabled     : {impact['disabled_collectors']}")
        history_summary = plan["history_recoverability_summary"]
        console.print(
            "History recoverability  : "
            f"{history_summary['recoverable_sources']} recoverable / "
            f"{history_summary['partial_sources']} partial / "
            f"{history_summary['external_source_required_sources']} external / "
            f"{history_summary['not_recoverable_from_ship_sources']} not recoverable"
        )
        console.print()

        table = Table(show_lines=False)
        table.add_column("Source")
        table.add_column("Events to delete", justify="right")
        table.add_column("API-eq cost in window", justify="right")
        table.add_column("Recoverability")
        table.add_column("Repair scope")
        for row in plan["sources"]:
            table.add_row(
                row["source"],
                str(row["events_to_delete"]),
                f"${row['api_equivalent_cost_usd']:.4f}",
                row["recoverability"],
                row["repair_scope"],
            )
        console.print(table)
        risky_sources = [
            row
            for row in plan["sources"]
            if row["recoverability"] != "recoverable"
        ]
        if risky_sources:
            console.print()
            console.print("[yellow]Sources requiring repair caution:[/yellow]")
            for row in risky_sources:
                console.print(f"  - {row['source']}: {row['recoverability']}")
                console.print(f"    scope: {row['repair_scope']}")
                console.print(f"    recommendation: {row['repair_recommendation']}")
                if row["unrecoverable_truth_fields"]:
                    console.print(
                        "    cannot repair: "
                        + ", ".join(str(field) for field in row["unrecoverable_truth_fields"])
                    )
                console.print(f"    boundary: {row['claim_boundary']}")
        console.print()
        console.print("[bold]Warnings:[/bold]")
        for warning in plan["warnings"]:
            console.print(f"  - {warning}")
        console.print()
        return

    console.print("[bold cyan]═══ ship1000x reclassify ═══[/bold cyan]")
    console.print()
    preflight = build_reclassify_plan(
        storage,
        since=since,
        cutoff=cutoff,
        sources_enabled=privacy_config.get("sources", {}),
        window_days=_since_to_days(since) or 180,
    )
    impact = preflight["impact"]
    console.print("[bold]Preflight impact:[/bold]")
    console.print(f"  Events purges           : {impact['events_to_delete']}")
    console.print(f"  Ingestion offsets reset : {impact['ingestion_offsets_to_reset']}")
    console.print(f"  Rollup rows rebuild     : {impact['daily_rollup_rows_to_rebuild']}")
    console.print(
        "  History recoverability  : "
        f"{preflight['history_recoverability_summary']['recoverable_sources']} recoverable / "
        f"{preflight['history_recoverability_summary']['partial_sources']} partial / "
        f"{preflight['history_recoverability_summary']['external_source_required_sources']} external / "
        f"{preflight['history_recoverability_summary']['not_recoverable_from_ship_sources']} not recoverable"
    )
    if preflight["warnings"]:
        console.print("  [yellow]Warnings:[/yellow]")
        for warning in preflight["warnings"]:
            console.print(f"    - {warning}")
    risky_sources = [
        row
        for row in preflight["sources"]
        if row["recoverability"] != "recoverable"
    ]
    if risky_sources:
        console.print("  [yellow]Sources requiring repair caution:[/yellow]")
        for row in risky_sources[:8]:
            console.print(f"    - {row['source']}: {row['recoverability']}")
            console.print(f"      scope: {row['repair_scope']}")
            console.print(f"      recommendation: {row['repair_recommendation']}")
            if row["unrecoverable_truth_fields"]:
                console.print(
                    "      cannot repair: "
                    + ", ".join(str(field) for field in row["unrecoverable_truth_fields"])
                )
            console.print(f"      boundary: {row['claim_boundary']}")
    console.print()

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
    classifier = _get_classifier()

    # On retire TOUS les events sur la fenetre (pas juste git) pour que le
    # upsert OR IGNORE re-insere tout avec le bon project_id courant.
    with storage.conn() as c:
        cur = c.execute(
            "DELETE FROM events WHERE started_at >= ?",
            (cutoff.isoformat(),),
        )
        deleted_events = cur.rowcount
    console.print(f"  [dim]→ {deleted_events} events purges pour la fenetre[/dim]")

    # Re-run toutes les sources (idem que `ship1000x ingest`). Les collectors
    # vont relire les fichiers source (Claude Code JSONL, Codex sessions,
    # git log) et les inserer avec le classifier V2.
    sources_enabled = privacy_config.get("sources", {})

    def _src_enabled(name: str, default: str = "enabled") -> bool:
        return sources_enabled.get(name, default) == "enabled"

    total_ingested = 0
    for collector_name, enabled_key in RECLASSIFY_COLLECTORS:
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
        console.print("[red]✗[/red] Consent non signe. Lance [cyan]ship1000x init[/cyan] d'abord.")
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
            "  Lance [cyan]ship1000x doctor --fix[/cyan] pour auto-corriger "
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
                "  1. [cyan]ship1000x doctor --fix[/cyan]  — prompt interactif, "
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


@cli.command()
@click.option(
    "--since",
    default="30d",
    help="Window: 7d, 30d, 90d, 365d (default 30d).",
)
@click.option(
    "--threshold-pct",
    default=10.0,
    type=float,
    show_default=True,
    help="Daily-delta % above which a day is flagged as warn.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Save the Markdown report to this file instead of stdout.",
)
@click.option(
    "--pair",
    multiple=True,
    help=(
        "local:billing pair, may be repeated, e.g. "
        "--pair claude_code:anthropic_usage. Defaults cover Claude Code "
        "and Codex when omitted."
    ),
)
def reconcile(since: str, threshold_pct: float, output: str | None, pair: tuple[str, ...]):
    """Reconcile local-collector costs against billing snapshots.

    Compares the cost SHIP estimates from local JSONL/SQLite collectors
    (`claude_code`, `codex`, ...) with the cost reported by billing-side
    adapters (`anthropic_usage`, `openai_usage`) for the same calendar
    day and provider. Reports per-day deltas, model-fallback events,
    and non-factual-cost share.

    Reads only the local SHIP store; never calls a provider API.
    """
    from ship1000x.core.reconcile import (
        DEFAULT_RECONCILE_PAIRS,
        ReconcilePair,
        reconcile_pairs,
        render_markdown_report,
    )

    storage = _get_storage()
    window_days = _since_to_days(since) or 30

    if pair:
        try:
            pairs = tuple(
                ReconcilePair(
                    local=spec.split(":", 1)[0].strip(),
                    billing=spec.split(":", 1)[1].strip(),
                    provider_label=spec.split(":", 1)[0].strip(),
                )
                for spec in pair
                if ":" in spec
            )
        except (IndexError, ValueError):
            console.print("[red]✗[/red] Invalid --pair format; expected local:billing.")
            sys.exit(2)
        if not pairs:
            console.print("[red]✗[/red] No valid --pair provided.")
            sys.exit(2)
    else:
        pairs = DEFAULT_RECONCILE_PAIRS

    reports = reconcile_pairs(
        storage,
        pairs=pairs,
        window_days=window_days,
        threshold_pct=threshold_pct,
    )
    md = render_markdown_report(reports)

    if output:
        Path(output).write_text(md, encoding="utf-8")
        console.print(
            f"[green]✓[/green] Reconciliation report saved to [bold]{output}[/bold] "
            f"({len(reports)} pair(s), window {window_days}d, threshold {threshold_pct}%)."
        )
    else:
        click.echo(md)


@cli.command("audit-cost")
@click.option(
    "--source",
    multiple=True,
    help=(
        "Source to audit, may be repeated. Defaults to claude_code, codex, "
        "anthropic_usage, openai_usage when omitted."
    ),
)
@click.option(
    "--since",
    default="30d",
    help="Window: 7d, 30d, 90d, 365d (default 30d).",
)
@click.option(
    "--top",
    default=10,
    type=int,
    show_default=True,
    help="Top N events per source (sorted by stored API-equivalent cost desc).",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Save the Markdown report to this file instead of stdout.",
)
def audit_cost(source: tuple[str, ...], since: str, top: int, output: str | None):
    """Drill-down audit: justify every API-equivalent cost dollar from tokens × pricing.

    For each event of the chosen source(s), prints the SHIP-stored
    API-equivalent cost, a manual recomputation from per-model tokens × the published
    `pricing.py` rate card, and the per-rate breakdown (uncached
    input / cache_read / cache_write / output). Use this when you want
    to convince yourself an API-equivalent reconcile total like "$26,955"
    is reproducible, line by line.

    Reads only the local SHIP store; never calls a provider API.
    """
    from ship1000x.core.cost_audit import audit_source, render_markdown_report

    storage = _get_storage()
    window_days = _since_to_days(since) or 30
    sources = tuple(s.strip() for s in source if s.strip()) or (
        "claude_code",
        "codex",
        "anthropic_usage",
        "openai_usage",
    )
    reports = [
        audit_source(storage, src, window_days=window_days, top=top)
        for src in sources
    ]
    md = render_markdown_report(reports)
    if output:
        Path(output).write_text(md, encoding="utf-8")
        console.print(
            f"[green]✓[/green] Cost audit report saved to [bold]{output}[/bold] "
            f"({len(sources)} source(s), window {window_days}d, top {top})."
        )
    else:
        click.echo(md)


@cli.command()
@click.option(
    "--since",
    default="30d",
    help="Window: 7d, 30d, 90d, 365d (default 30d).",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Save the report to this file instead of stdout.",
)
@click.option(
    "--format",
    "fmt",
    default="markdown",
    type=click.Choice(["markdown", "json"], case_sensitive=False),
    show_default=True,
    help="Output format.",
)
@click.option(
    "--category",
    default=None,
    help=(
        "Filter to one category (anthropic, openai, coding_ide, "
        "other_llm, dev_code, system, runtime, os_context). Useful "
        "for per-provider drill-downs, e.g. --category openai for a "
        "Codex/ChatGPT-focused summary."
    ),
)
def coverage(since: str, output: str | None, fmt: str, category: str | None):
    """Coverage health report: what SHIP actually observes vs misses.

    Walks the curated source registry (``ship1000x/core/coverage.py``)
    and reports, for every source SHIP could see, whether it is active
    (collector + events in window), supported but quiet (collector
    available, no events on this machine), or not supported at all
    (known audit gap, listed with an audit reference and a one-line
    reason).

    For active sources, surfaces freshness, days covered,
    unknown-model count, auth_mode / cost_basis / cost_quality
    distribution, and api_equivalent vs billed_estimated totals — the
    same numbers ``reconcile`` and ``audit-cost`` consume, but
    aggregated as a health snapshot.

    Reads only the local SHIP store; no network call, no payload
    inspection. Output contains only event-derived metadata (counts,
    ISO dates, distribution counters, rate-card dollar totals).
    """
    from ship1000x.core.coverage import (
        compute_coverage,
        render_json_report,
        render_markdown_report,
    )

    storage = _get_storage()
    window_days = _since_to_days(since) or 30
    healths = compute_coverage(storage, since_days=window_days)
    if category:
        cat = category.strip().lower()
        healths = [h for h in healths if h.entry.category == cat]
        if not healths:
            console.print(
                f"[yellow]No source matches category '{category}'. Try one of: "
                "anthropic, openai, coding_ide, other_llm, dev_code, system, "
                "runtime, os_context.[/yellow]"
            )
            return
    if fmt.lower() == "json":
        rendered = render_json_report(healths, since_days=window_days)
    else:
        rendered = render_markdown_report(healths, since_days=window_days)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
        active = sum(1 for h in healths if h.status == "active")
        not_supported = sum(1 for h in healths if h.status == "not_supported")
        console.print(
            f"[green]✓[/green] Coverage report saved to [bold]{output}[/bold] "
            f"({len(healths)} sources, {active} active, {not_supported} uncovered, "
            f"window {window_days}d)."
        )
    else:
        click.echo(rendered)


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
    # consciemment via `ship1000x projects --select`. En mode cron (non-TTY)
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
                "    [dim]Lance [cyan]ship1000x projects --select[/cyan] "
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
    """Installe le cron launchd pour ship1000x daily (3h du matin par defaut)."""
    from ship1000x.core.scheduler import install as scheduler_install
    try:
        hour, minute = map(int, time_str.split(":"))
    except ValueError:
        console.print(f"[red]✗[/red] Format invalide : {time_str}. Attendu HH:MM")
        return

    try:
        plist = scheduler_install(hour, minute)
        console.print(f"[green]✓[/green] Scheduler installe : {plist}")
        console.print(f"  Prochain run : chaque jour a {time_str}")
        console.print("  Logs : ~/Library/Logs/ship1000x-daily.log et .err.log")
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
        console.print("  Installe avec : [cyan]ship1000x install-scheduler[/cyan]")
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


def _sum_quality_aware_factual_cost(conn, days: int) -> float:
    """Sum cost whose usage metadata says cost quality is factual.

    Legacy fallback keeps older rows useful only for historically factual
    sources that predate `raw_meta.usage`.
    """
    rows = conn.execute(
        """SELECT cost_estimated, raw_meta FROM events
           WHERE date(started_at) >= date('now', ? || ' days')
             AND (
               json_extract(raw_meta, '$.usage.quality.cost') = 'factual'
               OR json_extract(raw_meta, '$.usage.cost.quality') = 'factual'
               OR (
                 json_extract(raw_meta, '$.usage.quality.cost') IS NULL
                 AND json_extract(raw_meta, '$.usage.cost.quality') IS NULL
                 AND source IN ('claude_code', 'anthropic_usage', 'openai_usage', 'openclaw', 'web_exports')
               )
             )""",
        (f"-{int(days)}",),
    ).fetchall()
    return sum(_event_api_equivalent_cost(row) for row in rows)


def _discover_github(owner: str) -> None:
    """List GitHub repos via gh CLI + suggest aliases for unmapped local activity."""
    import json
    import subprocess

    console.print(f"[bold cyan]═══ GitHub discovery : {owner} ═══[/bold cyan]")
    console.print()

    # 1. List repos via gh CLI
    try:
        result = subprocess.run(
            ["gh", "repo", "list", owner, "--limit", "200", "--json", "name,nameWithOwner"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        repos = json.loads(result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        console.print(f"[red]Erreur gh CLI :[/red] {e}")
        console.print("[dim]Installer GitHub CLI : https://cli.github.com[/dim]")
        return
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        console.print(f"[red]Erreur lors de la liste des repos :[/red] {e}")
        return

    console.print(f"[green]✓[/green] {len(repos)} repos trouves dans {owner}")
    console.print()

    # 2. Get all project_ids from DB
    storage = _get_storage()
    with storage.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT project_id, COUNT(*) AS n FROM events GROUP BY project_id ORDER BY n DESC"
        ).fetchall()
    db_pids = {r["project_id"]: r["n"] for r in rows if r["project_id"]}

    # 3. Match : for each GitHub repo, expected canonical id is "github.com/<owner>/<name>" lowercased
    expected_ids = {f"github.com/{r['nameWithOwner'].lower()}": r["name"] for r in repos}

    # 4. Find unmapped : project_ids in DB that look like local folder names
    #    matching a GitHub repo name (= candidate alias targets)
    suggestions: list[tuple[str, str, int]] = []  # (current_pid, suggested_canonical, n_events)
    for pid, n in db_pids.items():
        # Skip already-canonical github.com/ ids
        if pid.startswith("github.com/"):
            continue
        # Skip system buckets
        if pid in ("unclassified", None) or pid.startswith("dir:") or pid.startswith("local:"):
            continue
        # Try to match the pid (or its tail) to a GitHub repo name
        pid_lower = pid.lower()
        for canonical, repo_name in expected_ids.items():
            if pid_lower == repo_name.lower() or pid_lower.endswith(f"/{repo_name.lower()}"):
                suggestions.append((pid, canonical, n))
                break

    if not suggestions:
        console.print("[green]✓ Aucune suggestion d'alias[/green] : tes project_ids matchent deja les repos GitHub")
        return

    console.print(f"[yellow]→ {len(suggestions)} alias suggere(s) :[/yellow]")
    console.print()
    table = Table(show_header=True)
    table.add_column("Current project_id", style="yellow")
    table.add_column("→ Canonical (GitHub)", style="cyan")
    table.add_column("Events", justify="right")
    for current, canonical, n in sorted(suggestions, key=lambda x: -x[2]):
        table.add_row(current, canonical, str(n))
    console.print(table)
    console.print()

    # 5. Print yaml snippet ready to copy
    console.print("[bold]Add to your config/projects.yaml under `aliases:` :[/bold]")
    console.print()
    console.print("[cyan]aliases:[/cyan]")
    for current, canonical, _ in sorted(suggestions, key=lambda x: -x[2]):
        console.print(f"  [cyan]\"{current}\": \"{canonical}\"[/cyan]")
    console.print()
    console.print("[dim]Then run :[/dim]")
    console.print("  [cyan]ship1000x reclassify --since 365d[/cyan]   # propagate to historical events")


@cli.command()
@click.option("--port", default=10000, type=int, help="Port to bind (default 10000)")
@click.option("--no-open", "no_open", is_flag=True, help="Do not auto-open the browser")
def dashboard(port: int, no_open: bool):
    """Launch the local web dashboard (V1.2 MVP).

    Opens http://localhost:<port> in your browser. Bound to localhost
    only — never accepts external connections. Auth-free because local
    user (your machine, your data, your eyes only).

    Pages :
      /          Overview (highlights + trend chart + Trust Score breakdown)
      /projects  Cross-tab projects table (sortable, filterable)

    Stop with Ctrl+C.
    """
    from ship1000x.web.app import run_server
    run_server(DB_PATH, CONFIG_DIR, port=port, open_browser=not no_open)


@cli.command("pricing-status")
def pricing_status_cmd():
    """Fraicheur du snapshot tarifaire + modeles vus sans tarif exact.

    Le cout API-equivalent s'appuie sur un snapshot date (vendore). Les tarifs
    LLM bougent : cette commande dit l'age du snapshot et liste les modeles
    captes qui n'ont pas de tarif exact (a ajouter deliberement, pas devine).
    """
    from ship1000x.core.pricing import pricing_freshness
    fr = pricing_freshness()
    age = fr.get("age_days")
    console.print(f"[bold]Snapshot tarifaire[/bold] : {fr['version']}", end="")
    if age is not None:
        flag = " [red]⚠ perime — rafraichir[/red]" if fr["stale"] else " [green](frais)[/green]"
        console.print(f"  ({age} jours){flag}")
    else:
        console.print()

    storage = _get_storage()
    with storage.conn() as conn:
        rows = conn.execute(
            "SELECT provider, model, SUM(cost_api_equivalent) c, "
            "MAX(pricing_quality) q FROM daily_model_usage "
            "GROUP BY provider, model ORDER BY c DESC"
        ).fetchall()
    fallback = [r for r in rows if r["q"] == "fallback"]
    if fallback:
        console.print("\n[yellow]Modeles sans tarif exact (fallback) — a tarifer :[/yellow]")
        for r in fallback:
            console.print(f"  - {r['provider']}/{r['model']}  (cout estime ${r['c'] or 0:,.0f})".replace(",", " "))
        console.print(
            "\n[dim]Pour tarifer : ajouter le modele dans ship1000x/core/pricing.py "
            "(ANTHROPIC_PRICING / OPENAI_PRICING) puis `ship1000x rollup`.[/dim]"
        )
    else:
        console.print("\n[green]Tous les modeles captes ont un tarif exact.[/green]")


@cli.command()
def pulse():
    """One-line daily check : your habit-forming morning command.

    Shows today + week trend in a single line. Designed to become the
    user's morning ritual : alias it as `pulse` in your shell and check
    it like checking the weather.
    """
    storage = _get_storage()
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = today_start.date().isoformat()
    week_ago = (today_start - timedelta(days=7)).date().isoformat()

    with storage.conn() as c:
        # Today : active sec
        today_row = c.execute(
            """SELECT
                 COALESCE(SUM(duration_sec), 0) AS sec
               FROM events
               WHERE date(started_at) = ? AND source != 'git'""",
            (today_iso,),
        ).fetchone()
        today_h = (today_row["sec"] or 0) / 3600
        today_cost_rows = c.execute(
            """SELECT cost_estimated, raw_meta
               FROM events
               WHERE date(started_at) = ? AND source != 'git'""",
            (today_iso,),
        ).fetchall()
        today_cost = sum(_event_api_equivalent_cost(row) for row in today_cost_rows)
        # Today : commits
        today_commits = c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE date(started_at) = ? AND source = 'git'",
            (today_iso,),
        ).fetchone()["n"] or 0
        # Today : peak parallel sources (heuristic : count distinct active sources)
        today_sources = c.execute(
            """SELECT COUNT(DISTINCT source) AS n FROM events
               WHERE date(started_at) = ? AND source != 'git' AND duration_sec > 0""",
            (today_iso,),
        ).fetchone()["n"] or 0
        # Week comparison (last 7 days excluding today)
        week_row = c.execute(
            """SELECT
                 COALESCE(SUM(duration_sec) / 7.0, 0) AS daily_avg_sec
               FROM events
               WHERE date(started_at) BETWEEN ? AND date(?, '-1 day')
                 AND source != 'git'""",
            (week_ago, today_iso),
        ).fetchone()
        week_avg_h = (week_row["daily_avg_sec"] or 0) / 3600

    # Trend arrow vs week average
    if week_avg_h > 0:
        delta_pct = ((today_h - week_avg_h) / week_avg_h) * 100
        if abs(delta_pct) < 5:
            arrow, color = "→", "white"
        elif delta_pct > 0:
            arrow, color = "↗", "green"
        else:
            arrow, color = "↘", "yellow"
        trend_str = f" [{color}]{arrow} {delta_pct:+.0f}% vs 7d avg[/{color}]"
    else:
        trend_str = ""

    now = datetime.now().strftime("%H:%M")
    pulse_line = (
        f"[bold cyan]🚀 {now}[/bold cyan] · "
        f"[bold]{today_h:.1f}h[/bold] active · "
        f"[bold]${today_cost:.0f}[/bold] · "
        f"[bold]{today_commits}[/bold] commits · "
        f"[bold]{today_sources}[/bold] sources active"
        f"{trend_str}"
    )
    console.print(pulse_line)


@cli.command()
@click.option("--since", default="30d", help="Window ex: 7d, 30d, 90d")
def highlights(since: str):
    """Showcase view : the WOW numbers that demonstrate AI leverage.

    Designed to be the first thing a user sees. Audit-ready, defensible,
    impressive. Highlights what Ship1000x measures that no other tool does.
    """
    from rich.panel import Panel

    storage = _get_storage()
    days = _parse_since_days(since)
    user_email = _get_user_email()

    # 1. Active time (from daily_unified V3 - cross-source dedup, P95 calibrated)
    with storage.conn() as conn:
        unif = conn.execute(
            "SELECT SUM(active_sec_unified) AS u, SUM(wall_clock_sec) AS w, "
            "SUM(agent_sec_additive) AS aa, AVG(threshold_used_sec) AS thr "
            "FROM daily_unified WHERE date >= date('now', ? || ' days')",
            (f"-{days}",),
        ).fetchone()
        # Operator-facing API-equivalent cost.
        cost_rows = conn.execute(
            "SELECT cost_estimated, raw_meta FROM events "
            "WHERE date(started_at) >= date('now', ? || ' days')",
            (f"-{days}",),
        ).fetchall()
        cost = sum(_event_api_equivalent_cost(row) for row in cost_rows)
        # Lines (real defensible)
        lines_real = conn.execute(
            "SELECT SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_real_added'), 0) AS INTEGER)) AS l "
            "FROM events WHERE source = 'git' AND date(started_at) >= date('now', ? || ' days')",
            (f"-{days}",),
        ).fetchone()["l"] or 0
        lines_raw = conn.execute(
            "SELECT SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_added'), 0) AS INTEGER)) AS l "
            "FROM events WHERE source = 'git' AND date(started_at) >= date('now', ? || ' days')",
            (f"-{days}",),
        ).fetchone()["l"] or 0
        # Sources count
        sources_count = conn.execute(
            "SELECT COUNT(DISTINCT source) AS n FROM events "
            "WHERE date(started_at) >= date('now', ? || ' days')",
            (f"-{days}",),
        ).fetchone()["n"] or 0

    active_h = (unif["u"] or 0) / 3600
    agent_additive_h = (unif["aa"] or 0) / 3600
    orchestration_factor = (agent_additive_h / active_h) if active_h else 0
    threshold_min = (unif["thr"] or 0) / 60

    # Cost split factual vs heuristic. Prefer explicit raw_meta.usage quality;
    # keep a legacy fallback for old rows from historically factual sources.
    with storage.conn() as conn:
        cost_factual = _sum_quality_aware_factual_cost(conn, days)
    cost_factual_pct = (cost_factual / cost * 100) if cost else 0

    # Levier unique = travail cumule (sessions //, humain + agents) / temps
    # humain reel dedupliquee. Base sur le temps reellement engage, pas le
    # "temps app ouverte" : auditable, sans plafond heuristique.
    days_equivalent = active_h / 8  # 1 jour-homme = 8h ouvrées
    lines_per_hour = (lines_real / active_h) if active_h else 0
    cost_per_line = (cost / lines_real) if lines_real else 0
    real_pct = (lines_real / lines_raw * 100) if lines_raw else 0

    # Trust Score (raw weighted-average per source, no math hack)
    from ship1000x.insights.trust_score import compute_global_score
    trust = compute_global_score(storage, window_days=days, user_email=user_email)
    trust_checks = trust.get("robustness_checks", [])

    # Confidence labels per metric (Factual/Defensible/Indicative)
    cf_lines = "[Factual]"  # git lines = ground truth
    if cost_factual_pct >= 99.5:
        cf_cost = "[Factual]"
    else:
        cf_cost = (
            f"[{cost_factual_pct:.0f}% native token/pricing coverage; "
            "remainder indicative/unknown]"
        )

    # Format the showcase panel
    lines = []
    lines.append("")
    lines.append(f"  [bold magenta]Effet de levier IA[/bold magenta]           [bold cyan]x{orchestration_factor:.1f}[/bold cyan]        [dim]cumulé ÷ présence réelle[/dim]")
    lines.append(f"  [bold magenta]Équivalent jours-homme[/bold magenta]       [bold cyan]{days_equivalent:.0f} jours[/bold cyan]    [dim](en {days} jours cal.)[/dim]")
    lines.append("")
    lines.append(f"  [bold blue]Présence humaine réelle[/bold blue]     [bold cyan]{active_h:.0f} h[/bold cyan]       [dim]dédupliquée, ≤ 24h/j[/dim]")
    lines.append(f"  [bold blue]Travail cumulé (//)[/bold blue]         [bold cyan]{agent_additive_h:.0f} h[/bold cyan]       [dim]humain + agents //[/dim]")
    lines.append("")
    lines.append(f"  [bold green]Production réelle[/bold green]            [bold cyan]{lines_real:,}[/bold cyan]   [dim]lignes vrai code ({real_pct:.0f}%) {cf_lines}[/dim]".replace(",", " "))
    lines.append(f"  [bold green]Coût API-equivalent[/bold green]          [bold cyan]${cost:,.0f}[/bold cyan]      [dim]{cf_cost}[/dim]".replace(",", " "))
    lines.append(f"  [bold green]API-eq / ligne nette[/bold green]         [bold cyan]${cost_per_line:.4f}[/bold cyan]   [dim]total API-eq / net line[/dim]")
    lines.append("")
    lines.append(f"  [bold yellow]Trust Score[/bold yellow]                  [bold cyan]{trust['score']}/100[/bold cyan]     [dim]{trust['label']} · weighted avg per source (raw)[/dim]")
    lines.append(f"  [bold yellow]Sources captées[/bold yellow]              [bold cyan]{sources_count}[/bold cyan]          [dim]Factual + Defensible[/dim]")
    if trust_checks:
        lines.append("")
        lines.append("  [bold yellow]Robustness checks[/bold yellow]")
        for chk in trust_checks:
            mark = "[green]✓[/green]" if chk["passed"] else "[red]✗[/red]"
            lines.append(f"    {mark} {chk['name']}  [dim]{chk['detail']}[/dim]")
    lines.append("")
    lines.append(f"  [dim]→ Avec 1h de ta présence, ~{orchestration_factor:.1f}h de travail sont abattues[/dim]")
    lines.append(f"  [dim]  (toi + agents en parallèle) et {lines_per_hour:.0f} lignes de vrai code défendable.[/dim]")
    lines.append("")
    lines.append(f"  [dim italic]Présence calculée avec cap_time = {threshold_min:.1f} min (P95 personnel)[/dim italic]")
    lines.append("  [dim italic]Travail cumulé = somme des durées de session (sessions // additionnées)[/dim italic]")

    title = f"🚀 Highlights — derniers {days} jours"
    panel = Panel("\n".join(lines), title=title, border_style="magenta", expand=False)
    console.print()
    console.print(panel)
    console.print()
    console.print(f"[dim]Pour le détail technique : [cyan]ship1000x insights --since {days}d[/cyan][/dim]")


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
                  f"{t['commits']} commits · +{t['lines_added']:,} lignes · ${t['cost']:.2f} API-eq"
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
    console.print(f"  Cout API-eq IA   : ${c['total_usd']} · ${c['per_commit_usd'] or 0:.2f}/commit · "
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

        # Robustness checks (independent qualitative signals, not additive)
        checks = global_score.get("robustness_checks", [])
        if checks:
            console.print()
            console.print("  [bold]Robustness checks[/bold]  [dim](independent setup signals — do not alter the score)[/dim]")
            for chk in checks:
                mark = "[green]✓[/green]" if chk["passed"] else "[red]✗[/red]"
                console.print(f"    {mark} {chk['name']}  [dim]{chk['detail']}[/dim]")
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
    table.add_row("Cout API-eq / commit", f"${r['cost_per_commit']:.2f}" if r["cost_per_commit"] else "—", "USD")
    table.add_row("Cout API-eq / ligne nette", f"${r['cost_per_line_net']:.4f}" if r["cost_per_line_net"] else "—", "USD")
    table.add_row("Cout API-eq / heure", f"${r['cost_per_hour']:.2f}" if r["cost_per_hour"] else "—", "USD")
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
    console.print("[bold]Cout API-equivalent IA (LLM)[/bold]")
    console.print(f"  Total            : ${c['total_usd']}")
    console.print(f"  Par heure        : ${c['per_hour_usd'] or 0:.2f}/h")
    console.print(f"  Par commit       : ${c['per_commit_usd'] or 0:.2f}")
    console.print(f"  Par ligne nette  : ${c['per_line_net_usd'] or 0:.4f}")

    conf = m.get("confidence")
    if conf and conf.get("caveats"):
        console.print()
        console.print(
            f"[dim]Base : lignes {conf.get('lines_basis', 'real')} (vrai code). "
            "Caveats :[/dim]"
        )
        for caveat in conf["caveats"]:
            console.print(f"[dim]  · {caveat}[/dim]")


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
        console.print("[red]✗[/red] Consent non signe. Lance [cyan]ship1000x init[/cyan] d'abord.")
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
    _row("Cout API-eq ($)", ta["cost"], tb["cost"], "${:.2f}")
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
    table.add_row("Cout API-eq", f"${t_cur['cost']:.2f}", f"${t_prev['cost']:.2f}", _fmt_delta(d["cost"]))
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


@cli.command("source-audit")
@click.option("--since", default="30d", help="Fenetre d'audit (defaut 30j)")
@click.option("--json", "json_output", is_flag=True, help="Sortie JSON machine-readable.")
@click.option("--show-absent", is_flag=True, help="Inclut les sources connues absentes de la fenetre.")
@click.option(
    "--fail-under",
    type=float,
    default=None,
    help="Exit 1 si le score moyen des sources observees est sous ce seuil.",
)
@click.option(
    "--fail-on-risk",
    multiple=True,
    type=click.Choice(["fragile", "partial", "unknown-source"]),
    help="Exit 1 si une source observee porte ce risque. Peut etre repete.",
)
@click.option(
    "--strict-public",
    is_flag=True,
    help="Gate vitrine publique: fail-under=70 + fail-on-risk fragile/unknown-source.",
)
def source_audit(
    since: str,
    json_output: bool,
    show_absent: bool,
    fail_under: float | None,
    fail_on_risk: tuple[str, ...],
    strict_public: bool,
):
    """Audit source par source: couverture, usage metadata, qualite et gaps.

    Cette commande est le garde-fou avant dashboard/public claims: elle montre
    les sources observees, les sources absentes, les sources non referencees et
    les collectors qui n'ont pas encore de raw_meta.usage normalise.
    """
    import json as _json

    from ship1000x.core.source_quality import (
        PUBLIC_UNTRUSTED_COLLECTOR_STAGES,
        build_source_quality_report,
    )

    cutoff = _parse_since(since)
    if cutoff is None:
        console.print("[red]Format --since invalide (ex: 7d, 30d, 12h)[/red]")
        return
    delta_days = _since_to_days(since)
    if delta_days is None:
        console.print("[red]Format --since invalide (ex: 7d, 30d, 12h)[/red]")
        return
    report = build_source_quality_report(_get_storage(), window_days=delta_days)
    gate_threshold = 70.0 if strict_public and fail_under is None else fail_under
    gate_risks = set(fail_on_risk)
    gate_stages: set[str] = set()
    if strict_public:
        gate_risks.update({"fragile", "unknown-source"})
        gate_stages.update(PUBLIC_UNTRUSTED_COLLECTOR_STAGES)
    gate_failures = _apply_source_quality_gate(
        report,
        fail_under=gate_threshold,
        fail_on_risk=gate_risks,
        fail_on_collector_stage=gate_stages,
    )

    if json_output:
        click.echo(_json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        if gate_failures:
            raise click.exceptions.Exit(1)
        return

    summary = report["summary"]
    console.print()
    console.print(f"[bold cyan]SOURCE QUALITY AUDIT — {since}[/bold cyan]")
    console.print()
    console.print(f"Known sources              : {summary['known_sources']}")
    console.print(f"Observed sources           : {summary['observed_sources']}")
    console.print(f"Unknown observed sources   : {summary['observed_unknown_sources']}")
    console.print(f"Sources missing usage meta : {summary['sources_missing_usage_metadata']}")
    console.print(f"Fragile sources            : {summary['fragile_sources']}")
    console.print(f"Partial sources            : {summary['partial_sources']}")
    if summary.get("collector_stage_counts"):
        stage_counts = ", ".join(f"{k}:{v}" for k, v in summary["collector_stage_counts"].items())
        console.print(f"Collector stages           : {stage_counts}")
    console.print(f"Public-untrusted stages    : {summary.get('public_untrusted_stage_sources', 0)}")
    if summary.get("average_observed_quality_score") is not None:
        console.print(f"Avg observed quality score : {summary['average_observed_quality_score']:.2f}/100")
    console.print(f"Low quality sources        : {summary.get('low_quality_sources', 0)}")
    console.print(
        "Provider policy snapshots : "
        f"{summary.get('provider_policy_snapshot_events', 0)}/"
        f"{summary.get('usage_events', 0)} valid · "
        f"{summary.get('invalid_provider_policy_snapshot_events', 0)} invalid · "
        f"{summary.get('missing_provider_policy_snapshot_events', 0)} missing · "
        f"{summary.get('provider_policy_snapshot_coverage_pct', 0.0):.1f}% coverage"
    )
    cost_truth = summary.get("cost_truth") or {}
    if cost_truth:
        console.print(
            "Cost truth split          : "
            f"API-equivalent ${cost_truth.get('api_equivalent_usd', 0.0):.2f} · "
            f"billed-est. ${cost_truth.get('billed_estimated_usd', 0.0):.2f} · "
            f"subscription ${cost_truth.get('subscription_absorbed_usd', 0.0):.2f} · "
            f"unknown-basis ${cost_truth.get('unknown_billing_basis_usd', 0.0):.2f} "
            f"({cost_truth.get('events_with_unknown_billing_basis', 0)} unknown-basis events)"
        )
        console.print(
            "Cost truth coverage       : "
            f"{cost_truth.get('events_with_cost_truth', 0)} cost-truth events · "
            f"{cost_truth.get('events_with_unknown_billing_basis', 0)} unknown-basis events"
        )
    console.print()

    risk_style = {
        "ok": "green",
        "partial": "yellow",
        "fragile": "red",
        "unknown-source": "magenta",
        "absent": "dim",
    }

    def _qualities(q: dict) -> str:
        parts = [f"{k[:1]}:{v}" for k, v in q.items() if v]
        return " ".join(parts) if parts else "—"

    rows = report["rows"] if show_absent else [r for r in report["rows"] if r["events"] > 0]
    table = Table(show_lines=False)
    table.add_column("Source")
    table.add_column("Stage")
    table.add_column("Risk")
    table.add_column("Score", justify="right")
    table.add_column("Events", justify="right")
    table.add_column("Usage", justify="right")
    table.add_column("Auth")
    table.add_column("Expected")
    table.add_column("Observed")
    table.add_column("Next action", overflow="fold")
    for row in rows:
        risk = row["risk"]
        style = risk_style.get(risk, "white")
        expected = (
            f"tok={row['expected_quality']['tokens']} "
            f"cost={row['expected_quality']['cost']} "
            f"time={row['expected_quality']['active_time']}"
        )
        observed = (
            f"tok[{_qualities(row['observed_quality']['tokens'])}] "
            f"cost[{_qualities(row['observed_quality']['cost'])}]"
        )
        auth = ", ".join(
            f"{k}:{v}" for k, v in row.get("auth_mode_counts", {}).items()
        ) or "—"
        score = row.get("quality_scores", {}).get("overall")
        score_label = "—" if score is None else f"{score:.0f}/{row.get('quality_band', 'unknown')}"
        table.add_row(
            row["source"],
            str(row.get("collector_stage") or "unknown"),
            f"[{style}]{risk}[/{style}]",
            score_label,
            str(row["events"]),
            f"{row['usage_coverage_pct']:.0f}%",
            auth,
            expected,
            observed,
            row["next_action"],
        )
    console.print(table)

    missing = report.get("missing_usage_metadata_sources") or []
    if missing:
        console.print()
        console.print("[yellow]Sources observed but missing normalized usage metadata:[/yellow]")
        console.print("  " + ", ".join(missing))
    unknown_billing = [
        (
            row["source"],
            row.get("cost_truth", {}).get("unknown_billing_basis_usd", 0.0),
            row.get("cost_truth", {}).get("events_with_unknown_billing_basis", 0),
        )
        for row in rows
        if row.get("cost_truth", {}).get("unknown_billing_basis_usd", 0.0)
    ]
    if unknown_billing:
        console.print()
        console.print(
            "[yellow]Sources with unknown billing basis "
            "(API-equivalent vs billed-estimated cannot be split):[/yellow]"
        )
        console.print(
            "  "
            + ", ".join(
                f"{source} ${amount:.2f} ({events} event{'s' if events != 1 else ''})"
                for source, amount, events in unknown_billing
            )
        )
    if gate_threshold is not None or gate_risks:
        console.print()
        if gate_failures:
            console.print("[red]Source quality gate failed:[/red]")
            for failure in gate_failures:
                console.print(f"  - {failure}")
        else:
            console.print("[green]Source quality gate passed.[/green]")
    console.print()
    if gate_failures:
        raise click.exceptions.Exit(1)


@cli.command("observation-audit")
@click.option("--since", default="30d", help="Fenetre d'audit (defaut 30j)")
@click.option("--json", "json_output", is_flag=True, help="Sortie JSON machine-readable.")
def observation_audit(since: str, json_output: bool):
    """Audit des capacites d'observation: implemente, partiel, planned, deferred."""
    import json as _json

    from ship1000x.core.observation_coverage import build_observation_audit_report

    cutoff = _parse_since(since)
    if cutoff is None:
        console.print("[red]Format --since invalide (ex: 7d, 30d, 12h)[/red]")
        return
    delta_days = _since_to_days(since)
    if delta_days is None:
        console.print("[red]Format --since invalide (ex: 7d, 30d, 12h)[/red]")
        return

    report = build_observation_audit_report(_get_storage(), window_days=delta_days)

    if json_output:
        click.echo(_json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return

    summary = report["summary"]
    console.print()
    console.print(f"[bold cyan]OBSERVATION AUDIT — {since}[/bold cyan]")
    console.print()
    console.print(f"Capabilities       : {summary['capabilities']}")
    console.print(f"Implemented        : {summary['implemented']}")
    console.print(f"Partial            : {summary['partial']}")
    console.print(f"Planned            : {summary['planned']}")
    console.print(f"Deferred           : {summary['deferred']}")
    console.print(f"Observed sources   : {summary['observed_sources']}")
    console.print(f"Unknown sources    : {summary['unknown_observed_sources']}")
    console.print(f"Provider capabilities : {summary['provider_capabilities']}")
    console.print(f"Provider needs fixture: {summary['provider_needs_fixture']}")
    console.print(f"Provider planned      : {summary['provider_planned']}")
    console.print(f"Audit source       : {report['audit_source']}")
    console.print()

    status_style = {
        "implemented": "green",
        "partial": "yellow",
        "planned": "cyan",
        "deferred": "dim",
        "supported": "green",
        "needs-fixture": "red",
    }
    table = Table(show_lines=False)
    table.add_column("Capability")
    table.add_column("Category")
    table.add_column("Status")
    table.add_column("Gap", overflow="fold")
    table.add_column("Next action", overflow="fold")
    for row in report["rows"]:
        status = row["status"]
        style = status_style.get(status, "white")
        table.add_row(
            row["label"],
            row["category"],
            f"[{style}]{status}[/{style}]",
            row["gap"],
            row["next_action"],
        )
    console.print(table)
    console.print()
    provider_table = Table(title="Provider capability gaps", show_lines=False)
    provider_table.add_column("Provider/tool")
    provider_table.add_column("Status")
    provider_table.add_column("Current coverage", overflow="fold")
    provider_table.add_column("Missing", overflow="fold")
    provider_table.add_column("Public claim", overflow="fold")
    for row in report["provider_capabilities"]:
        status = row["status"]
        style = status_style.get(status, "white")
        provider_table.add_row(
            row["label"],
            f"[{style}]{status}[/{style}]",
            row["current_coverage"],
            row["missing"],
            row["public_claim"],
        )
    console.print(provider_table)
    if report["unknown_observed_sources"]:
        console.print()
        console.print("[yellow]Unknown observed sources:[/yellow]")
        console.print("  " + ", ".join(report["unknown_observed_sources"]))
    console.print()


@cli.command("history-audit")
@click.option("--since", default="365d", help="Fenetre d'audit (defaut 365j)")
@click.option("--json", "json_output", is_flag=True, help="Sortie JSON machine-readable.")
def history_audit(since: str, json_output: bool):
    """Audit read-only de recuperabilite historique par source."""
    import json as _json

    from ship1000x.core.history_recoverability import build_history_recoverability_report

    delta_days = _since_to_days(since)
    if delta_days is None:
        console.print("[red]Format --since invalide (ex: 7d, 30d, 12h)[/red]")
        raise click.exceptions.Exit(2)

    report = build_history_recoverability_report(_get_storage(), window_days=delta_days)

    if json_output:
        click.echo(_json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return

    summary = report["summary"]
    console.print()
    console.print(f"[bold cyan]HISTORY RECOVERABILITY AUDIT — {since}[/bold cyan]")
    console.print()
    console.print(f"Observed sources       : {summary['observed_sources']}")
    console.print(f"Events                 : {summary['events']}")
    console.print(f"Usage metadata events  : {summary['usage_events']}")
    console.print(f"Recoverable sources    : {summary['recoverable_sources']}")
    console.print(f"Partial sources        : {summary['partial_sources']}")
    console.print(f"External-source needed : {summary['external_source_required_sources']}")
    console.print(f"Not recoverable        : {summary['not_recoverable_from_ship_sources']}")
    console.print(f"Malformed metadata     : {summary['malformed_meta_events']}")
    console.print()

    status_style = {
        "recoverable": "green",
        "partial": "yellow",
        "external-source-required": "cyan",
        "not-recoverable-from-ship": "red",
    }
    table = Table(show_lines=False)
    table.add_column("Source", no_wrap=True)
    table.add_column("Recoverability", no_wrap=True)
    table.add_column("Repair scope", overflow="fold")
    table.add_column("Events", justify="right")
    table.add_column("Usage", justify="right")
    table.add_column("Malformed", justify="right")
    table.add_column("Recommendation", overflow="fold")
    for row in report["rows"]:
        recoverability = row["recoverability"]
        style = status_style.get(recoverability, "white")
        table.add_row(
            row["source"],
            f"[{style}]{recoverability}[/{style}]",
            row["repair_scope"],
            str(row["events"]),
            str(row["usage_events"]),
            str(row["malformed_meta_events"]),
            row["recommendation"],
        )
    console.print(table)
    detail_rows = [
        row
        for row in report["rows"]
        if row["recoverability"] != "recoverable"
        or int(row["malformed_meta_events"]) > 0
        or int(row["usage_events"]) == 0
    ]
    if detail_rows:
        console.print()
        console.print("[bold]Repair details:[/bold]")
        for row in detail_rows:
            console.print(f"  - {row['source']}: {row['command']}")
            console.print(f"    scope: {row['repair_scope']}")
            console.print(f"    recommendation: {row['recommendation']}")
            if row["unrecoverable_truth_fields"]:
                console.print(
                    "    cannot repair: "
                    + ", ".join(str(field) for field in row["unrecoverable_truth_fields"])
                )
            console.print(f"    boundary: {row['claim_boundary']}")
            console.print(f"    caveat: {row['caveat']}")
    console.print()
    console.print(f"[dim]{report['operator_guidance']}[/dim]")
    console.print()


@cli.command("public-check")
@click.option("--since", default="30d", help="Fenetre de verification (defaut 30j)")
@click.option("--json", "json_output", is_flag=True, help="Sortie JSON machine-readable.")
@click.option("--fail-under", type=float, default=70.0, help="Seuil minimal du score qualite observe.")
@click.option(
    "--strict-history",
    is_flag=True,
    help="Echoue aussi si l'historique contient des sources partielles, externes, non reparables ou des metadonnees malformees.",
)
def public_check(since: str, json_output: bool, fail_under: float, strict_history: bool):
    """Gate unique avant demo publique: source quality + observation coverage."""
    import json as _json

    from ship1000x.core.history_recoverability import (
        build_history_recoverability_report,
        strict_history_failures_from_report,
    )
    from ship1000x.core.observation_coverage import build_observation_audit_report
    from ship1000x.core.public_claims import (
        build_public_claim_readiness,
        build_public_proof_sequence,
    )
    from ship1000x.core.source_quality import (
        PUBLIC_UNTRUSTED_COLLECTOR_STAGES,
        build_source_quality_report,
    )

    delta_days = _since_to_days(since)
    if delta_days is None:
        console.print("[red]Format --since invalide (ex: 7d, 30d, 12h)[/red]")
        raise click.exceptions.Exit(2)

    storage = _get_storage()
    source_report = build_source_quality_report(storage, window_days=delta_days)
    source_failures = _apply_source_quality_gate(
        source_report,
        fail_under=fail_under,
        fail_on_risk={"fragile", "unknown-source"},
        fail_on_collector_stage=set(PUBLIC_UNTRUSTED_COLLECTOR_STAGES),
    )
    observation_report = build_observation_audit_report(storage, window_days=delta_days)
    observation_warnings = [
        f"{row['key']} is {row['status']}"
        for row in observation_report["rows"]
        if row["status"] in {"planned", "deferred"}
    ]
    provider_gaps = [
        row
        for row in observation_report["provider_capabilities"]
        if row["status"] in {"needs-fixture", "planned", "deferred"}
    ]
    provider_warnings = [
        f"{row['key']} is {row['status']}: {row['public_claim']}" for row in provider_gaps
    ]
    history_report = build_history_recoverability_report(storage, window_days=delta_days)
    history_summary = history_report["summary"]
    claim_readiness = build_public_claim_readiness(
        source_report=source_report,
        observation_report=observation_report,
        history_report=history_report,
    )
    history_warnings = strict_history_failures_from_report(history_report)
    history_failures = history_warnings if strict_history else []
    all_failures = source_failures + history_failures
    public_proof_sequence = build_public_proof_sequence(
        since=since,
        fail_under=fail_under,
        strict_history=strict_history,
        source_report=source_report,
        observation_report=observation_report,
        history_report=history_report,
        claim_readiness=claim_readiness,
        source_failures=source_failures,
        history_failures=history_failures,
    )

    report = {
        "schema_version": "ship1000x.public_check_report.v1",
        "window_days": delta_days,
        "proof_status": public_proof_sequence["status"],
        "presentation_status": public_proof_sequence["status"],
        # Backward-compatible gate/exit-code boolean. `passed=true` can still
        # have `proof_status=caveat` when blocked claims remain visible.
        "passed": not all_failures,
        "source_quality": {
            "passed": not source_failures,
            "summary": source_report["summary"],
            "gate": source_report["gate"],
        },
        "observation_audit": {
            "summary": observation_report["summary"],
            "warnings": observation_warnings,
        },
        "provider_coverage": {
            "summary": {
                "provider_capabilities": observation_report["summary"]["provider_capabilities"],
                "provider_supported": observation_report["summary"]["provider_supported"],
                "provider_partial": observation_report["summary"]["provider_partial"],
                "provider_needs_fixture": observation_report["summary"]["provider_needs_fixture"],
                "provider_planned": observation_report["summary"]["provider_planned"],
                "provider_deferred": observation_report["summary"]["provider_deferred"],
            },
            "gaps": provider_gaps,
            "warnings": provider_warnings,
        },
        "public_claim_readiness": claim_readiness,
        "public_proof_sequence": public_proof_sequence,
        "history_recoverability": {
            "passed": not history_failures,
            "strict": strict_history,
            "summary": history_summary,
            "warnings": history_warnings,
            "failures": history_failures,
        },
        "failures": all_failures,
        "next_action": (
            "Public demo/report is safe to present with visible caveats."
            if public_proof_sequence["status"] == "pass"
            else "Public demo/report is usable only with visible caveats; quote allowed claims only."
            if public_proof_sequence["status"] == "caveat"
            else "Fix blocking quality/history failures before using SHIP as a public proof point."
        ),
    }

    if json_output:
        click.echo(_json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        if all_failures:
            raise click.exceptions.Exit(1)
        return

    proof_status = public_proof_sequence["status"]
    if proof_status == "pass":
        status = "[green]PASS[/green]"
    elif proof_status == "caveat":
        status = "[yellow]CAVEAT[/yellow]"
    else:
        status = "[red]NO GO[/red]"
    console.print()
    console.print(f"[bold cyan]PUBLIC CHECK — {since}[/bold cyan]  {status}")
    console.print()
    sq = source_report["summary"]
    avg = sq.get("average_observed_quality_score")
    avg_label = "not measured" if avg is None else f"{float(avg):.2f}/100"
    console.print(f"Average source quality : {avg_label}")
    console.print(f"Low quality sources    : {sq.get('low_quality_sources', 0)}")
    console.print(f"Missing usage metadata : {sq.get('sources_missing_usage_metadata', 0)}")
    console.print(f"Untrusted source stages: {sq.get('public_untrusted_stage_sources', 0)}")
    console.print(f"Observation planned    : {observation_report['summary']['planned']}")
    console.print(f"Observation deferred   : {observation_report['summary']['deferred']}")
    console.print(f"Provider gaps visible  : {len(provider_gaps)}")
    console.print(
        "History recoverability : "
        f"{history_summary['recoverable_sources']} recoverable / "
        f"{history_summary['partial_sources']} partial / "
        f"{history_summary['not_recoverable_from_ship_sources']} not recoverable"
    )
    claim_summary = claim_readiness["summary"]
    console.print(
        "Public claims          : "
        f"{claim_summary.get('allowed', 0)} allowed / "
        f"{claim_summary.get('conditional', 0)} conditional / "
        f"{claim_summary.get('blocked', 0)} blocked"
    )
    console.print()
    console.print("[bold]Allowed public claims:[/bold]")
    for claim in [c for c in claim_readiness["claims"] if c["status"] == "allowed"]:
        console.print(f"  - {claim['label']}: {claim['evidence']}")
        console.print(f"    caveat: {claim['caveat']}")
    conditional_claims = [
        c for c in claim_readiness["claims"] if c["status"] == "conditional"
    ]
    if conditional_claims:
        console.print()
        console.print("[bold yellow]Conditional public claims:[/bold yellow]")
        for claim in conditional_claims:
            console.print(f"  - {claim['label']}: {claim['evidence']}")
            console.print(f"    caveat: {claim['caveat']}")
    console.print()
    console.print("[bold]Blocked public claims:[/bold]")
    for claim in [c for c in claim_readiness["claims"] if c["status"] == "blocked"]:
        console.print(f"  - {claim['label']}: {claim['evidence']}")
        console.print(f"    caveat: {claim['caveat']}")
    console.print()
    console.print("[bold]Public proof sequence:[/bold]")
    for step in public_proof_sequence["commands"]:
        console.print(f"  - {step['key']}: {step['status']}")
        console.print(f"    command: {step['command']}")
        console.print(f"    evidence: {step['evidence']}")
        console.print(f"    caveat: {step['caveat']}")
    if source_failures:
        console.print()
        console.print("[red]Failures:[/red]")
        for failure in source_failures:
            console.print(f"  - {failure}")
    if observation_warnings:
        console.print()
        console.print("[yellow]Known observation gaps (not blocking):[/yellow]")
        for warning in observation_warnings[:5]:
            console.print(f"  - {warning}")
    if provider_warnings:
        console.print()
        console.print("[yellow]Known provider gaps (not blocking):[/yellow]")
        for warning in provider_warnings[:8]:
            console.print(f"  - {warning}")
    if history_warnings:
        console.print()
        if strict_history:
            console.print("[red]History recoverability failures:[/red]")
        else:
            console.print("[yellow]Known history recoverability caveats (not blocking):[/yellow]")
        for warning in history_warnings[:8]:
            console.print(f"  - {warning}")
    history_repair_risks = claim_readiness.get("history_repair_risks") or []
    if history_repair_risks:
        console.print()
        console.print("[yellow]History repair risks:[/yellow]")
        for risk in history_repair_risks[:8]:
            source = risk.get("source", "unknown")
            recoverability = risk.get("recoverability", "unknown")
            repair_scope = risk.get("repair_scope", "visible-evidence-only")
            malformed = int(risk.get("malformed_meta_events") or 0)
            recommendation = risk.get("recommendation") or "Rerun history-audit before making public claims."
            boundary = risk.get("claim_boundary") or "No claim boundary supplied."
            unrecoverable = risk.get("unrecoverable_truth_fields") or []
            console.print(
                f"  - {source}: {recoverability}; scope={repair_scope}; "
                f"malformed_meta_events={malformed}"
            )
            console.print(f"    recommendation: {recommendation}")
            if unrecoverable:
                console.print(
                    "    cannot repair: "
                    + ", ".join(str(field) for field in unrecoverable)
                )
            console.print(f"    boundary: {boundary}")
    console.print()
    console.print(report["next_action"])
    console.print()
    if all_failures:
        raise click.exceptions.Exit(1)


@cli.command()
@click.option(
    "--save",
    is_flag=True,
    help="Ecrit les paths decouverts dans privacy.yaml (section discovered_paths).",
)
@click.option(
    "--github",
    "github_owner",
    default=None,
    help="GitHub owner/org : liste les repos GitHub et suggere des aliases pour ceux qui ont une activite locale non-mappee.",
)
def discover(save: bool, github_owner: str | None):
    """Scan HOME pour trouver tous les emplacements d'outils IA (Claude Code, Codex, Cursor).

    Utile si tu as des installs dans des dossiers non-standard. Par defaut
    les collectors ne scannent que ~/.claude/projects, ~/.codex/sessions,
    ~/.cursor/ai-tracking. Cette commande detecte les copies/reloges.

    Avec --save, ajoute les paths trouves a privacy.yaml → pris en compte
    au prochain `ship1000x ingest`.

    Avec --github <owner>, interroge l'API GitHub (via `gh` CLI) pour
    lister les repos de ton org/user et suggere les aliases necessaires
    pour les project_ids qui ne matchent pas un repo connu.
    """
    if github_owner:
        _discover_github(github_owner)
        return
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
            "  Lance [cyan]ship1000x ingest[/cyan] pour que les collectors les utilisent."
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
                                "  Les prochains [cyan]ship1000x push[/cyan] utiliseront ces credentials."
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
        console.print("  [red]x[/red] Consent non signe. Lance [cyan]ship1000x init[/cyan]")
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
        from ship1000x.core import scheduler as _sched
        status = _sched.status()
        if isinstance(status, dict) and status.get("installed"):
            console.print(f"  [green]v[/green] Cron installe a {status.get('time', '-')}")
        else:
            console.print("  [yellow]o[/yellow] Pas de cron installe. Lance [cyan]ship1000x install-scheduler[/cyan]")
    except Exception:
        console.print("  [dim](scheduler.status() non dispo)[/dim]")
    console.print()

    # 6. Suggestions
    console.print("[bold]6. Suggestions[/bold]")
    suggestions = []
    for s in sources:
        if s.id == "shell" and s.status != "tracked":
            suggestions.append("  - Activer EXTENDED_HISTORY dans ~/.zshrc (ship1000x check-shell-config)")
        if s.id == "mac_system" and s.status == "disabled":
            suggestions.append("  - Optionnel: activer mac_system dans privacy.yaml pour wake/sleep cross-check")
        if s.id == "claude_desktop" and s.path_exists and s.status == "not_tracked":
            suggestions.append("  - Exporter claude.ai: Settings > Privacy > Export data dans drop folder")
        if s.id == "codex_desktop" and s.path_exists and s.status == "not_tracked":
            suggestions.append("  - Codex Desktop: les logs state_5.sqlite sont captures via codex_desktop_logs")
    suggestions.append("  - Multi-Mac: installer ship1000x sur chaque machine avec meme user_email")
    suggestions.append("  - Lance [cyan]ship1000x audit --since 30d[/cyan] pour voir les gaps commits vs sessions")
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


@cli.command()
@click.option(
    "--foreground/--no-foreground",
    default=True,
    help="Run in foreground (V1 default and only supported mode).",
)
@click.option(
    "--interval",
    default=30,
    type=int,
    help="Polling interval in seconds (default 30, minimum 5).",
)
@click.option(
    "--max-ticks",
    default=None,
    type=int,
    help="Stop after N ticks (testing; production runs unbounded).",
)
def watch(foreground: bool, interval: int, max_ticks: int | None):
    """Foreground daemon: poll AI processes + listening ports every N seconds.

    Writes one snapshot per tick to
    ``~/.ship1000x/drop/watch/<YYYY-MM-DD>.jsonl``. The regular
    ``ship1000x ingest --source agent_runtime`` run aggregates these
    ticks into per-minute ``agent_runtime_tick`` events.

    Requires the optional ``psutil`` dependency. Install with::

        pip install ship1000x[watch]

    V1 is foreground-only by explicit design. No LaunchAgent / systemd
    install. Stop with Ctrl-C.
    """
    if not foreground:
        console.print(
            "[yellow]Background mode is not supported in V1.[/yellow] "
            "Re-run without --no-foreground."
        )
        sys.exit(2)
    from ship1000x.runtime.watch_daemon import run_foreground
    rc = run_foreground(interval_sec=interval, max_ticks=max_ticks)
    sys.exit(rc)


def main() -> None:
    """Entry point wrapper for pyproject.toml [project.scripts]."""
    cli()


if __name__ == "__main__":
    main()
