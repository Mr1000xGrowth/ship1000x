"""Scheduler launchd macOS.

Genere un plist en dur (pas de fichier template externe), l'installe dans
~/Library/LaunchAgents et le charge via `launchctl bootstrap gui/$UID`. Le
plist lance chaque nuit `ship1000x daily` qui chaine ingest + rollup + push.

launchd n'herite ni du PATH ni du venv courant : on resout donc le binaire
`ship1000x` vers un chemin ABSOLU au moment de l'installation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.mr1000xgrowth.ship1000x"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs"


def _plist_path() -> Path:
    return LAUNCH_AGENTS / f"{LABEL}.plist"


def _domain_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _resolve_ship_args() -> list[str]:
    """Resout l'invocation absolue de la commande `ship1000x daily`.

    launchd ne voit pas le PATH du shell ni le venv actif, il faut un chemin
    absolu. Ordre : binaire sur le PATH courant -> binaire a cote de
    l'interpreteur (venv/bin) -> module via l'interpreteur courant.
    """
    found = shutil.which("ship1000x")
    if found:
        return [str(Path(found).resolve()), "daily"]

    # Console script installe dans le bin du venv/prefix courant.
    for base in (Path(sys.prefix), Path(sys.executable).resolve().parent.parent):
        candidate = base / "bin" / "ship1000x"
        if candidate.exists():
            return [str(candidate), "daily"]

    # Dernier recours : module via l'interpreteur courant (chemin absolu).
    return [sys.executable, "-m", "ship1000x", "daily"]


def _render_plist(hour: int, minute: int) -> str:
    program_args = "".join(
        f"    <string>{arg}</string>\n" for arg in _resolve_ship_args()
    )
    stdout_log = LOG_DIR / "ship1000x-daily.log"
    stderr_log = LOG_DIR / "ship1000x-daily.err.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{program_args}  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{stdout_log}</string>
  <key>StandardErrorPath</key>
  <string>{stderr_log}</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""


def install(hour: int, minute: int) -> str:
    """Installe le plist launchd (agent daily). Retourne le chemin du fichier."""
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError(f"Heure invalide : {hour:02d}:{minute:02d}")

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = _plist_path()

    plist_path.write_text(_render_plist(hour, minute))

    # Valide le plist avant de charger (echec rapide, message clair).
    lint = subprocess.run(
        ["plutil", "-lint", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if lint.returncode != 0:
        raise RuntimeError(f"plist invalide : {lint.stdout.strip() or lint.stderr.strip()}")

    # Idempotent : bootout si deja charge, puis bootstrap.
    target = _domain_target()
    if subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", target], capture_output=True)

    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap echec : {result.stderr.strip() or result.stdout.strip()}")

    return str(plist_path)


def uninstall() -> bool:
    """Desinstalle le scheduler. Retourne True si qqch a ete supprime."""
    plist_path = _plist_path()
    if not plist_path.exists():
        return False

    subprocess.run(["launchctl", "bootout", _domain_target()], capture_output=True)
    plist_path.unlink()
    return True


def is_installed() -> bool:
    return _plist_path().exists()


def status() -> dict:
    plist_path = _plist_path()
    if not plist_path.exists():
        return {"installed": False}

    result = subprocess.run(
        ["launchctl", "print", _domain_target()],
        capture_output=True,
        text=True,
    )
    return {
        "installed": True,
        "plist_path": str(plist_path),
        "loaded": result.returncode == 0,
    }
