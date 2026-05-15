"""Flask app factory for the Ship1000x web dashboard.

Exposes :
- Pages : / (overview), /projects (cross-tab matrix)
- API JSON : /api/highlights, /api/trend, /api/projects, /api/trust

Security :
- Localhost-only binding (refuse 0.0.0.0)
- No external CDN credentials, no auth needed (local user)
- All queries read-only on the SQLite DB
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template, request


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
                "AVG(threshold_used_sec) AS thr "
                "FROM daily_unified WHERE date >= date('now', ? || ' days')",
                (f"-{days}",),
            ).fetchone()
            cost = conn.execute(
                "SELECT SUM(cost_estimated) AS c FROM events "
                "WHERE date(started_at) >= date('now', ? || ' days')",
                (f"-{days}",),
            ).fetchone()["c"] or 0
            cost_factual = conn.execute(
                """SELECT SUM(cost_estimated) AS c FROM events
                   WHERE date(started_at) >= date('now', ? || ' days')
                     AND source IN ('claude_code', 'anthropic_usage', 'openai_usage', 'openclaw', 'web_exports')""",
                (f"-{days}",),
            ).fetchone()["c"] or 0
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
            # Wall_brut capped 5x duration_sec per source
            rows_per_source = conn.execute(
                """SELECT source, SUM(duration_sec) AS dur, SUM(wall_clock_sec) AS wall
                   FROM events WHERE date(started_at) >= date('now', ? || ' days') AND source != 'git'
                   GROUP BY source""",
                (f"-{days}",),
            ).fetchall()

        active_h = (unif["u"] or 0) / 3600
        wall_h = (unif["w"] or 0) / 3600
        threshold_min = (unif["thr"] or 0) / 60

        wall_brut_capped = 0
        for r in rows_per_source:
            d = r["dur"] or 0
            w = r["wall"] or 0
            wall_brut_capped += min(w, d * 5) if d > 0 else 0

        leverage = (wall_brut_capped / 3600 / active_h) if active_h else 0
        presence = (wall_h / active_h) if active_h else 0
        parallelism = (leverage / presence) if presence else 0
        days_equivalent = active_h / 8
        cost_per_line = (cost_factual / lines_real) if lines_real else 0
        real_pct = (lines_real / lines_raw * 100) if lines_raw else 0
        cost_factual_pct = (cost_factual / cost * 100) if cost else 0

        # Trust Score
        from ship1000x.insights.trust_score import compute_global_score
        trust = compute_global_score(storage, window_days=days, user_email=user_email)

        return jsonify({
            "window_days": days,
            "leverage": round(leverage, 2),
            "parallelism": round(parallelism, 2),
            "days_equivalent": round(days_equivalent, 1),
            "active_hours": round(active_h, 1),
            "wall_hours": round(wall_h, 1),
            "lines_real": lines_real,
            "lines_raw": lines_raw,
            "real_pct": round(real_pct, 1),
            "cost_total": round(cost, 2),
            "cost_factual": round(cost_factual, 2),
            "cost_factual_pct": round(cost_factual_pct, 1),
            "cost_per_line": round(cost_per_line, 4),
            "trust_score": trust["score"],
            "trust_base": trust.get("base", trust["score"]),
            "trust_bonus": trust.get("bonus", 0),
            "trust_label": trust["label"],
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
                          wall_clock_sec
                   FROM daily_unified
                   WHERE date >= date('now', ? || ' days')
                   ORDER BY date""",
                (f"-{days}",),
            ).fetchall()
        return jsonify([
            {
                "date": r["date"],
                "active_hours": round((r["active_sec"] or 0) / 3600, 2),
                "wall_hours": round((r["wall_clock_sec"] or 0) / 3600, 2),
            }
            for r in rows
        ])

    @app.route("/api/projects")
    def api_projects():
        days = int(request.args.get("days", 30))
        with storage.conn() as conn:
            rows = conn.execute(
                """SELECT
                       COALESCE(project_id, 'unclassified') AS project,
                       source,
                       SUM(duration_sec) AS sec,
                       SUM(cost_estimated) AS cost,
                       COUNT(*) AS events
                   FROM events
                   WHERE date(started_at) >= date('now', ? || ' days')
                   GROUP BY project, source
                   ORDER BY project, source""",
                (f"-{days}",),
            ).fetchall()

        # Aggregate by project
        by_project: dict[str, dict] = {}
        for r in rows:
            pid = r["project"]
            if pid not in by_project:
                by_project[pid] = {
                    "project_id": pid,
                    "total_sec": 0,
                    "total_cost": 0.0,
                    "sources_ia": 0,
                    "commits": 0,
                    "sources_breakdown": {},
                }
            p = by_project[pid]
            sec = r["sec"] or 0
            n = r["events"] or 0
            cost = r["cost"] or 0
            p["total_sec"] += sec
            p["total_cost"] += cost
            p["sources_breakdown"][r["source"]] = {"sec": sec, "events": n}
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
            "global": global_score,
            "per_source": [
                {
                    "source": src,
                    "score": info["score"],
                    "event_count": info["event_count"],
                    "label": get_score_label(info["score"])[0],
                }
                for src, info in sorted(per_source.items(), key=lambda x: -x[1]["score"])
            ],
        })

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
