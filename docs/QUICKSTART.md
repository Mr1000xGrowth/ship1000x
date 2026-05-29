# Quickstart - Install And Run Ship1000x

Ship1000x is a local-first AI/dev activity tracker. By default, it reads
local metadata and keeps its data on your machine.

## Prerequisites

- macOS as the primary target; Linux works for most collectors.
- Python 3.10+.
- Git.
- At least one supported local tool with metadata to observe, such as Claude
  Code, Codex CLI, Cursor, Cline, OpenClaw, or git repositories.

## Install From Source

```bash
git clone https://github.com/Mr1000xGrowth/ship1000x
cd ship1000x
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
ship1000x init
```

`ship1000x init` walks through local identity, privacy consent, project
detection, optional scheduling, and optional cloud sync. Cloud sync is off
unless you explicitly configure a bucket you control.

## Daily Usage

```bash
ship1000x ingest
ship1000x highlights
ship1000x today
ship1000x today --compare-modes
ship1000x calibrate
ship1000x week
ship1000x project myproject --since 30d
ship1000x insights --since 30d
```

The local dashboard is available with:

```bash
ship1000x dashboard
```

## Public-Proof Checks

Before using Ship1000x as a public proof point, run the read-only gates:

```bash
ship1000x source-audit --strict-public --since 30d
ship1000x observation-audit --since 30d
ship1000x history-audit --since 30d
ship1000x reclassify --since 30d --dry-run
ship1000x public-check --strict-history --since 30d
```

These commands do not read new private stores. They evaluate already-ingested
metadata and separate safe public claims from blocked or conditional claims.
`public-check --json` also emits `public_proof_sequence`, which repeats the
safe command sequence with per-step evidence, caveats, and quote policy.
Use `proof_status` / `presentation_status` for machine decisions: `pass` means
clean public proof, `caveat` means usable only with visible caveats, and `fail`
means fix blocking quality or history failures first. The older `passed`
boolean is retained for exit-code compatibility and can be true while
`proof_status` is `caveat`.

## Privacy Quick Check

```bash
ship1000x privacy
ship1000x source-audit --since 30d
ship1000x public-check --since 30d
```

The audits expose aggregate counts, source names, quality labels, and caveats.
They do not print prompt bodies, responses, diffs, secret values, or raw local
payloads.

## Troubleshooting

**No data after install**: confirm one supported tool has local metadata, then
run `ship1000x ingest --source all`.

**A source appears partial or fragile**: run `ship1000x source-audit --since
30d --json` and inspect the source row's `risk`, `collector_stage`, expected
quality, observed quality, and next action.

**Historical rows need repair**: run `ship1000x history-audit --since 365d`,
then preview with `ship1000x reclassify --since 365d --dry-run`. Only run a
mutation after confirming the original local files or explicit provider exports
still exist.

**Cost numbers look surprising**: distinguish API-equivalent cost from
billed-estimated cost. Use `ship1000x reconcile` when you have explicit
provider billing/export approval.

## Next Steps

- Read [`SOURCE_QUALITY.md`](SOURCE_QUALITY.md) for source trust semantics.
- Read [`COVERAGE.md`](COVERAGE.md) for tracked sources and gaps.
- Read [`METHODOLOGY.md`](METHODOLOGY.md) for metric computation.
- Read [`PRIVACY.md`](PRIVACY.md) for local-first boundaries.
- Read [`TRUST_SCORE.md`](TRUST_SCORE.md) for confidence scoring.
