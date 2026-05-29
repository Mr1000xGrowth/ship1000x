"""Flask app factory for the Ship1000x web dashboard.

Exposes :
- Pages : / (overview), /projects (cross-tab matrix)
- API JSON : /api/highlights, /api/trend, /api/projects, /api/trust,
  /api/source-quality

Security :
- Localhost-only binding (refuse 0.0.0.0)
- No external CDN credentials, no auth needed (local user)
- All queries read-only on the SQLite DB
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from ship1000x.core.cost_truth import (
    api_equivalent_cost_from_row,
    event_cost_truth,
    safe_raw_meta,
)
from ship1000x.core.usage import format_token_count as _fmt_tok


def _cost_presentation(
    *,
    api_equivalent: float,
    billed_estimated: float,
    events_with_unknown_billing_basis: int,
) -> tuple[str, str]:
    if events_with_unknown_billing_basis:
        return (
            "unknown_billing_basis",
            "API-equivalent includes unknown-basis rows; not invoice truth.",
        )
    if api_equivalent and billed_estimated < api_equivalent:
        return (
            "api_equivalent_with_subscription_absorption",
            "API-equivalent includes subscription-absorbed usage; not invoice truth.",
        )
    if api_equivalent and billed_estimated == api_equivalent:
        return (
            "api_equivalent_matches_billed_estimate",
            "API-equivalent matches local billed estimate; still not provider invoice truth.",
        )
    if api_equivalent:
        return "api_equivalent_only", "API-equivalent only; not invoice truth."
    return "no_cost_observed", "No cost observed in this window."


def _compute_cost_truth(conn, days: int) -> dict[str, Any]:
    """Split dashboard cost into API-equivalent, billed-estimated, and unknown.

    The dashboard must not imply that every stored token estimate is a real
    bill. We only inspect normalized usage metadata and never expose raw_meta.
    """
    rows = conn.execute(
        """SELECT source, cost_estimated, raw_meta
           FROM events
           WHERE date(started_at) >= date('now', ? || ' days')""",
        (f"-{days}",),
    ).fetchall()

    api_equivalent = 0.0
    billed_estimated = 0.0
    subscription_absorbed = 0.0
    unknown_billing_basis = 0.0
    native_token_pricing = 0.0
    events_with_cost_truth = 0
    events_with_unknown_billing_basis = 0

    for row in rows:
        meta = safe_raw_meta(row["raw_meta"])

        truth = event_cost_truth(
            stored_cost=row["cost_estimated"] or 0.0,
            meta=meta,
            unknown_strategy="include_in_api_equivalent",
        )
        api_equivalent += truth.api_equivalent_usd
        billed_estimated += truth.billed_estimated_usd
        subscription_absorbed += truth.subscription_absorbed_usd
        unknown_billing_basis += truth.unknown_billing_basis_usd
        events_with_cost_truth += truth.events_with_cost_truth
        events_with_unknown_billing_basis += truth.events_with_unknown_billing_basis
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        quality = usage.get("quality") if isinstance(usage.get("quality"), dict) else {}
        cost_block = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        cost_quality = quality.get("cost") or cost_block.get("quality")
        if cost_quality == "factual" or (
            cost_quality is None
            and (row["cost_estimated"] or 0.0) > 0
            and row["source"] in {"claude_code", "anthropic_usage", "openai_usage", "openclaw", "web_exports"}
        ):
            native_token_pricing += truth.api_equivalent_usd

    presentation_status, presentation_label = _cost_presentation(
        api_equivalent=api_equivalent,
        billed_estimated=billed_estimated,
        events_with_unknown_billing_basis=events_with_unknown_billing_basis,
    )

    return {
        "api_equivalent_usd": round(api_equivalent, 2),
        "billed_estimated_usd": round(billed_estimated, 2),
        "subscription_absorbed_usd": round(subscription_absorbed, 2),
        "unknown_billing_basis_usd": round(unknown_billing_basis, 2),
        "native_token_pricing_usd": round(native_token_pricing, 2),
        "events_with_cost_truth": events_with_cost_truth,
        "events_with_unknown_billing_basis": events_with_unknown_billing_basis,
        "presentation_status": presentation_status,
        "presentation_label": presentation_label,
    }


def create_app(db_path: Path, config_dir: Path) -> Flask:
    """Build a Flask app bound to a specific Ship1000x DB + config.

    Args:
        db_path: path to tracker.sqlite
        config_dir: path to ~/.config/ship1000x/ (for privacy.yaml)
    """
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["DB_PATH"] = db_path
    app.config["CONFIG_DIR"] = config_dir

    from ship1000x.core.storage import Storage
    storage = Storage(db_path)

    def _get_user_email() -> str | None:
        """Read user_email from privacy.yaml (best-effort)."""
        try:
            import yaml
            cfg_path = config_dir / "privacy.yaml"
            if not cfg_path.exists():
                return None
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            return (cfg.get("consent") or {}).get("user_email")
        except Exception:
            return None

    # ─── Pages ─────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("overview.html")

    @app.route("/projects")
    def projects_page():
        return render_template("projects.html")

    # ─── API endpoints ─────────────────────────────────────────────────

    @app.route("/api/highlights")
    def api_highlights():
        days = int(request.args.get("days", 30))
        user_email = _get_user_email()

        with storage.conn() as conn:
            unif = conn.execute(
                "SELECT SUM(active_sec_unified) AS u, SUM(wall_clock_sec) AS w, "
                "SUM(agent_sec_additive) AS aa, AVG(threshold_used_sec) AS thr "
                "FROM daily_unified WHERE date >= date('now', ? || ' days')",
                (f"-{days}",),
            ).fetchone()
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
            sources_count = conn.execute(
                "SELECT COUNT(DISTINCT source) AS n FROM events "
                "WHERE date(started_at) >= date('now', ? || ' days')",
                (f"-{days}",),
            ).fetchone()["n"] or 0
            cost_truth = _compute_cost_truth(conn, days)
            # Tokens FACTUELS — somme du superset usage_breakdown (capture
            # exhaustive). Compteurs fournisseur, decomposes (jamais un total nu).
            def _sum_ub(field: str) -> int:
                return conn.execute(
                    "SELECT SUM(CAST(COALESCE("
                    f"json_extract(raw_meta, '$.usage_breakdown.{field}'), 0) AS INTEGER)) AS s "
                    "FROM events WHERE source != 'git' "
                    "AND date(started_at) >= date('now', ? || ' days')",
                    (f"-{days}",),
                ).fetchone()["s"] or 0
            tok = {f: _sum_ub(f) for f in (
                "fresh_input", "cache_read", "cache_write_5m", "cache_write_1h",
                "output_tokens", "thinking", "reasoning",
                "web_search_requests", "web_fetch_requests",
            )}

        active_h = (unif["u"] or 0) / 3600
        wall_h = (unif["w"] or 0) / 3600
        agent_additive_h = (unif["aa"] or 0) / 3600
        threshold_min = (unif["thr"] or 0) / 60
        # Levier unique = heures de travail cumulees (sessions //, humain + agents)
        # par heure de presence humaine reelle dedupliquee. > 1 = pilotage en
        # parallele. Base sur le temps reellement engage (pas le temps "app
        # ouverte"), donc auditable et sans plafond heuristique.
        orchestration_factor = (agent_additive_h / active_h) if active_h else 0
        days_equivalent = active_h / 8
        api_equivalent_cost = float(cost_truth.get("api_equivalent_usd") or 0.0)
        cost_factual = float(cost_truth.get("native_token_pricing_usd") or 0.0)
        cost_per_line = (cost_factual / lines_real) if lines_real else 0
        real_pct = (lines_real / lines_raw * 100) if lines_raw else 0
        cost_factual_pct = (cost_factual / api_equivalent_cost * 100) if api_equivalent_cost else 0

        # Trust Score
        from ship1000x.insights.trust_score import compute_global_score
        trust = compute_global_score(storage, window_days=days, user_email=user_email)

        return jsonify({
            "schema_version": "ship1000x.dashboard.highlights.v1",
            "window_days": days,
            "days_equivalent": round(days_equivalent, 1),
            "active_hours": round(active_h, 1),
            "wall_hours": round(wall_h, 1),
            "agent_hours_additive": round(agent_additive_h, 1),
            "orchestration_factor": round(orchestration_factor, 2),
            "tokens": {
                "raw": tok,
                "fmt": {k: _fmt_tok(v) for k, v in tok.items()},
                "input_total": tok["fresh_input"] + tok["cache_read"]
                + tok["cache_write_5m"] + tok["cache_write_1h"],
            },
            "lines_real": lines_real,
            "lines_raw": lines_raw,
            "real_pct": round(real_pct, 1),
            "cost_api_equivalent": round(api_equivalent_cost, 2),
            "cost_total": round(api_equivalent_cost, 2),
            "cost_total_basis": "api_equivalent_legacy_alias",
            "cost_factual": round(cost_factual, 2),
            "cost_factual_pct": round(cost_factual_pct, 1),
            "cost_truth": cost_truth,
            "cost_per_line": round(cost_per_line, 4),
            "trust_score": trust["score"],
            "trust_label": trust["label"],
            "trust_robustness": trust.get("robustness_checks", []),
            "sources_count": sources_count,
            "threshold_min": round(threshold_min, 1),
        })

    @app.route("/api/trend")
    def api_trend():
        days = int(request.args.get("days", 30))
        with storage.conn() as conn:
            rows = conn.execute(
                """SELECT date,
                          active_sec_unified AS active_sec,
                          agent_sec_additive,
                          wall_clock_sec
                   FROM daily_unified
                   WHERE date >= date('now', ? || ' days')
                   ORDER BY date""",
                (f"-{days}",),
            ).fetchall()
        return jsonify([
            {
                "schema_version": "ship1000x.dashboard.trend_point.v1",
                "date": r["date"],
                "active_hours": round((r["active_sec"] or 0) / 3600, 2),
                "agent_hours_additive": round((r["agent_sec_additive"] or 0) / 3600, 2),
                "wall_hours": round((r["wall_clock_sec"] or 0) / 3600, 2),
            }
            for r in rows
        ])

    @app.route("/api/cost-models")
    def api_cost_models():
        """Coût + tokens par modèle (table daily_model_usage).

        api_equivalent = ce que ça coûterait au tarif API (et-si).
        billed = estimé facturé (0 sous abonnement). subscription_absorbed =
        api_equivalent - billed = valeur absorbée par les abonnements.
        """
        days = int(request.args.get("days", 30))
        tok_fields = (
            "fresh_input", "cache_read", "cache_write_5m", "cache_write_1h",
            "output_tokens", "thinking", "reasoning", "web_search_requests",
        )
        with storage.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_model_usage "
                "WHERE date >= date('now', ? || ' days') ORDER BY date",
                (f"-{days}",),
            ).fetchall()
            pricing_version = conn.execute(
                "SELECT MAX(pricing_version) v FROM daily_model_usage"
            ).fetchone()["v"]

        by_model: dict[tuple, dict] = {}
        total_api = total_billed = 0.0
        for r in rows:
            key = (r["provider"], r["model"])
            m = by_model.get(key)
            if m is None:
                m = {
                    "provider": r["provider"], "model": r["model"],
                    "tokens": {f: 0 for f in tok_fields},
                    "cost_api_equivalent": 0.0, "cost_billed": 0.0,
                    "pricing_quality": r["pricing_quality"],
                    "auth_mode": r["auth_mode"], "daily": [],
                }
                by_model[key] = m
            for f in tok_fields:
                m["tokens"][f] += r[f] or 0
            m["cost_api_equivalent"] += r["cost_api_equivalent"] or 0.0
            m["cost_billed"] += r["cost_billed"] or 0.0
            m["daily"].append({
                "date": r["date"],
                "cost": round(r["cost_api_equivalent"] or 0.0, 4),
            })
            total_api += r["cost_api_equivalent"] or 0.0
            total_billed += r["cost_billed"] or 0.0

        models = sorted(by_model.values(), key=lambda x: -x["cost_api_equivalent"])
        for m in models:
            m["cost_api_equivalent"] = round(m["cost_api_equivalent"], 2)
            m["cost_billed"] = round(m["cost_billed"], 2)
        fallback_models = [
            f"{m['provider']}/{m['model']}" for m in models
            if m["pricing_quality"] == "fallback"
        ]
        return jsonify({
            "schema_version": "ship1000x.dashboard.cost_models.v1",
            "window_days": days,
            "totals": {
                "api_equivalent": round(total_api, 2),
                "billed": round(total_billed, 2),
                "subscription_absorbed": round(total_api - total_billed, 2),
            },
            "by_model": models,
            "pricing": {
                "version": pricing_version,
                "fallback_models": fallback_models,
                "note": "API-equivalent = what-if at API rates, not an invoice.",
            },
        })

    @app.route("/api/projects")
    def api_projects():
        days = int(request.args.get("days", 30))
        with storage.conn() as conn:
            rows = conn.execute(
                """SELECT
                       COALESCE(project_id, 'unclassified') AS project,
                       source,
                       duration_sec AS sec,
                       cost_estimated,
                       raw_meta
                   FROM events
                   WHERE date(started_at) >= date('now', ? || ' days')
                   ORDER BY project, source""",
                (f"-{days}",),
            ).fetchall()

        # Aggregate by project
        by_project: dict[str, dict] = {}
        for r in rows:
            pid = r["project"]
            if pid not in by_project:
                by_project[pid] = {
                    "schema_version": "ship1000x.dashboard.project.v1",
                    "project_id": pid,
                    "total_sec": 0,
                    "total_cost": 0.0,
                    "cost_truth": {
                        "api_equivalent_usd": 0.0,
                        "billed_estimated_usd": 0.0,
                        "subscription_absorbed_usd": 0.0,
                        "unknown_billing_basis_usd": 0.0,
                        "events_with_cost_truth": 0,
                        "events_with_unknown_billing_basis": 0,
                    },
                    "sources_ia": 0,
                    "commits": 0,
                    "sources_breakdown": {},
                }
            p = by_project[pid]
            sec = r["sec"] or 0
            n = 1
            cost = api_equivalent_cost_from_row(r)
            truth = event_cost_truth(
                stored_cost=float(r["cost_estimated"] or 0.0),
                meta=safe_raw_meta(r["raw_meta"]),
                unknown_strategy="include_in_api_equivalent",
            )
            p["total_sec"] += sec
            p["total_cost"] += cost
            p["cost_truth"]["api_equivalent_usd"] += truth.api_equivalent_usd
            p["cost_truth"]["billed_estimated_usd"] += truth.billed_estimated_usd
            p["cost_truth"]["subscription_absorbed_usd"] += truth.subscription_absorbed_usd
            p["cost_truth"]["unknown_billing_basis_usd"] += truth.unknown_billing_basis_usd
            p["cost_truth"]["events_with_cost_truth"] += truth.events_with_cost_truth
            p["cost_truth"]["events_with_unknown_billing_basis"] += (
                truth.events_with_unknown_billing_basis
            )
            source_breakdown = p["sources_breakdown"].setdefault(
                r["source"],
                {"sec": 0, "events": 0},
            )
            source_breakdown["sec"] += sec
            source_breakdown["events"] += n
            if r["source"] == "git":
                p["commits"] += n
            else:
                p["sources_ia"] += n

        # Compute dominant tool per project (by active sec, excluding git)
        out = []
        for pid, p in by_project.items():
            ia_only = {s: v for s, v in p["sources_breakdown"].items() if s != "git"}
            if ia_only:
                dom = max(ia_only.items(), key=lambda x: x[1]["sec"])
                p["dominant_tool"] = dom[0]
                p["dominant_pct"] = round((dom[1]["sec"] / p["total_sec"] * 100), 0) if p["total_sec"] else 0
            else:
                p["dominant_tool"] = "git only"
                p["dominant_pct"] = 0
            p["total_hours"] = round(p["total_sec"] / 3600, 2)
            p["total_cost"] = round(p["total_cost"], 2)
            p["total_api_equivalent_cost"] = p["total_cost"]
            for key, value in list(p["cost_truth"].items()):
                if key.endswith("_usd"):
                    p["cost_truth"][key] = round(value, 2)
            status, label = _cost_presentation(
                api_equivalent=p["cost_truth"]["api_equivalent_usd"],
                billed_estimated=p["cost_truth"]["billed_estimated_usd"],
                events_with_unknown_billing_basis=p["cost_truth"][
                    "events_with_unknown_billing_basis"
                ],
            )
            p["cost_truth"]["presentation_status"] = status
            p["cost_truth"]["presentation_label"] = label
            p["total_billed_estimated_cost"] = p["cost_truth"]["billed_estimated_usd"]
            p["total_subscription_absorbed_cost"] = p["cost_truth"][
                "subscription_absorbed_usd"
            ]
            p["unknown_billing_basis_cost"] = p["cost_truth"][
                "unknown_billing_basis_usd"
            ]
            p["events_with_unknown_billing_basis"] = p["cost_truth"][
                "events_with_unknown_billing_basis"
            ]
            p["total_cost_basis"] = "api_equivalent_legacy_alias"
            del p["total_sec"]
            del p["sources_breakdown"]  # keep response payload small
            out.append(p)
        out.sort(key=lambda x: -x["total_hours"])
        return jsonify(out)

    @app.route("/api/trust")
    def api_trust():
        days = int(request.args.get("days", 30))
        user_email = _get_user_email()
        from ship1000x.insights.trust_score import (
            compute_global_score,
            get_all_source_scores,
            get_score_label,
        )
        per_source = get_all_source_scores(storage, window_days=days)
        global_score = compute_global_score(storage, window_days=days, user_email=user_email)
        return jsonify({
            "schema_version": "ship1000x.dashboard.trust.v1",
            "global": global_score,
            "per_source": [
                {
                    "schema_version": "ship1000x.dashboard.trust_source.v1",
                    "source": src,
                    "score": info["score"],
                    "event_count": info["event_count"],
                    "label": get_score_label(info["score"])[0],
                }
                for src, info in sorted(per_source.items(), key=lambda x: -x[1]["score"])
            ],
        })

    @app.route("/api/source-quality")
    def api_source_quality():
        days = int(request.args.get("days", 30))
        from ship1000x.core.source_quality import build_source_quality_report

        return jsonify(build_source_quality_report(storage, window_days=days))

    return app


def run_server(db_path: Path, config_dir: Path, port: int = 10000, open_browser: bool = True) -> None:
    """Launch Flask dev server on localhost only.

    Blocks until Ctrl+C. Refuses to bind 0.0.0.0 (security).
    """
    app = create_app(db_path, config_dir)

    if open_browser:
        import threading
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    # Force localhost-only — never expose externally
    host = "127.0.0.1"
    print(f"\n  🚀 Ship1000x dashboard → http://localhost:{port}")
    print(f"     DB     : {db_path}")
    print(f"     Config : {config_dir}")
    print("     (Ctrl+C to stop)\n")

    # Disable Flask reloader (can spawn duplicate processes in dev)
    app.run(host=host, port=port, debug=False, use_reloader=False)
