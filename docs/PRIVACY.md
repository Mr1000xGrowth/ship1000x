# Privacy — What Ship1000x stores (and what it never does)

Reference document explaining **what data leaves your machine**, **what stays local-only**, and **how the privacy filter is enforced** at the architectural level.

Last updated: 2026-05-15 (V1).

---

## Non-negotiable principle

> **No content** (prompt, file, diff, response, stdout, command) ever touches the local DB or any network endpoint. Only **quantitative metadata**.

Ship1000x is designed as **local-first**: by default, no data leaves your machine. Cloud push is **opt-in**, requires explicit consent, and goes through a conservative filter.

---

## What gets stored locally (and only locally)

The local SQLite DB at `~/ship1000x/db/tracker.sqlite` contains:

### Per event (table `events`)

| Field | Example | Sensitive? |
|---|---|---|
| `id` | `claude_code:abc123:2026-04-23` | No (opaque) |
| `source` | `claude_code` | No |
| `event_type` | `session_day` | No |
| `started_at` / `ended_at` | `2026-04-23T14:32:15Z` | No |
| `duration_sec` / `wall_clock_sec` | `3600` | No |
| `cwd` | `~/work/ship1000x` (anonymized) | Anonymized — username replaced by `~` |
| `project_id` | `ship1000x` | No (slug, not path) |
| `tool_or_action` | `Edit` | No (tool name only, no params) |
| `token_input` / `token_output` | `12345` | No |
| `cost_estimated` | `0.42` | No |
| `user_msg_type` | `typed` / `approval` / `paste` / `system` / `tool_result` | No (category, no content) |
| `wordcount` | `42` | No (count only, no text) |
| `payload_hash` | `a1b2c3d4...` | No (16-char SHA256, no original content) |
| `raw_meta` | JSON with whitelisted keys only | Filtered by `sanitize_event` |
| `machine_id` | `Mac-Studio.local` | Local hostname (not user info) |

### What is NEVER in the DB

- No prompt content (typed, pasted, dictated)
- No file content (Read, Write, Edit input/output)
- No command stdout/stderr (Bash result)
- No diff (Edit old_string / new_string)
- No assistant response text
- No tool result contents
- No model reasoning traces
- No absolute paths to user home (always anonymized to `~`)
- No keystroke logging
- No screen capture
- No clipboard content

---

## The filter: `sanitize_event` (core/privacy.py)

Every event passes through `sanitize_event` **before** being inserted into the DB.

### Multiple defense layers

```python
# Layer 1: Forbidden keys at top level
FORBIDDEN_META_KEYS = {
    "content", "text", "message", "prompt", "response",
    "body", "source_code", "diff", "file_content",
    "input", "output", "result", "stdout", "stderr",
    "command", "new_string", "old_string",
}
# These keys are stripped wherever they appear.

# Layer 2: Whitelist of allowed metadata keys (46 keys total)
ALLOWED_META_KEYS = {
    # Opaque identifiers (no content)
    "session_id", "session_uuid", "process_uuid", "workspace_id",
    "task_id", "pid", "commit_hash", "primary_project",
    # Numeric counters
    "lines_added", "lines_deleted", "files_changed", ...
    # Tokens (exposed for audit)
    "cache_read_tokens", "cache_write_tokens", ...
    # Categorical short strings
    "model", "mode", "auth_mode", "source_api", ...
    # Aggregated structures (timeline, model stats, ratios)
    "model_stats", "event_timeline", "tool_calls", "split_ratio",
    # Paths to anonymize (handled specifically)
    "paths_sampled", "files_touched", "log_file",
}
# Anything not in this list is silently dropped.

# Layer 3: Recursive path anonymization
META_KEYS_WITH_PATHS = {
    "paths_sampled", "files_touched", "log_file", "primary_project",
}
# Lists and nested dicts get _anonymize_value() applied recursively.
```

### Central guardrail in `core/storage.upsert_event`

After patch `fix(storage) 5776103`, `sanitize_event` is called **automatically** at the single DB entry point (`upsert_event`). This guarantees:

- Even if a future collector forgets to call `sanitize_event` upstream, the filter still runs
- The contract "no event reaches the DB unfiltered" is enforced at the storage layer, not just at convention

```python
def upsert_event(self, event: dict[str, Any], replace: bool = False) -> None:
    """Central guardrail: sanitize_event always applied, even if the collector
    already called it (idempotent). Guarantees no unfiltered event reaches the DB,
    including for any future collector that might forget the call.
    """
    event = sanitize_event(event)  # ← always runs
    ...
```

### Path anonymization

Every `cwd` and every path in metadata is converted before storage:

```
/Users/alice/work/project/file.py  →  ~/work/project/file.py
/home/bob/code/lib/util.js         →  ~/code/lib/util.js
```

The username never appears in the DB.

---

## Cloud push (optional, opt-in)

If you want to share aggregated metrics with collaborators via a Garage S3 bucket you control, Ship1000x provides `tracker push insights` with a **conservative filter** by default.

### Default filter (`share_config: null` or unset)

After patch `fix(insights_push) febe2ea`:

| Field | Default behavior |
|---|---|
| `user_email` | **Hashed** (SHA256:16 chars) — opt-in via `share_email: true` |
| `machine_id` | Sent **clear** (needed for multi-Mac routing) — opt-out via `share_machine_id: false` |
| Financials (`tjm_eur_per_day`, `value_produit_eur`, `equivalent_value_eur`, `ratio_value_per_cost`) | **Stripped** — opt-in via `share_financials: true` |
| `project_ids` | **Hashed** (`proj-<sha16>`) — opt-in `full` or opt-out `none` via `share_projects` |
| `signals` (intensity P95, etc.) | Sent | opt-out via `share_signals: false` |
| `profile` (primary tool) | Sent | opt-out via `share_profile: false` |

→ Even if your S3 bucket leaks, **no PII or financial data is exposed by default**.

### Configuration in `privacy.yaml`

```yaml
cloud:
  endpoint: https://...
  bucket: ...
  share_config:
    share_email: false        # default: hash
    share_machine_id: true    # default: clear (multi-Mac routing)
    share_financials: false   # default: strip TJM / value_produit
    share_projects: aggregate # full | aggregate | none
    share_signals: true
    share_profile: true
```

### Auditability

The applied `share_config` is stored in the payload's `_meta.share_config_applied` field. Server-side, you can verify exactly what was applied to each push.

---

## The 4 collectors that touch external APIs

Most collectors read **only local files**. Three of them connect to external APIs (always with your explicit credentials):

| Collector | Endpoint | What it reads | What is sent |
|---|---|---|---|
| `anthropic_usage` | `https://api.anthropic.com/v1/organizations/usage_report` (Admin API) | Your aggregated billing data | Nothing |
| `openai_usage` | OpenAI usage API | Your aggregated billing data | Nothing |
| `web_exports` | None (reads local drop folder) | Your manual exports of ChatGPT/Claude/Gemini | Nothing |

→ **No prompt, no response, no token-by-token detail is fetched from these APIs.** Only daily aggregates per workspace.

---

## What you can do to verify

### Inspect the DB yourself

```bash
sqlite3 ~/ship1000x/db/tracker.sqlite

# See the schema (no content fields)
.schema events

# Sample 5 events with all metadata
SELECT * FROM events ORDER BY started_at DESC LIMIT 5;

# Confirm raw_meta contains only quantitative metadata
SELECT raw_meta FROM events WHERE source='claude_code' LIMIT 3;
```

You will see only counts, IDs, timestamps, anonymized paths. No prompt, no code, no response.

### Inspect a cloud push payload (dry-run)

```bash
tracker push insights --dry-run --since 7d
```

This prints what would be sent to S3 **without sending it**. You can verify your `share_config` filter is applied as expected.

### Read the source code

The privacy filter is concentrated in **2 files**:

- `core/privacy.py` (~150 lines): the `sanitize_event` function and its allowed/forbidden key lists
- `core/storage.py` (`upsert_event`): the central guardrail call

These files are designed to be auditable in 5 minutes.

---

## Threat model

### What Ship1000x protects against

- ✅ **Accidental leak via local backup**: the DB has no content, so a leaked Time Machine backup exposes nothing sensitive
- ✅ **Accidental leak via cloud push**: conservative filter strips PII and financials by default
- ✅ **Bug in a future collector**: central guardrail in `upsert_event` catches forgotten sanitizations
- ✅ **Path/username leak via metadata**: recursive anonymization
- ✅ **Cross-machine multi-user setups**: `machine_id` segregates rows, `user_email` is hashed in cloud push by default

### What Ship1000x does NOT protect against

- ❌ **Active attacker on your machine**: if your local machine is compromised, the DB is readable. Use disk encryption.
- ❌ **Snapshot-level exfiltration of source files**: Ship1000x doesn't read your code, but a generic exfiltration tool can. Use disk encryption + endpoint security.
- ❌ **Network sniffing of cloud push**: TLS is enforced (Garage S3 with `https://`), but you should also restrict bucket ACL.
- ❌ **You configuring your own bucket badly**: if your S3 bucket is publicly readable, all `share_config`-filtered payloads are still readable. Configure ACL properly.

---

## Reporting a privacy issue

If you discover that Ship1000x stores or transmits content it shouldn't:

1. Open a `[security]` GitHub issue with reproduction steps.
2. For sensitive reports, use the SECURITY.md contact (TBD sprint 2).

We treat privacy regressions as **P0 bugs** (highest severity).

---

See also:
- [COVERAGE.md](COVERAGE.md): sources × confidence level matrix
- [METHODOLOGY.md](METHODOLOGY.md): how each metric is computed
- [TRUST_SCORE.md](TRUST_SCORE.md): how the confidence score is computed per module
