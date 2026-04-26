"""Exporter rapport Markdown.

Genere un rapport structure pour relecture rapide (terminal ou PR) :
metriques par projet, vitesse, cout, output, signaux.
"""

from __future__ import annotations

from datetime import datetime

from ..core.intervals import union_active_sec_from_events
from ..core.pricing import is_hourly_estimated


def _fmt_hours(sec: int | None) -> str:
    if not sec:
        return "0h"
    hours = sec / 3600
    if hours < 1:
        return f"{int(sec/60)}min"
    return f"{hours:.1f}h"


def _fmt_int(n: int | None) -> str:
    return f"{n or 0:,}".replace(",", " ")


def generate_report(storage, cutoff: datetime, since_label: str = "") -> str:
    """Genere un rapport Markdown complet."""
    lines: list[str] = []

    # Header
    today = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"# Ship1000x — rapport {since_label or 'complet'}")
    lines.append("")
    lines.append(f"*Genere le {today} · Fenetre : depuis {cutoff.date()}*")
    lines.append("")

    # Totaux globaux (sans active_sec SQL : il serait sommé naïvement cross-sources,
    # ce qui double-compte les overlaps Claude Code + Codex.app simultanes).
    totals = storage.query(
        """
        SELECT
            COUNT(DISTINCT id) AS events,
            SUM(wall_clock_sec) AS wall_clock_sec,
            SUM(token_input + token_output) AS tokens,
            SUM(cost_estimated) AS cost,
            COUNT(DISTINCT source) AS sources,
            COUNT(DISTINCT project_id) AS projects
        FROM events
        WHERE started_at >= ?
        """,
        (cutoff.isoformat(),),
    )
    t = totals[0] if totals else None

    # Temps actif humain via UNION d'intervalles cross-sources : si Claude Code
    # CLI et Codex.app macOS tournent en parallele, les minutes humaines ne
    # sont comptees qu'une fois. Cf core/intervals.py.
    interval_rows = storage.query(
        """
        SELECT started_at, duration_sec
        FROM events
        WHERE started_at >= ?
          AND duration_sec > 0
          AND source != 'git'
        """,
        (cutoff.isoformat(),),
    )
    active_sec = union_active_sec_from_events(interval_rows)

    if not t or active_sec == 0:
        lines.append("> Aucune donnee sur cette fenetre.")
        return "\n".join(lines)

    # Multiplicateur IA = temps agents cumule / temps actif humain.
    # Chaque heure humaine orchestre ~N heures d'execution agent en parallele.
    # Plus robuste que le "% pilotage" qui fluctue fortement avec le cap.
    wall_clock_sec = t["wall_clock_sec"] or 0
    ai_multiplier = wall_clock_sec / active_sec if active_sec > 0 else 0

    # Split cout mesure (tokens reels Anthropic/OpenAI) vs estime horaire
    # (Codex.app / Codex Desktop / Cursor — apps fermees).
    cost_split = storage.query(
        """
        SELECT source, SUM(cost_estimated) AS cost
        FROM events
        WHERE started_at >= ?
        GROUP BY source
        """,
        (cutoff.isoformat(),),
    )
    cost_measured = sum(
        (r["cost"] or 0) for r in cost_split if not is_hourly_estimated(r["source"])
    )
    cost_hourly = sum(
        (r["cost"] or 0) for r in cost_split if is_hourly_estimated(r["source"])
    )

    lines.append("## Vue d'ensemble")
    lines.append("")
    lines.append(f"- **Temps actif humain** : {_fmt_hours(active_sec)}")
    lines.append(f"- **Temps agents cumule** : {_fmt_hours(wall_clock_sec)} "
                 f"(wall-clock first event -> last event, sommes par source)")
    if ai_multiplier > 0:
        lines.append(f"- **Multiplicateur IA** : **x{ai_multiplier:.1f}** "
                     f"(chaque heure humaine orchestre ~{ai_multiplier:.1f}h d'execution agent)")
    lines.append(f"- **Events** : {_fmt_int(t['events'])}")
    lines.append(f"- **Tokens IA** : {_fmt_int(t['tokens'])}")
    total_cost = t['cost'] or 0
    if cost_hourly > 0 and cost_measured > 0:
        lines.append(
            f"- **Cout estime** : ${total_cost:.2f} "
            f"(${cost_measured:.2f} mesure tokens reels · "
            f"${cost_hourly:.2f} estime horaire apps fermees)"
        )
    else:
        lines.append(f"- **Cout estime** : ${total_cost:.2f}")
    lines.append(f"- **Sources** : {t['sources']}")
    lines.append(f"- **Projets** : {t['projects']}")
    lines.append("")

    # Par projet
    lines.append("## Repartition par projet")
    lines.append("")
    lines.append("| Projet | Temps actif | Events | Tokens | Cout $ |")
    lines.append("|---|---:|---:|---:|---:|")
    project_rows = storage.query(
        """
        SELECT
            COALESCE(project_id, 'unclassified') AS project,
            SUM(duration_sec) AS active_sec,
            COUNT(*) AS events,
            SUM(token_input + token_output) AS tokens,
            SUM(cost_estimated) AS cost
        FROM events
        WHERE started_at >= ?
        GROUP BY project
        ORDER BY active_sec DESC
        """,
        (cutoff.isoformat(),),
    )
    for r in project_rows:
        lines.append(
            f"| {r['project']} | {_fmt_hours(r['active_sec'])} | "
            f"{_fmt_int(r['events'])} | {_fmt_int(r['tokens'])} | "
            f"{r['cost'] or 0:.2f} |"
        )
    lines.append("")

    # Par source
    lines.append("## Repartition par source")
    lines.append("")
    lines.append("| Source | Sessions | Temps actif | Tokens |")
    lines.append("|---|---:|---:|---:|")
    source_rows = storage.query(
        """
        SELECT source,
            COUNT(*) AS n,
            SUM(duration_sec) AS active_sec,
            SUM(token_input + token_output) AS tokens
        FROM events
        WHERE started_at >= ?
        GROUP BY source
        ORDER BY active_sec DESC
        """,
        (cutoff.isoformat(),),
    )
    for r in source_rows:
        lines.append(
            f"| {r['source']} | {_fmt_int(r['n'])} | "
            f"{_fmt_hours(r['active_sec'])} | {_fmt_int(r['tokens'])} |"
        )
    lines.append("")

    # Git production
    git_rows = storage.query(
        """
        SELECT
            COALESCE(project_id, 'unclassified') AS project,
            COUNT(*) AS commits,
            SUM(CAST(json_extract(raw_meta, '$.lines_added') AS INT)) AS added,
            SUM(CAST(json_extract(raw_meta, '$.lines_deleted') AS INT)) AS deleted
        FROM events
        WHERE source = 'git' AND started_at >= ?
        GROUP BY project
        ORDER BY commits DESC
        """,
        (cutoff.isoformat(),),
    )
    if git_rows:
        lines.append("## Production git (commits + lignes)")
        lines.append("")
        lines.append("| Projet | Commits | Lignes + | Lignes - |")
        lines.append("|---|---:|---:|---:|")
        for r in git_rows:
            lines.append(
                f"| {r['project']} | {_fmt_int(r['commits'])} | "
                f"+{_fmt_int(r['added'])} | -{_fmt_int(r['deleted'])} |"
            )
        lines.append("")

    # Jours les plus intenses
    lines.append("## Top 10 journees les plus intenses")
    lines.append("")
    lines.append("| Jour | Temps actif | Events | Projet dominant |")
    lines.append("|---|---:|---:|---|")
    day_rows = storage.query(
        """
        SELECT
            DATE(started_at) AS day,
            SUM(duration_sec) AS active_sec,
            COUNT(*) AS events
        FROM events
        WHERE started_at >= ?
        GROUP BY day
        ORDER BY active_sec DESC
        LIMIT 10
        """,
        (cutoff.isoformat(),),
    )
    for r in day_rows:
        # Projet dominant du jour
        top = storage.query(
            """
            SELECT COALESCE(project_id, 'unclassified') AS project, SUM(duration_sec) AS s
            FROM events
            WHERE DATE(started_at) = ?
            GROUP BY project
            ORDER BY s DESC LIMIT 1
            """,
            (r["day"],),
        )
        top_project = top[0]["project"] if top else "?"
        lines.append(
            f"| {r['day']} | {_fmt_hours(r['active_sec'])} | "
            f"{_fmt_int(r['events'])} | {top_project} |"
        )
    lines.append("")

    # Insights (efficience + multiplicateur + signaux)
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from ship1000x.insights.engine import Window, compute_overview
        from ship1000x.insights.multiplier import compute_multiplier
        from ship1000x.insights.signals import compute_all_signals

        _since = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=_tz.utc)
        _until = _dt.now(_tz.utc)
        window = Window(since=_since, until=_until)

        overview = compute_overview(storage, window)
        mult = compute_multiplier(storage, window)
        signals_list = compute_all_signals(storage, window)

        r = overview["ratios"]

        def _fmt(n, decimals=1):
            if n is None:
                return "—"
            if isinstance(n, (int, float)):
                if abs(n) >= 1000:
                    return f"{n:,.0f}".replace(",", " ")
                if isinstance(n, float):
                    return f"{n:.{decimals}f}"
                return str(n)
            return str(n)

        lines.append("## Efficience IA-native")
        lines.append("")
        lines.append("| Metrique | Valeur | Unite |")
        lines.append("|---|---:|---|")
        lines.append(f"| Output | {_fmt(r['lines_per_hour'])} | lignes / h active |")
        lines.append(f"| Prompts humain | {_fmt(r['typed_per_hour'])} | typed / h |")
        lines.append(f"| Tokens brasses | {_fmt(r['tokens_per_hour'])} | tokens / h |")
        lines.append(f"| Commits | {_fmt(r['commits_per_hour'], 2)} | commits / h |")
        lines.append(f"| Amplification | {_fmt(r['lines_per_typed'])} | lignes / prompt typed |")
        lines.append(f"| Agent mode | {_fmt(r['tool_per_typed'])} | outils / prompt typed |")
        lines.append("")

        out = mult["output"]
        v = mult["value"]
        c = mult["cost"]
        lines.append("## Multiplicateur IA-native")
        lines.append("")
        lines.append(f"- **Facteur de production** : x{out['factor_vs_senior_low']} → x{out['factor_vs_senior_high']} "
                     f"(mid x{out['factor_vs_senior_mid']}) vs {out['benchmark_senior_low']}-{out['benchmark_senior_high']} lignes/h sans IA")
        lines.append(f"- **Temps actif** : {v['active_hours']}h = {v['days_equivalent']} jours-equivalents")
        tjm_eur = v['tjm_equivalent_eur']
        lines.append(f"- **Valeur TJM** : {tjm_eur:,.0f} EUR (TJM {mult['inputs']['tjm_eur_per_day']} EUR/j, {mult['inputs']['workday_hours']}h/j)".replace(",", " "))
        if v["value_time_ratio"]:
            lines.append(f"- **Valeur produit benchmark** : {v['value_produit_eur']:,.0f} EUR (agence Tier-1)".replace(",", " "))
            lines.append(f"- **Ratio valeur/temps** : **x{v['value_time_ratio']}**")
        lines.append(f"- **Cout IA** : ${c['total_usd']} total · ${c['per_commit_usd'] or 0:.2f}/commit · ${c['per_line_net_usd'] or 0:.4f}/ligne nette")
        lines.append("")

        if signals_list:
            lines.append("## Signaux")
            lines.append("")
            for s in signals_list:
                level_tag = {"critical": "[CRIT]", "warning": "[WARN]", "info": "[INFO]"}.get(s["level"], "")
                lines.append(f"- {level_tag} **{s['category']} / {s['type']}** (confidence={s['confidence']}) : {s['description']}")
            lines.append("")
    except Exception as _e:
        lines.append(f"<!-- Insights non calculables : {_e} -->")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append(
        "*Methodologie : temps actif = intervalles entre events USER < 5 min. "
        "Sessions cap 12h (anti-aberration). Tokens/cout = agregation sessions Claude Code. "
        "Privacy-first : aucun contenu (prompts, fichiers, diffs) n'est stocke.*"
    )

    return "\n".join(lines)
