# Observation audit

`ship1000x observation-audit` is the capability-level companion to
`ship1000x source-audit`.

- `source-audit` checks whether observed local sources have trustworthy
  token/cost/active-time metadata.
- `observation-audit` checks the broader observability map:
  batch/local usage, provider coverage, correlation, live runtime, OS context,
  API truth, and privacy boundaries.

```bash
ship1000x observation-audit --since 30d
ship1000x observation-audit --since 30d --json
```

The JSON schema is `ship1000x.observation_audit_report.v1`.

The command may read already-indexed Ship1000x event counts from the local
tracker database when a user runs it, but it never reads new Codex, Claude,
Cursor, Cline, shell, Git, system logs, provider APIs, prompts, responses,
diffs, or file contents. It returns statuses and counts only.

The report also includes a provider capability gap registry. This makes the
public boundary explicit for tools that appear in the open-source audit but are
not yet first-class SHIP sources. Examples include Gemini CLI, GitHub Copilot /
Copilot Chat, OpenCode, Cursor Agent, Continue, Aider, Antigravity, Goose,
Crush, Kimi, and Qwen. These rows are intentionally
non-blocking warnings in `public-check`: they prevent overclaiming without
pretending that missing providers are already measured.

Gemini CLI, Copilot agents, OpenCode, and Cursor Agent also have conservative
source-quality profiles. That means a fixture-shaped event is no longer an
`unknown-source`, but the provider remains `needs-fixture` in this audit until
SHIP has a read-only collector backed by redacted synthetic source fixtures.
Roo/Kilo Code is now a dedicated partial provider capability: SHIP can observe
bounded activity metadata through `roo_kilo_code`, but token/cost truth is
still explicitly incomplete.

## Status vocabulary

| Status | Meaning |
|---|---|
| `implemented` | SHIP has a current, fixture-backed capability. |
| `partial` | Useful signal exists, but gaps or unknown quality remain. |
| `planned` | Belongs to a future explicit live-capture capability. |
| `deferred` | Intentionally out of scope for SHIP public-trust metrics. |

## Provider gap vocabulary

| Status | Meaning |
|---|---|
| `supported` | Fixture-backed batch/local SHIP coverage exists for a bounded claim. |
| `partial` | Activity coverage or metadata exists, but token/cost truth is incomplete. |
| `needs-fixture` | Known provider/tool gap that must receive synthetic fixtures before claims. |
| `planned` | Known roadmap provider/tool, not observed by SHIP today. |
| `deferred` | Belongs to a separate consented capture capability. |

## Current boundary

SHIP is the batch/local usage source. It should not silently grow into every
observability mode:

- Live runtime heartbeat signals belong to a separate live-capture tool.
- Strict opt-in API truth / proxy reconciliation belongs to a separate
  consented local proxy.
- A downstream layer should own the common timeline and cross-tool contract.
