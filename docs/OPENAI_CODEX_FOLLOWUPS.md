# OpenAI / Codex coverage — follow-up backlog

This document tracks the OpenAI / Codex sub-sources that SHIP1000x has
investigated but not yet implemented, with the precise reason each
one is deferred. It is the canonical complement to `ship1000x coverage`,
which surfaces these as `not_supported` with an audit reference.

Last updated: 2026-05-23 (after PRs #27 → #31 closed the active-source
side of the OpenAI / Codex panorama).

## Status snapshot of the 10 OpenAI / Codex pistes

| # | Piste | Status | Notes |
|---|---|---|---|
| 1 | Codex Cloud / chatgpt.com analytics events | **Blocked / not useful here** | Analytics HTTP responses are logged in `logs_2.sqlite` but contain only response headers, no payload. The actual Codex Cloud task data is stored by ChatGPT Desktop in `~/Library/Application Support/com.openai.chat/codex-*/`, which on the maintainer's machine is empty (the user does not use Codex Cloud). See #1 below for the recovery plan if usage starts. |
| 2 | ChatGPT Desktop standalone | **Blocked — encrypted store** | `~/Library/Application Support/com.openai.chat/conversations-v3-*` stores conversations as `.data` files that are encrypted (high-entropy, no recognizable header). Decryption keys live in the macOS Keychain. See #2 below. |
| 3 | ChatGPT Web (chat.openai.com / chatgpt.com) | **Blocked — needs proxy or extension** | No continuous local source. Requires either an opt-in local HTTPS proxy (Claude Tap style) or a browser extension. Both are full sub-projects. See #3 below. |
| 4 | OpenCode collector validation | **Validation skipped — zero events** | The collector ships under `ship1000x/collectors/opencode.py`. The maintainer's store has zero OpenCode events; coverage report (`coverage --category coding_ide`) marks it `⚠️ supported (no events in window)`. Quality check is deferred until a real OpenCode session lands. |
| 5 | OpenAI Responses API request-level (proxy opt-in) | **Deferred** | Same proxy architecture as #3. Would let SHIP verify token / cost numbers against the real API payload instead of inferring from logs. Privacy-sensitive: a proxy sees prompts, completions, and tool arguments in clear. See #5 below. |
| 6 | Codex live runtime (PIDs / ports / rate limits / MCP) | **Deferred — opt-in design needed** | ABTop-style live observer. Requires its own consent flow + heartbeat aggregation policy (single tick per N seconds, never raw process tree). |
| 7 | Pricing alias drift detection | **Not done — low value here** | Codex events already resolve to a known pricing row in the current rate card (`gpt-5`, `gpt-5.5`, `gpt-5-codex`, `gpt-5-mini`, `gpt-5-nano`). The detector would alert on a future model whose name doesn't match the table; postponed until that happens. |
| 8 | codex_macapp 15 events still `unknown` after #30 | **Done** | Root cause identified and fixed in this PR. The 15 events were sessions from 2026-05-03 → 2026-05-10 whose `conversationId` no longer appears in `logs_2.sqlite` because OTEL spans rotate after ~15 days. They are now reported as `model_source=logs_2_sqlite_expired` instead of generic `unknown`, so the report tells retention drop apart from a session that never had a conversationId. |
| 9 | OpenAI billing reconcile dashboard | **Done by reusing existing tools** | `ship1000x reconcile --pair codex:openai_usage` already produces a per-day comparison. The new `ship1000x coverage --category openai` summarises the same numbers cross-source. |
| 10 | OpenAI / Codex unified report | **Done** | The new `coverage --category openai` filter gives a one-page snapshot of every Codex / ChatGPT / OpenAI sub-source, active and not-supported, with cost totals and quality breakdown. |

## Detailed recovery plans for the deferred pistes

### #1 — Codex Cloud / chatgpt.com analytics events

The `chatgpt.com/backend-api/codex/analytics-events/events` POST is
visible in `logs_2.sqlite` under `target = codex_client::default_client`,
but only the HTTP **response** is logged (status, headers, no body).
The Codex Cloud task / environment data lives in
`~/Library/Application Support/com.openai.chat/`:

- `codex-taskItems-v2-default-*` — task list
- `codex-taskDetails-v1-*` — per-task detail
- `codex-environments-*` — sandbox environments

On the maintainer's machine these directories are present but empty
(`0B`), confirming the user does not currently use Codex Cloud. When
that changes, the recovery plan is:

1. Inspect any file the dir starts to contain (likely the same `.data`
   encrypted format as #2).
2. If encrypted, fall back to #5 (proxy on the `chatgpt.com/backend-api/codex/*`
   path).

### #2 — ChatGPT Desktop standalone (`com.openai.chat`)

`conversations-v3-*/*.data` are encrypted blobs (verified: high
entropy, no SQLite / JSON / plist magic header). Decryption keys are
in the macOS Keychain under the `com.openai.chat` access group. To
read them, SHIP would need:

- A signed Keychain entitlement (only achievable through a signed
  macOS app, not a Python package), **or**
- Explicit user consent to call `security find-generic-password` and
  decrypt with the resulting symmetric key (still requires the user
  to unlock the Keychain on every run).

Both options are out of scope for the current package shape. The right
follow-up is #5 (proxy), which captures the same conversations at the
network layer before they are encrypted to disk.

### #3 — ChatGPT Web

Requires either:

- A local HTTPS proxy with installed CA (Claude Tap / mitmproxy style),
  consent-gated and allowlist-scoped to `chat.openai.com` /
  `chatgpt.com`.
- A browser extension distributed separately (Chrome / Firefox web
  stores).

Both are non-trivial sub-projects with their own privacy review,
distribution and CI pipeline. Not appropriate as a SHIP feature
addition; should ship as a sibling package.

### #5 — OpenAI Responses API proxy opt-in

Same proxy architecture as #3, intercepting `api.openai.com/v1/*` and
`chatgpt.com/backend-api/codex/*`. Would unlock:

- Request-level token / cost truth (no inference from log shapes).
- Codex Cloud data (#1).
- ChatGPT Desktop conversation content (#2).

Privacy implications are heavy (the proxy sees raw prompts, tool
arguments, and completion text). Must be:

- Strictly opt-in, with an explicit consent flow.
- Allowlist-scoped (per endpoint pattern).
- Redaction-strict (default to dropping body, only emitting token /
  cost summaries).
- Retention-bounded (24h max for raw, 30d for summaries by default).

### #6 — Codex live runtime

Needs:

- A separate "live" collector that runs as a daemon (not on the batch
  ingest schedule).
- Sampling rate that does not produce hundreds of events per minute
  (heartbeats every N seconds with diff-only emission).
- Coverage of: PIDs of `codex-cli` / `Codex.app`, open ports, MCP
  server children, context window state (read from logs_2.sqlite or
  live RPC), rate limit headers from recent responses.

The audit (P1.1) flags this as a candidate but explicitly warns that
"polling live can spam the store without aggregation". Worth doing
once the batch side is stable, which it now is.

## What this PR closes

- **Piste 8** — codex_macapp `unknown` → `logs_2_sqlite_expired` for
  the 15 sessions whose OTEL spans rotated out. Distribution is now
  100% honestly attributed (27 joined + 15 expired).
- **Piste 9** — `ship1000x coverage --category openai` lists every
  OpenAI sub-source with cost totals + quality breakdown.
- **Piste 10** — Same command, with the `--category openai` filter,
  is the unified report.
- This document — captures pistes 1, 2, 3, 4, 5, 6, 7 with the
  precise reason each one is deferred. Future contributors don't
  need to re-investigate from scratch.
