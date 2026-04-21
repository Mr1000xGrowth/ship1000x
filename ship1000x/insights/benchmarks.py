"""Benchmarks de reference pour le calcul des multiplicateurs IA-native.

Toutes les valeurs sont ajustables via `config/benchmarks.yaml` si besoin.
Les valeurs par defaut sont calibrees France 2026 pour un dev senior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULTS: dict[str, Any] = {
    # TJM senior dev (EUR/jour)
    "tjm_senior_low": 600,
    "tjm_senior_mid": 900,
    "tjm_senior_high": 1200,

    # Lignes de code produites par heure, dev senior SANS IA (industrie)
    "lines_per_hour_no_ai_low": 20,
    "lines_per_hour_no_ai_mid": 35,
    "lines_per_hour_no_ai_high": 50,

    # Valeur livrable "MVP SaaS V1" par une agence Tier-1 (EUR)
    "value_mvp_saas_low": 80_000,
    "value_mvp_saas_mid": 150_000,
    "value_mvp_saas_high": 250_000,

    # Seuils burnout / alertes
    "burnout_long_session_h": 10,           # session > Xh = suspect
    "burnout_long_session_count_7d": 3,     # > X sessions longues sur 7j = alerte
    "burnout_night_hour_start": 22,         # heure >= 22h = nuit
    "burnout_night_hour_end": 6,            # heure < 6h = nuit
    "burnout_night_ratio_pct": 25,          # > 25% du temps la nuit = alerte
    "burnout_consecutive_days": 7,          # > 7 jours consecutifs sans pause = alerte

    # Seuils derives projet
    "derive_estimate_delta_pct": 20,        # ecart reel vs estime > 20% = alerte
    "derive_secondary_project_pct": 30,     # projet "secondaire" > 30% du temps = alerte

    # Seuils blocages
    "stuck_no_commits_days": 2,             # prompts sans commits pendant > X jours = alerte
    "stuck_output_drop_pct": 60,            # chute lines/typed > 60% = alerte

    # Heures d'une journee de travail "normale"
    "workday_hours": 8,

    # Fuseau horaire pour heures locales
    "timezone": "Europe/Paris",
}


def load_benchmarks(config_path: Path | None = None) -> dict[str, Any]:
    """Charge les benchmarks : DEFAULTS ecrasables par config/benchmarks.yaml.

    Si le fichier existe, on merge les valeurs. Sinon on retourne DEFAULTS.
    """
    merged = dict(DEFAULTS)
    if config_path is None:
        repo_root = Path(__file__).parent.parent
        config_path = repo_root / "config" / "benchmarks.yaml"
    if config_path.exists():
        try:
            user = yaml.safe_load(config_path.read_text()) or {}
            if isinstance(user, dict):
                merged.update(user)
        except yaml.YAMLError:
            pass
    return merged
