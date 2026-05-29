# GitHub Copilot agents promotion — design notes (Wave 7+ batch 7f)

> **Status**: design — implementation deferred pending controlled
> access to a real Copilot agent store AND a stable schema. Storage
> paths for Copilot agents are **undocumented and volatile across
> VS Code versions** — Phase A must precede any implementation.
> The current `copilot_agents.py` collector (122 lines) is **fixture-only**.

## Discovery (2026-05-24 session, via web research only)

### Candidate storage locations

GitHub Copilot has multiple surfaces, each with its own storage. Identified
candidates per the web search:

| Surface | macOS path (candidate) | Stability |
|---|---|---|
| Agent plugins | `~/Library/Application Support/Code/agentPlugins/github.com/<ORG>/<REPO>/` | "may change as the feature matures" — explicit warning |
| Copilot Chat data | inside `<workspace-storage>/GitHub.copilot-chat/` | per-workspace, structure not documented |
| Memory files | under `~/.vscode-insiders/` | "exact path isn't officially documented and could change" |
| Custom instructions (.instructions.md) | `~/Library/Application Support/Code/User/prompts/` (or VS Code Insiders) | per-user, semi-stable |

→ Three of the four paths are explicitly flagged as "could change between
updates" in the GitHub community docs. **This is the riskiest of the Wave 7+
batches from a path-stability angle.**

### Surface scope decision

V1 of the `collect()` SHOULD restrict itself to **one surface only**, the
most stable. Candidate priority:

1. **Copilot Chat data** in workspace storage — semi-stable, scope matches
   what users expect from "Copilot session observability".
2. Custom `.instructions.md` — stable (file-based, user folder) but contains
   user-authored instructions which are sensitive — skip for V1.
3. Agent plugins + memory files — too volatile for V1.

V1 thus targets only `<workspace-storage>/GitHub.copilot-chat/*` (path TBD
in Phase A).

## Implementation plan (4 phases — extra Phase 0 vs OpenCode/Gemini)

### Phase 0 — surface scoping decision (~0.5 j, human required)

Before any code, the maintainer confirms:

- Which of the 4 candidate paths exists on their machine.
- Which surface gives the most useful signal for SHIP (probably Copilot Chat
  data — per-workspace session metadata).
- Whether VS Code Insiders is in scope (`~/.vscode-insiders/` vs `~/.vscode/`).

### Phase A — schema confirmation (~0.5 j)

Open ONE Copilot Chat data file from the chosen surface, read-only. Document
schema. Identify safe scalar keys. Same shape as Gemini CLI / OpenCode.

**Schema drift risk is highest here**: GitHub may change the format between
VS Code releases. The design doc must include a **version detection key**
(read file header / format marker, refuse parsing if unknown — fallback
to fixture-only schema).

### Phase B — `collect()` runtime (~1 j)

Discovery: iterate VS Code workspace storage roots:

```python
VSCODE_WORKSPACE_BASES = [
    Path.home() / "Library/Application Support/Code/User/workspaceStorage",
    Path.home() / "Library/Application Support/Code - Insiders/User/workspaceStorage",
]

def collect(storage, classifier, privacy_config):
    for base in VSCODE_WORKSPACE_BASES:
        if not base.exists():
            continue
        for workspace_dir in base.iterdir():
            copilot_dir = workspace_dir / "GitHub.copilot-chat"
            if not copilot_dir.exists():
                continue
            # parse with version detection
```

Critical safeguards:

- **Skip silently on unknown schema version** (don't crash).
- **No chat body extraction** — count messages, model id, timestamps, token
  totals if present; never `request`, `response`, `prompt`, `system_prompt`.
- **No Bearer tokens** even if accidentally found — sanitize_event filters.
- **Workspace storage path** is hashed → use the workspace hash as
  candidate `project_id` after classifier normalisation.

### Phase C — promotion (~0.5 j)

Same as OpenCode/Gemini CLI: move to ACTIVE_COLLECTORS, add tests, SOURCE_REGISTRY, CHANGELOG.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Path changes between VS Code versions | **High** | Version marker check; refuse parsing on unknown; log warning instead of error. |
| Multiple paths simultaneous (Code + Insiders) | Medium | Iterate both bases; idempotence via offset key embedding the base. |
| Body content leak (chat messages, instructions) | **Very high** | Scalar-only whitelist; anti-leak test asserting `prompt`/`response`/`message_body`/`instructions`/`Bearer`/`gh_pat` absent from serialised event. |
| Custom .instructions.md may contain sensitive project policy text | High | **Out of scope for V1.** Hard-skip `.instructions.md` files. |
| Schema not documented officially | High | Phase A locks the schema explicitly in this design doc; bump and re-Phase-A on each VS Code major upgrade. |
| Workspace hash → de-anonymisation | Low | Run through classifier; never expose raw workspace path. |

## Anti-targets

- Read `~/.vscode-insiders/` memory files (path not stable, schema unknown,
  potentially carries secrets).
- Read `<workspace>/User/prompts/*.instructions.md` (user-authored sensitive
  content).
- Read agent plugin code or manifests from
  `~/Library/Application Support/Code/agentPlugins/` — not session data.
- Open any GitHub PAT, OAuth token, or `gh_pat_` cookie even if found.
- Talk to the GitHub API. SHIP only reads local persisted Copilot Chat
  data.

## Open questions for Phase 0

1. Should SHIP cover **both** Code stable and Code Insiders, or stable only?
2. Should `.instructions.md` ever be in scope (count only, never content)?
3. How should SHIP handle the case where the same Copilot session has data
   in both `agentPlugins/` and `workspaceStorage/<hash>/GitHub.copilot-chat/`?
   Likely: V1 trusts only `workspaceStorage`.

## Reference

- [Where does VS Code Copilot store the local index?](https://github.com/orgs/community/discussions/152490)
- [Agent plugins in VS Code (Preview)](https://code.visualstudio.com/docs/copilot/customization/agent-plugins)
- [Memory file storage discussion](https://github.com/orgs/community/discussions/189688)
- Existing fixture parser: `ship1000x/collectors/copilot_agents.py`
- Existing fixture: `tests/fixtures/provider_expansion/copilot_agents_session.json`
- Pattern reference: `ship1000x/collectors/cline.py` (multi-workspace storage iteration).
