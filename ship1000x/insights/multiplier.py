"""Multiplicateur IA-native — D3.

Calcule les facteurs de comparaison vs benchmarks humain senior sans IA.
Produit des chiffres pitch-ready pour les RDV commerciaux.
"""

from __future__ import annotations

from typing import Any

from ship1000x.insights.benchmarks import load_benchmarks
from ship1000x.insights.engine import Window, compute_overview


def compute_multiplier(
    storage,
    window: Window,
    tjm_eur_per_day: float | None = None,
    value_produit_eur: float | None = None,
    workday_hours: int | None = None,
) -> dict[str, Any]:
    """Calcule les multiplicateurs IA-native.

    Args:
        tjm_eur_per_day : TJM senior (EUR/jour). Defaut = mid des benchmarks.
        value_produit_eur : Valeur produit livre (audit agence). Defaut = mid.
        workday_hours : H/jour pour convertir h -> jours-equivalent. Defaut = 8.

    Returns:
        Dict avec : output_factor (vs senior), tjm_equivalent_eur,
        value_time_ratio, cost_per_commit, cost_per_line, etc.
    """
    b = load_benchmarks()
    tjm = tjm_eur_per_day if tjm_eur_per_day is not None else b["tjm_senior_mid"]
    value_mvp = value_produit_eur if value_produit_eur is not None else b["value_mvp_saas_mid"]
    wday = workday_hours if workday_hours is not None else b["workday_hours"]

    overview = compute_overview(storage, window)
    active_hours = overview["totals"]["active_hours"]
    lines_added = overview["totals"]["lines_added"]
    commits = overview["totals"]["commits"]
    cost = overview["totals"]["cost"]
    lines_net = lines_added - overview["totals"]["lines_deleted"]

    # Facteur production : lignes/h vs benchmark senior
    lines_per_hour = (lines_added / active_hours) if active_hours else 0.0
    factor_low = lines_per_hour / b["lines_per_hour_no_ai_high"] if b["lines_per_hour_no_ai_high"] else None
    factor_mid = lines_per_hour / b["lines_per_hour_no_ai_mid"] if b["lines_per_hour_no_ai_mid"] else None
    factor_high = lines_per_hour / b["lines_per_hour_no_ai_low"] if b["lines_per_hour_no_ai_low"] else None

    # TJM equivalent : (h/wday) * tjm
    days_equivalent = active_hours / wday if wday else 0.0
    tjm_equivalent_eur = days_equivalent * tjm

    # Ratio valeur/temps (sur la base de tjm_equivalent vs valeur produit)
    value_time_ratio = (value_mvp / tjm_equivalent_eur) if tjm_equivalent_eur > 0 else None

    # Couts unitaires (en dollars)
    cost_per_commit = (cost / commits) if commits else None
    cost_per_line = (cost / max(1, lines_net)) if lines_net > 0 else None
    cost_per_hour = (cost / active_hours) if active_hours else None

    return {
        "inputs": {
            "tjm_eur_per_day": tjm,
            "value_produit_eur": value_mvp,
            "workday_hours": wday,
        },
        "output": {
            "lines_per_hour": round(lines_per_hour, 1),
            "benchmark_senior_low": b["lines_per_hour_no_ai_low"],
            "benchmark_senior_high": b["lines_per_hour_no_ai_high"],
            "factor_vs_senior_low": round(factor_low, 1) if factor_low else None,
            "factor_vs_senior_mid": round(factor_mid, 1) if factor_mid else None,
            "factor_vs_senior_high": round(factor_high, 1) if factor_high else None,
        },
        "value": {
            "active_hours": round(active_hours, 1),
            "days_equivalent": round(days_equivalent, 1),
            "tjm_equivalent_eur": round(tjm_equivalent_eur, 0),
            "value_produit_eur": value_mvp,
            "value_time_ratio": round(value_time_ratio, 1) if value_time_ratio else None,
        },
        "cost": {
            "total_usd": round(cost, 2),
            "per_hour_usd": round(cost_per_hour, 2) if cost_per_hour else None,
            "per_commit_usd": round(cost_per_commit, 2) if cost_per_commit else None,
            "per_line_net_usd": round(cost_per_line, 4) if cost_per_line else None,
        },
    }
