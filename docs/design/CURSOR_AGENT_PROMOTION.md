# Cursor Agent promotion — design notes (Wave 7+ sub-lot, complète batch 7a)

> **Status**: design — implementation deferred. The current
> `cursor_agent.py` collector (69 lines) is **fixture-only**. Cursor
> Agent is intentionally kept as a separate identity from the generic
> Cursor AI-block collector (`cursor.py`, which ships `ai_blocks_daily`
> since V0.9) because Cursor Agent has its own session model with
> per-task scope, whereas `cursor.py` aggregates blocks per day.

## Discovery (2026-05-24 session — Cursor stores already partially known)

### Candidate storage locations (existing Cursor collector context)

The existing `cursor.py` collector reads from
`~/.cursor/ai-tracking/ai-code-tracking.db` (limited daily blocks store).
Cursor Agent uses a **different, richer store** also under Cursor's
state DB:

```
~/Library/Application Support/Cursor/
  User/
    globalStorage/
      state.vscdb            # main 9+ GB store (cf. CURSOR_DEEP_PARSING.md)
    workspaceStorage/
      <workspace-hash>/
        state.vscdb          # per-workspace store
```

The `CURSOR_DEEP_PARSING.md` design doc (Wave 3 D6-7) covers the deep
parsing of `globalStorage/state.vscdb` with its 81 714 rows including
`composerData` blobs (up to 162 MB each) — that is where Cursor Agent
sessions live alongside other interactions.

→ Cursor Agent promotion is **directly downstream of the Wave 3 D6-7
Cursor deep parsing implementation**. Until `CURSOR_DEEP_PARSING.md` Phase
A and B land, Cursor Agent stays fixture-only.

## Implementation plan (depends on CURSOR_DEEP_PARSING)

### Phase 0 — coordinate with Cursor deep parsing

Cursor Agent and the broader Cursor deep parsing share the same
`state.vscdb` store. Promoting Cursor Agent alone (without the broader
deep parsing) means duplicating the SQLite reader. Two options:

- **Option A**: ship Cursor deep parsing first (CURSOR_DEEP_PARSING.md
  Phase A-E), then Cursor Agent reuses the same reader with a filter on
  `composerData` of type `agent`.
- **Option B**: ship Cursor Agent first as a narrow reader that only
  selects rows tagged `agent` — risks future drift when broader deep
  parsing lands.

**Recommendation**: Option A. Wait for `CURSOR_DEEP_PARSING.md` Phase A
before scoping Cursor Agent.

### Phase A — schema confirmation (~0.5 j, depends on CURSOR_DEEP_PARSING)

Inspect a row of type `agent` in `composerData` of a real
`state.vscdb`, read-only. Document the schema. The fixture-only schema
(`ship1000x.cursor_agent.session.v1`) was set conservatively without
real data — re-validate.

### Phase B — `collect()` runtime (~1 j)

Reuse the SQLite reader from `CURSOR_DEEP_PARSING.md` Phase B. Add a
filter on Cursor Agent session type. Emit one event per agent
session, source `cursor_agent`, distinguished from `cursor` source via
the collector identity.

### Phase C — promotion (~0.5 j)

Same as other Wave 7+ promotions: move `"cursor_agent"` from
`FIXTURE_ONLY_PARSERS` to `ACTIVE_COLLECTORS`, preserve existing
fixture test `test_cursor_agent_fixture_is_separate_from_generic_cursor`,
add live test, SOURCE_REGISTRY, CHANGELOG.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Bloat from `composerData` blobs (up to 162 MB each) | High | Only read the small metadata fields; never load the full blob into memory. Use SQLite limit/offset or column projection. |
| Coupling with Cursor deep parsing | High | Strict Phase 0 sequence: deep parsing reader lands first. |
| Schema drift between Cursor versions | Medium | Schema marker check, refuse parsing on unknown version, log warning. |
| Cursor Agent body content (instructions, completions) leak | High | Scalar-only whitelist, anti-leak test on body field names. |
| Confusion with `cursor.py` `ai_blocks_daily` events | Low | Two distinct collector identities — `cursor` keeps batch ai-block scope; `cursor_agent` covers per-session agent activity. |

## Anti-targets

- Read `composerData` blob bodies (prompts, responses, code suggestions).
- Read `bubbleId` row contents (per-message data with full content).
- Read `messageRequestContext` row contents (system context).
- Open the workspace `state.vscdb` (workspaceStorage) before Phase 0
  confirms scope.

## Dependency on `CURSOR_DEEP_PARSING.md`

```
CURSOR_DEEP_PARSING.md Phase A (schema discovery)
   ↓
CURSOR_DEEP_PARSING.md Phase B (SQLite reader)
   ↓
CURSOR_AGENT_PROMOTION.md Phase A (filter row type agent)
   ↓
CURSOR_AGENT_PROMOTION.md Phase B (collect() reusing reader)
   ↓
CURSOR_AGENT_PROMOTION.md Phase C (promotion)
```

## Reference

- `ship1000x/collectors/cursor_agent.py` — current fixture-only parser
- `ship1000x/collectors/cursor.py` — generic Cursor collector (`ai_blocks_daily`)
- `tests/fixtures/provider_expansion/cursor_agent_session.json` — fixture
- `docs/design/CURSOR_DEEP_PARSING.md` — upstream design doc, dependency
