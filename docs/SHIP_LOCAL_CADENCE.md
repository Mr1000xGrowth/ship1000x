# SHIP1000x local cadence — runbook

> **Status** : non-normative runbook. Local-machine convention for
> single-user Mac setups. Adapt before deploying anywhere else.
> Last updated 2026-05-25.

## Why this document exists

SHIP1000x is a **batch / local usage** collector. It is the one
mode of LLM-activity observation that is **safe to run on a
cadence** because :

- it never decrypts in-flight traffic ;
- it never installs a CA ;
- it never reads provider stores ;
- it never persists prompt / response bodies ;
- it operates only on artefacts the user already has on disk
  (logs, exports, drop files from the optional usage proxy).

By contrast, **the optional usage proxy must NOT be put on a
cadence**. It is a per-session, foreground, opt-in proxy. Its CA
stays installed between sessions, but the proxy process only runs
when the user explicitly starts it for a defined debugging session.

This runbook documents the recommended SHIP local cadence and
makes the proxy-not-on-cadence position explicit so future agents
do not try to "automate everything".

## TL;DR

| When | What | Why |
|---|---|---|
| Every hour (lightweight) | `ship1000x status` | Heartbeat : DB writable, ingestion fresh, source counts visible. |
| Every day at 03:00 local | `ship1000x daily` | Full pipeline : ingest all sources → rollup → optional health push. |
| Every day at 03:30 local | `ship1000x doctor` | Diagnostic : config drift, source coverage, suggestions. Read-only. |
| Per session, foreground | local usage proxy (opt-in) | **Opt-in.** Started manually by the user when they want to observe HTTPS metadata, stopped by them. Never scheduled. |

## Hourly lightweight — `ship1000x status`

Cheap heartbeat. Reads the local SQLite, prints the last
ingestion timestamp, event counts, DB size. Does **not** touch
the network, providers, or any source under `~/`.

Typical output (sanitised) :

```text
SHIP1000x status
  events       : 124 372
  sessions     : 1 049
  db size      : 184.2 MB
  last ingest  : 2026-05-25T09:00:12Z (12 min ago)
```

Recommended schedule : every hour on the hour, weekdays only.
Skip nights and weekends — there's nothing to observe.

If `status` reports a `last ingest` older than 36 h, the
maintainer should run `ship1000x doctor` manually before assuming
the data layer is healthy.

## Daily full — `ship1000x daily`

This command already exists in `ship1000x/cli.py` and is shaped
for cron / launchd : `ingest all → rollup → push (if configured)`.

Recommended schedule : every day at 03:00 local. The window is
chosen so :

- the user is asleep (no foreground contention) ;
- the rollup operates on a fully-closed previous day ;
- the optional `push-health` runs when the network is quiet.

The command is idempotent — re-running it within the same day
will not double-count rollups.

## Daily diagnostic — `ship1000x doctor`

Read-only diagnostic. Reports config drift, source coverage,
adapter readiness, suggested actions. Never mutates state without
`--fix`.

Recommended schedule : 30 minutes after `daily` so the rollup is
complete before the doctor inspects it.

## launchd template (dry-run only)

The template below is **not installed by this commit** — it is a
reference the maintainer can adapt and load manually with
`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.mr1000xgrowth.ship1000x.<schedule>.plist`.

Replace `/Users/YOUR_USER/path/to/ship1000x` with
the actual repo path. Replace the venv path if the install uses a
different one.

### `com.mr1000xgrowth.ship1000x.status.plist` (hourly)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mr1000xgrowth.ship1000x.status</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/path/to/ship1000x/.venv/bin/ship1000x</string>
    <string>status</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>1</integer></dict>
    <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>1</integer></dict>
    <!-- ... extend per hour, per weekday ... -->
  </array>
  <key>StandardOutPath</key>
  <string>/Users/YOUR_USER/Library/Logs/ship1000x-status.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOUR_USER/Library/Logs/ship1000x-status.err.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
```

### `com.mr1000xgrowth.ship1000x.daily.plist` (daily 03:00)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mr1000xgrowth.ship1000x.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/path/to/ship1000x/.venv/bin/ship1000x</string>
    <string>daily</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/YOUR_USER/Library/Logs/ship1000x-daily.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOUR_USER/Library/Logs/ship1000x-daily.err.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
```

### `com.mr1000xgrowth.ship1000x.doctor.plist` (daily 03:30)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mr1000xgrowth.ship1000x.doctor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/path/to/ship1000x/.venv/bin/ship1000x</string>
    <string>doctor</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/YOUR_USER/Library/Logs/ship1000x-doctor.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOUR_USER/Library/Logs/ship1000x-doctor.err.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
```

### Dry-run / load / unload

```bash
# Validate the plist without loading it.
plutil -lint ~/Library/LaunchAgents/com.mr1000xgrowth.ship1000x.daily.plist

# Load (one-time, will then run on schedule).
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.mr1000xgrowth.ship1000x.daily.plist

# Force one immediate run (useful for testing).
launchctl kickstart -p gui/$UID/com.mr1000xgrowth.ship1000x.daily

# Unload.
launchctl bootout gui/$UID/com.mr1000xgrowth.ship1000x.daily
```

### Opt-in installer scripts (since 2026-05-25)

To avoid hand-copying the plists above, two opt-in helper scripts
ship under `scripts/` :

```bash
# Dry-run : render the 3 plists to stdout, plutil-lint each, never
# touch ~/Library/LaunchAgents/ or launchctl.
bash scripts/install-ship-launchd.sh --dry-run

# Install all 3 agents (status hourly weekdays, daily 03:00,
# doctor 03:30). Idempotent — re-runs bootout the previous load
# then bootstrap the new plist.
bash scripts/install-ship-launchd.sh

# Install only a subset.
bash scripts/install-ship-launchd.sh --scope daily

# Override the venv path (default : ./.venv).
bash scripts/install-ship-launchd.sh --venv ~/my-venv

# Uninstall (removes plist + bootout + deletes logs by default).
bash scripts/uninstall-ship-launchd.sh

# Uninstall but keep the logs.
bash scripts/uninstall-ship-launchd.sh --keep-logs
```

Preflight in `install-ship-launchd.sh` :
- `--venv` directory must exist + contain `bin/ship1000x`
  executable. Errors out with the `pip install -e .` hint if not.
- Every plist is `plutil -lint`'d **before** being copied into
  `~/Library/LaunchAgents/`. Lint failure → exit 3, no install.
- `launchctl bootout` is run first if the agent label is already
  loaded, to make `--scope all` re-installs safe.
- Only writes under `~/Library/LaunchAgents/com.mr1000xgrowth.ship1000x.*.plist`
  and `~/Library/Logs/ship1000x-*.log`. **Never** `sudo`. Per-user
  `gui/$UID` domain only.
- **No usage-proxy plist** is installed by these scripts ; the proxy stays
  foreground / per-session by design (see § "anti-pattern" below).

Both scripts have `bash -n` clean syntax (verified on `/bin/bash 3.2.57`)
and `plutil -lint` clean output (verified on the 3 plists they
generate).

## What this runbook explicitly does NOT recommend

- **No always-on usage proxy.** The proxy remains foreground and
  per-session.
- **No automatic CA re-install.** Installing the proxy's CA requires
  Touch ID and is intentionally non-automatable.
- **No real provider-store reads** (Anthropic web console, OpenAI
  dashboard, Claude.ai DB). SHIP works on local artefacts only.
- **No prompt / response body persistence** anywhere in the
  cadence. The doctor must surface a leak if it ever appears.
- **No network egress beyond `push-health`** (which is opt-in and
  scoped to a single object store the user configured).

## Cross-references

- `ship1000x/ship1000x/cli.py` — `status`, `daily`, `doctor`
  command sources.
