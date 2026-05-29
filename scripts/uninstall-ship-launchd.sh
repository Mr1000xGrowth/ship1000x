#!/usr/bin/env bash
# uninstall-ship-launchd.sh — opt-in uninstaller for SHIP1000x launchd agents
#
# Companion to install-ship-launchd.sh and docs/SHIP_LOCAL_CADENCE.md.
# Idempotent : safe to run when an agent is missing / not loaded.
#
# Usage :
#   bash scripts/uninstall-ship-launchd.sh [--scope status|daily|doctor|all] [--keep-logs]
#
# Defaults :
#   --scope all
#   logs deleted unless --keep-logs is passed
#
# Exit codes :
#   0  success (even if nothing was loaded)
#   2  argparse error

set -euo pipefail

SCOPE="all"
KEEP_LOGS=0

usage() {
    cat >&2 <<USAGE
Usage : bash scripts/uninstall-ship-launchd.sh [--scope status|daily|doctor|all] [--keep-logs]
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scope)     SCOPE="$2"; shift 2 ;;
        --keep-logs) KEEP_LOGS=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "Erreur : unknown flag $1" >&2; usage; exit 2 ;;
    esac
done

case "$SCOPE" in
    status|daily|doctor|all) ;;
    *) echo "Erreur : --scope must be status | daily | doctor | all (got '$SCOPE')" >&2; exit 2 ;;
esac

AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"

remove_agent() {
    local name="$1"
    local label="com.mr1000xgrowth.ship1000x.$name"
    local plist="$AGENT_DIR/$label.plist"

    if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
        launchctl bootout "gui/$UID/$label" 2>/dev/null || true
        echo "[uninstall] booted out $label"
    else
        echo "[uninstall] $label was not loaded"
    fi

    if [[ -f "$plist" ]]; then
        rm -f "$plist"
        echo "[uninstall] removed $plist"
    else
        echo "[uninstall] $plist did not exist"
    fi

    if [[ $KEEP_LOGS -eq 0 ]]; then
        rm -f "$LOG_DIR/ship1000x-$name.log" "$LOG_DIR/ship1000x-$name.err.log"
        echo "[uninstall] removed logs for $name"
    fi
}

case "$SCOPE" in
    status|daily|doctor) remove_agent "$SCOPE" ;;
    all)
        remove_agent status
        remove_agent daily
        remove_agent doctor
        ;;
esac

echo ""
echo "Done."
