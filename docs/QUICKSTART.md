# Quickstart — install and run Ship1000x in 3 commands

Local-first AI dev productivity tracker. Zero data leaves your machine by default.

---

## Prerequisites

- macOS (primary target — Linux works for most collectors, no Windows native support yet)
- Python 3.10+ with `venv`
- Git
- A few of these tools installed (otherwise nothing to track): Claude Code, Codex CLI, Cursor, Cline, OpenClaw, etc.

---

## Install

```bash
git clone https://github.com/Mr1000xGrowth/ship1000x
cd ship1000x
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tracker.py init
```

`tracker init` walks you through 5 short questions:

1. **Identity** — your local display name + email (stays local, used for cadence calibration only)
2. **Privacy consent** — confirm you understand what is and isn't tracked
3. **Project detection** — Ship1000x scans your `~/work` (or wherever you code) and lists detected git repos for you to confirm/edit
4. **Schedule** — what time the daily ingestion cron should run (default: 03:00)
5. **Optional cloud sync** — OFF by default. Only enable if you have a Garage S3 bucket you control

---

## Daily usage

```bash
# Snapshot of today's activity per project
tracker today

# Compare 5 active-time modes side by side (strict / auto P95 / loose / agent / wall-clock)
tracker today --compare-modes

# Calibrate your personal cadence profile (recommended after a few days of data)
tracker calibrate

# Last 7 days summary
tracker week

# Drill down on a specific project
tracker project myproject --since 30d

# Productivity insights with confidence levels
tracker insights --since 30d
```

The cron runs `tracker daily` automatically each night (ingest + rollup + optional push).

---

## Multi-Mac (advanced)

If you have multiple Macs, install Ship1000x on each (same email in `tracker init`). They will share the cadence profile and you'll see machine-attribution in the dashboard if you opted into cloud sync.

---

## Custom projects

After install, edit `config/projects.yaml` to add/remove projects, paths, and git remotes. The classifier reloads on each `tracker ingest`.

For the full project config syntax, see comments in `config/projects.yaml` itself.

---

## Privacy quick-check

```bash
sqlite3 ~/ship1000x/db/tracker.sqlite

# See the schema (no content fields)
.schema events

# Sample 5 events
SELECT * FROM events ORDER BY started_at DESC LIMIT 5;
```

You will see only counts, IDs, timestamps, anonymized paths. No prompts, no code, no responses.

For the full privacy threat model: [`PRIVACY.md`](PRIVACY.md).

---

## Troubleshooting

**No data after install**: confirm at least one supported tool actually wrote local traces. For Claude Code: `ls ~/.claude/projects/`. For Codex CLI: `ls ~/.codex/`. Then run `tracker ingest --source all`.

**`tracker today` shows 0 hours**: rollups not yet computed. Run `tracker rollup --since 7d`.

**Cross-source overcount looks wrong**: run `tracker today --compare-modes` and check the unified value. The pre-V1 rollups had a multi-agent overcount bug (×2.85). After upgrading, run `tracker rollup --since 365d` once to rebuild.

**Cadence profile shows fallback (5min)**: you have < 100 captured intervals. Use Ship1000x for a few days, then run `tracker calibrate` again.

For deeper debugging, see [`docs/internal/ARCHITECTURE_V2.md`](internal/ARCHITECTURE_V2.md) (advanced internals, currently French only — translation V1.1).

---

## Next steps

- Read [`COVERAGE.md`](COVERAGE.md) — exactly what is captured per source
- Read [`METHODOLOGY.md`](METHODOLOGY.md) — how each metric is computed
- Read [`PRIVACY.md`](PRIVACY.md) — what stays local vs what can be pushed
- Read [`TRUST_SCORE.md`](TRUST_SCORE.md) — how confidence is computed and displayed
- Run cross-validation: `python ship1000x-reconciliation/reconcile.py --days 30` (requires Anthropic Admin API key)

---

## Author

Charles GAUTIER — Mr1000xGrowth — Founder, LeadsFlowAI

charles@leadsflowai.com
