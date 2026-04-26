# Ship1000x

> Track what you actually ship with AI.

Local-first tracker for AI-assisted development. Measures **focus time**,
**code produced** (real vs generated), and **LLM cost** across Claude Code,
Codex, Cursor, git, and shell activity — all from your own machine, no SaaS.

[![CI](https://github.com/Mr1000xGrowth/ship1000x/actions/workflows/test.yml/badge.svg)](https://github.com/Mr1000xGrowth/ship1000x/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)]()

## Why

Hours in front of your IDE are not hours coding. AI-assisted development mixes
thinking, prompting, waiting, reviewing, and typing in ways that make naive
"screen time" metrics useless. Ship1000x gives you honest numbers :

- **Focus time** : weighted activity intervals (not wall clock), so a 2-minute
  bathroom break doesn't inflate your session.
- **Output classified** : lines committed split into `real` / `seed` /
  `vendored` / `generated` — no more boasting about the `package-lock.json`
  diff.
- **LLM cost** : token-based estimates from rollouts (Anthropic + OpenAI
  pricing baked in), not flat-rate fiction.
- **Multi-machine** : one user, several Macs, deduped commits, attributed by
  first-machine-to-see heuristic.
- **Privacy by default** : all data lives in `~/.config/ship1000x/`. No
  telemetry. Optional opt-in push to your own S3 bucket (AWS/B2/R2/Garage).

## Install

```bash
pip install ship1000x       # once published to PyPI
ship1000x init              # 5-question setup wizard
ship1000x ingest            # first scan of your AI tool logs
ship1000x today             # see today's activity
```

While in alpha (`v0.1.0`), install from source :

```bash
git clone https://github.com/Mr1000xGrowth/ship1000x.git
cd ship1000x
pip install -e .
ship1000x init
```

## What it collects

| Source | Where | What |
|---|---|---|
| Claude Code | `~/.claude/projects/*/history.jsonl` | Sessions, tokens, cache hits, tool calls |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | Sessions, token usage, model used |
| Codex Desktop | `~/.codex/state_5.sqlite` logs | Sessions, turns, tool calls |
| Codex macOS app | `~/Library/Logs/com.openai.codex/` | Turn starts, approvals |
| Cursor | Workspace + global SQLite | Chat turns, edits |
| Cline | `~/.claude_cline/chats/*.jsonl` | Chat turns |
| git | `git log` across all repos under `$HOME` | Commits, lines added/deleted, classified |
| shell | zsh history (opt-in, requires `EXTENDED_HISTORY`) | Git-intent commands |
| Mac system | `pmset` / `log show` (opt-in) | Screen wake/sleep, unlock |
| Web exports | Drop ZIPs from ChatGPT/Claude web exports | Conversations (aggregated) |

All collectors are **read-only**. Nothing is ever modified, deleted, or
transmitted from your machine without explicit opt-in.

## Core commands

```bash
ship1000x init                     # interactive setup (consent, projects, S3 opt-in)
ship1000x ingest                   # collect events from all enabled sources
ship1000x today                    # today's activity summary
ship1000x week                     # last 7 days with project breakdown
ship1000x project my-app           # drill-down on one project
ship1000x insights                 # overview + ratios + multiplier
ship1000x multiplier               # output factor vs senior-mid baseline
ship1000x profile                  # heatmap, sessions, habits
ship1000x signals                  # alerts: burnout, project drift
ship1000x compare proj-a proj-b    # side-by-side 2 projects
ship1000x export                   # generate Markdown report
ship1000x status                   # DB state + last ingestion
ship1000x doctor                   # diagnostic + fix common issues
ship1000x delete --confirm         # wipe all local data
```

Run `ship1000x --help` for the full list (40+ commands including
`rollup`, `push`, `daily`, `audit`, `reclassify`, `discover`, `health`,
`install-scheduler`, etc.).

## Privacy & consent

The first thing `ship1000x init` does is show a consent block :

```
Ce tracker collecte UNIQUEMENT des metriques quantitatives (timestamps,
durees, compteurs). AUCUN contenu (prompts, fichiers, diffs) ne quitte
votre machine.

Si tu actives le partage cloud, SEULS les daily_rollup agreges
(date, projet, duree, nb events) sont pushes vers le bucket S3 que tu
auras configure. Le contenu brut reste 100% local.
```

Three levels per-project :

- `disabled` : project not scanned at all
- `private` : scanned locally, never pushed anywhere (default)
- `aggregated` : daily rollups (date, project, duration, event count) may
  be pushed to your own S3 bucket if `share_cloud: true`

`ship1000x init` walks you through the share level for each detected
project. Run `ship1000x projects --select` any time to reconfigure (useful
after onboarding a new client repo or moving a side project off your work
machine). `ship1000x privacy` shows the current state read-only, and
`ship1000x daily` warns you when new projects appear in the DB without an
explicit entry in your `share` map.

## Optional : push to your own S3 bucket

If you want to visualize your data in a tool other than the CLI (a home-built
dashboard, a BI tool, another script), enable cloud push :

```yaml
# config/privacy.yaml
cloud:
  provider: s3
  bucket: my-tracker-data
  endpoint: "https://s3.us-west-004.backblazeb2.com"  # or AWS/R2/Garage/...
  push_enabled: true

consent:
  share_cloud: true
```

Then run `ship1000x push`. Credentials via env (`AWS_ACCESS_KEY_ID` +
`AWS_SECRET_ACCESS_KEY`) or `~/.aws/credentials`. Use `ship1000x doctor --fix`
to interactively set them up.

Compatible with : **AWS S3**, **Backblaze B2**, **Cloudflare R2**,
**[Garage](https://garagehq.deuxfleurs.fr/) self-hosted**, **MinIO**.

## Requirements

- macOS or Linux (Windows untested)
- Python 3.10+
- Any of the AI tools you want to track actually installed and used

## Development

```bash
git clone https://github.com/Mr1000xGrowth/ship1000x.git
cd ship1000x
pip install -e ".[dev]"
pytest tests/              # 48 tests
ruff check .
```

## Roadmap

**v0.1.0** (alpha) — current
- [x] 11 collectors (Claude Code, Codex ×3, Cursor, Cline, git, shell, macOS)
- [x] Multi-machine dedup by commit hash + machine_id
- [x] Line classification (real / seed / vendored / generated)
- [x] Token-based LLM cost (Anthropic + OpenAI)
- [x] S3 push opt-in
- [x] 40+ CLI commands + Markdown export

**v0.2.0** (next)
- [x] Per-project consent wizard (`init` + `projects --select`)
- [x] Unclassified-projects warning at `daily`
- [ ] Local Flask web dashboard (`ship1000x dashboard` → `localhost:8765`)
- [ ] PyPI release
- [ ] GitHub API enrichment (CI status, PR reviews)
- [ ] Aider, Zed collectors

**v1.0.0** (when stable)
- [ ] Packaging as standalone macOS app
- [ ] Linux distro packages (deb/rpm)

## License

[MIT](LICENSE) — do whatever you want, no warranty.

## Credits

Built by [Mr1000xGrowth](https://github.com/Mr1000xGrowth).
Developed with heavy use of Claude Code and Codex — and yes, those sessions
are tracked by Ship1000x itself.
