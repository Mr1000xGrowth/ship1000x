#!/usr/bin/env bash
# install-ship-launchd.sh — opt-in installer for SHIP1000x launchd agents
#
# Companion to docs/SHIP_LOCAL_CADENCE.md. Idempotent. Validates
# every plist via `plutil -lint` before loading. NEVER auto-runs ;
# the operator invokes this script explicitly.
#
# Usage :
#   bash scripts/install-ship-launchd.sh [--dry-run] [--scope status|daily|doctor|all] [--venv <path>]
#
# Defaults :
#   --scope all
#   --venv ./.venv (assumes the operator created a virtualenv at repo root)
#
# Flags :
#   --dry-run   : print the plist contents + plutil-lint result, never
#                 copy or load.
#   --scope     : which agent(s) to install ; defaults to all 3.
#   --venv      : path to the venv whose `bin/ship1000x` will be
#                 referenced ; resolved to absolute before writing.
#
# Exit codes :
#   0  success
#   1  user / environment error (venv missing, etc.)
#   2  argparse error
#   3  plutil-lint failure
#   4  launchctl failure
#
# Safety :
#   * Only modifies files under ~/Library/LaunchAgents/com.mr1000xgrowth.ship1000x.*.plist
#     and ~/Library/Logs/ship1000x-*.log (the latter implicitly via
#     StandardOutPath / StandardErrorPath).
#   * Never sudo. Per-user launchd domain only (gui/$UID).
#   * Never reads provider stores, secrets, .env, keychains.
#   * Never installs anything for the usage proxy — the proxy stays foreground /
#     per-session by design (docs/SHIP_LOCAL_CADENCE.md § anti-pattern).

set -euo pipefail

DRY_RUN=0
SCOPE="all"
VENV_PATH="$(pwd)/.venv"

usage() {
    cat >&2 <<USAGE
Usage : bash scripts/install-ship-launchd.sh [--dry-run] [--scope status|daily|doctor|all] [--venv <path>]

  --dry-run   : print + lint plists, never copy or load.
  --scope     : status | daily | doctor | all (default : all).
  --venv      : path to the venv (default : ./.venv).

Companion : docs/SHIP_LOCAL_CADENCE.md.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --scope)   SCOPE="$2"; shift 2 ;;
        --venv)    VENV_PATH="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Erreur : unknown flag $1" >&2; usage; exit 2 ;;
    esac
done

case "$SCOPE" in
    status|daily|doctor|all) ;;
    *) echo "Erreur : --scope must be status | daily | doctor | all (got '$SCOPE')" >&2; exit 2 ;;
esac

# Preflight : venv must exist and contain bin/ship1000x.
if [[ $DRY_RUN -eq 0 ]]; then
    if [[ ! -d "$VENV_PATH" ]]; then
        echo "Erreur : --venv $VENV_PATH does not exist." >&2
        echo "  Create one with : python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
        exit 1
    fi
    VENV_PATH="$(cd "$VENV_PATH" && pwd)"  # absolute
    SHIP_BIN="$VENV_PATH/bin/ship1000x"
    if [[ ! -x "$SHIP_BIN" ]]; then
        echo "Erreur : $SHIP_BIN is missing or not executable." >&2
        echo "  Did you 'pip install -e .' in the venv ?" >&2
        exit 1
    fi
else
    # In dry-run, still resolve paths but tolerate missing venv.
    VENV_PATH="$(cd "$VENV_PATH" 2>/dev/null && pwd || echo "$VENV_PATH")"
    SHIP_BIN="$VENV_PATH/bin/ship1000x"
fi

LOG_DIR="$HOME/Library/Logs"
AGENT_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LOG_DIR" "$AGENT_DIR"

# Render one plist. Returns 0 if plutil-lint passes, exits 3 otherwise.
render_plist() {
    local name="$1"          # status | daily | doctor
    local label="com.mr1000xgrowth.ship1000x.$name"
    local plist="$AGENT_DIR/$label.plist"
    local stdout_log="$LOG_DIR/ship1000x-$name.log"
    local stderr_log="$LOG_DIR/ship1000x-$name.err.log"
    local schedule_block

    case "$name" in
        status)
            # Hourly, weekdays only, 9h-19h.
            local interval=""
            for hour in 9 10 11 12 13 14 15 16 17 18 19; do
                for weekday in 1 2 3 4 5; do
                    interval+="    <dict><key>Hour</key><integer>${hour}</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>${weekday}</integer></dict>
"
                done
            done
            schedule_block="  <key>StartCalendarInterval</key>
  <array>
${interval}  </array>"
            ;;
        daily)
            schedule_block="  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>"
            ;;
        doctor)
            schedule_block="  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>30</integer>
  </dict>"
            ;;
    esac

    local body
    body=$(cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${SHIP_BIN}</string>
    <string>${name}</string>
  </array>
${schedule_block}
  <key>StandardOutPath</key>
  <string>${stdout_log}</string>
  <key>StandardErrorPath</key>
  <string>${stderr_log}</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
PLIST
)

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "==== [dry-run] $plist ===="
        echo "$body"
        # Lint via plutil on a tmp file.
        local tmp
        tmp="$(mktemp -t "ship1000x-${name}.XXXX.plist")"
        printf '%s\n' "$body" > "$tmp"
        if plutil -lint "$tmp" >/dev/null 2>&1; then
            echo "[dry-run] plutil -lint : OK"
        else
            echo "[dry-run] plutil -lint : FAILED" >&2
            plutil -lint "$tmp"
            rm -f "$tmp"
            exit 3
        fi
        rm -f "$tmp"
        echo ""
        return 0
    fi

    printf '%s\n' "$body" > "$plist"
    if ! plutil -lint "$plist" >/dev/null 2>&1; then
        echo "Erreur : plutil -lint failed on $plist" >&2
        plutil -lint "$plist"
        exit 3
    fi
    echo "[install] wrote $plist (plutil-lint OK)"

    # Load via launchctl (idempotent : bootout first if loaded, then bootstrap).
    if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
        launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    fi
    if ! launchctl bootstrap "gui/$UID" "$plist" 2>&1 | tee /tmp/launchctl-bootstrap.log; then
        echo "Erreur : launchctl bootstrap failed for $label" >&2
        exit 4
    fi
    echo "[install] loaded $label via launchctl (per-user gui/$UID domain)"
}

# Dispatch.
case "$SCOPE" in
    status|daily|doctor) render_plist "$SCOPE" ;;
    all)
        render_plist status
        render_plist daily
        render_plist doctor
        ;;
esac

if [[ $DRY_RUN -eq 1 ]]; then
    echo ""
    echo "Dry-run complete. No file was written, no agent was loaded."
    echo "Re-run without --dry-run to install."
else
    echo ""
    echo "Done. SHIP1000x launchd agents installed in the per-user domain."
    echo "Logs : $LOG_DIR/ship1000x-{status,daily,doctor}.log"
    echo "Uninstall : bash scripts/uninstall-ship-launchd.sh"
fi
