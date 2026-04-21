"""Scheduler launchd macOS.

Genere un plist a partir du template, l'installe dans ~/Library/LaunchAgents,
le charge avec launchctl. Le plist lance chaque nuit `ship1000x daily` qui
chaine ingest + rollup + push.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


LABEL = "com.mr1000xgrowth.ship1000x"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


def install(repo_root: Path, hour: int, minute: int) -> str:
    """Installe le plist launchd. Retourne le chemin du fichier cree."""
    template_path = repo_root / "scheduler" / "launchd.plist.template"
    if not template_path.exists():
        raise RuntimeError(f"Template introuvable : {template_path}")

    plist_content = template_path.read_text()
    plist_content = plist_content.replace("__TRACKER_DIR__", str(repo_root))
    plist_content = plist_content.replace("__HOME__", str(Path.home()))
    plist_content = plist_content.replace("__HOUR__", str(hour))
    plist_content = plist_content.replace("__MINUTE__", str(minute))

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist_path = LAUNCH_AGENTS / f"{LABEL}.plist"

    # Unload si deja present (avant d'ecraser)
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
        )

    plist_path.write_text(plist_content)

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launchctl load echec : {result.stderr}")

    return str(plist_path)


def uninstall() -> bool:
    """Desinstalle le scheduler. Retourne True si qqch a ete supprime."""
    plist_path = LAUNCH_AGENTS / f"{LABEL}.plist"
    if not plist_path.exists():
        return False

    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
    )
    plist_path.unlink()
    return True


def is_installed() -> bool:
    plist_path = LAUNCH_AGENTS / f"{LABEL}.plist"
    return plist_path.exists()


def status() -> dict:
    plist_path = LAUNCH_AGENTS / f"{LABEL}.plist"
    if not plist_path.exists():
        return {"installed": False}

    result = subprocess.run(
        ["launchctl", "list", LABEL],
        capture_output=True,
        text=True,
    )
    return {
        "installed": True,
        "plist_path": str(plist_path),
        "loaded": result.returncode == 0,
        "launchctl_output": result.stdout.strip() if result.stdout else result.stderr.strip(),
    }
