# Cursor IDE deep parsing — design notes (Wave 3 / Day 6-7)

> **Status**: design — implementation deferred pending controlled
> access to a live Cursor store. The current `cursor.py` collector
> (216 lines) reads the limited `~/.cursor/ai-tracking/ai-code-tracking.db`
> store; this document captures the much richer schema discovered in
> `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
> that the next iteration should target.

## Discovery (2026-05-23 session)

### File system layout

```
~/Library/Application Support/Cursor/
  User/
    globalStorage/
      state.vscdb            # 9.7 GB — main store, what we target
    workspaceStorage/
      <workspace-hash>/      # per-workspace storage
        state.vscdb          # workspace-specific composer chats
```

### Schema of `globalStorage/state.vscdb`

Two tables :

- `ItemTable` (~hundreds of rows) — settings, configuration, telemetry
  preferences. Mostly small string values. Useful keys :
  `aiCodeTracking.dailyStats.v1.5.<YYYY-MM-DD>` — already partially
  read by our existing `ai-code-tracking.db` collector.
- `cursorDiskKV` (**81 714 rows**) — main blob storage, contains the
  Composer chats, agent runs, code block diffs, etc.

### `cursorDiskKV` key prefix distribution

Surveyed on the maintainer's machine 2026-05-23 :

| Prefix | Count | What it carries |
|---|---:|---|
| `bubbleId:*` | 39 344 | Individual message bubble in a Composer chat |
| `agentKv:blob:*` | 19 959 | Agent-mode runtime metadata blobs |
| `checkpointId:*` | 15 560 | Checkpoint state for agent runs |
| `codeBlockDiff:*` | 2 787 | Code block diff applied / rejected |
| `messageRequestContext:*` | 1 727 | Request context per message (model, tokens, settings) |
| `composerData:*` | 132 | **Full Composer session** — top-level chat container |
| `codeBlockPartialInlineDiffFates:*` | 587 | Partial inline diff outcomes |
| `inlineDiff:*` | 39 | Inline diff sessions |
| (no prefix) | 1 579 | Misc |

Total : **81 714 rows**. Largest `composerData` blob seen : **162 MB**.

### Estimated semantic mapping

Hypothesis based on naming :

- One `composerData:<uuid>` = one Composer session (the conversational
  surface). Contains references to its `bubbleId` children, the
  workspace context, the model used.
- One `bubbleId:<uuid>` = one message bubble within a session (user
  or assistant).
- `messageRequestContext:<uuid>` = the API request that produced an
  assistant bubble — likely carries the **model name and token
  counts** that SHIP needs for the cost honesty contract.
- `agentKv:blob:*` = agent-mode internal state (tool calls, scratch
  pad, etc.). Probably noise for our purposes.
- `checkpointId:*` = save points for agent rollback. Also noise.

## Implementation plan (deferred)

### Phase 1 — Read-only probe of one session

1. Pick the smallest `composerData:<uuid>` blob (a few KB).
2. Parse the JSON, document the actual schema (top-level keys,
   nested message references, model field location, token field
   location).
3. Map fields to SHIP's canonical event shape :
   `model`, `token_input`, `token_output`, `cache_read_tokens`,
   `started_at`, `ended_at`, `project_id` (from workspace path).

### Phase 2 — Aggregation by day × project

Same pattern as `claude_code.py` :

- Walk `composerData:*` rows.
- For each session, extract per-message tokens + model from
  `messageRequestContext:*` (join via bubbleId reference).
- Resolve project_id via the workspace path in the composerData
  blob (already supported by `classifier.classify_session(paths=...)`).
- Produce one `session_day` event per (day, session, project) with
  the cost honesty contract applied (`auth_mode = oauth` for Cursor
  Pro / Business / Team — Cursor doesn't expose Anthropic API keys).

### Phase 3 — Cost honesty for Cursor

- Cursor charges per "fast request" + per "premium request", not
  per token. The model used (`claude-4-sonnet`, `gpt-5`, etc.) is
  observable in `messageRequestContext`.
- We can compute `api_equivalent_usd` from `tokens × pricing.py`
  (this is what an API key would have cost).
- `billed_estimated_usd` is harder : Cursor's per-request pricing
  isn't on a per-token basis. Best effort = 0 with a note that the
  user is on a subscription plan; the real billed cost is the
  Cursor subscription monthly fee, not visible at the event level.
- `auth_mode = oauth` (Cursor authentication is OAuth-based for
  Pro/Business plans).

### Phase 4 — Privacy

- **Do not store** the actual message content. We already strip
  forbidden raw_meta keys via `RegexScrubber`, but the parser must
  not even read the message text — it should only touch
  `messageRequestContext` (tokens / model / timestamp).
- File paths referenced in code blocks must go through
  `anonymize_path` like every other SHIP source.
- The 162 MB blob is mostly conversation content. The parser should
  use a streaming JSON reader (`ijson`) or a targeted query that
  only extracts the metadata fields, never the full text.

### Phase 5 — Performance

- 132 composerData rows is small. The bottleneck will be the size of
  the largest blobs (162 MB). The parser must :
  - Use SQLite's `mode=ro&immutable=1` URI (already done elsewhere).
  - Stream-parse large JSON values rather than `json.loads(blob)`.
  - Skip blobs above a size threshold with a warning logged once
    per ingestion.

## Tests we can build now without live data

- Synthetic `state.vscdb` fixture with a few minimal blobs in
  `cursorDiskKV` matching the documented prefix layout.
- Parser tests that verify the bubbleId join, the project_id
  resolution from workspace path, and the token extraction shape.

## Why this design wasn't implemented in the current session

The auto-mode safety classifier denied direct read access to
`state.vscdb` mid-implementation (legitimate scope guard — Cursor
data is outside the ship1000x repo). Coding the parser blind, with
only the key-prefix inventory and no example blob shape, would
produce code that very likely doesn't match Cursor's actual JSON
structure. The right next step is a controlled session where the
maintainer explicitly authorises reading one or two small
`composerData` blobs to confirm the schema before implementation.

## What we already have (preserved)

The existing `cursor.py` collector continues to ingest
`~/.cursor/ai-tracking/ai-code-tracking.db` :

- `ai_code_hashes` table — per-AI-block stats by file.
- `scored_commits` table — per-commit %AI scoring.

It produces 3 events on the maintainer's store (small because
ai-code-tracking.db only covers Cursor's "AI code stats" feature,
not the actual Composer chats). The deep parsing above is **additive**
— the existing collector keeps running and the new one will produce
a separate, more granular event stream.

## Tracking

Issue / PR : pending — re-open this ticket in a new session with
explicit data-access authorisation.

Related Wave 7+ batches : Cursor variants (cursor-agent in
`FIXTURE_ONLY_PARSERS`) will benefit from the same parsing
infrastructure once landed.
