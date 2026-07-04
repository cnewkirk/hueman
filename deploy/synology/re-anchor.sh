#!/bin/sh
# Daily sun re-anchor for the live circadian + night-motion config.
#
# Runs `hue-iac apply` once a day so the natively-delivered circadian timeslots
# re-anchor to the real sun. DST is handled inside the tool via `location.tz`
# in hue.yaml, so this wrapper carries no timezone logic.
#
# Layout (HUE_IAC_HOME = parent of this script's bin/ directory):
#   $HUE_IAC_HOME/{hue.yaml,.hue-key,.hue-pin.json,.hue-backup/,.venv/,src/,bin/,logs/}
#
# Invoked by DSM Task Scheduler:  sh $HUE_IAC_HOME/bin/re-anchor.sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HUE_IAC_HOME=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$HUE_IAC_HOME"

LOG_DIR="$HUE_IAC_HOME/logs"
LOG="$LOG_DIR/re-anchor.log"
mkdir -p "$LOG_DIR"

# Cap the log at ~1 MB so it can't grow unbounded on the NAS.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ]; then
    mv "$LOG" "$LOG.1"
fi

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

if [ ! -r "$HUE_IAC_HOME/.hue-key" ]; then
    echo "[$(ts)] re-anchor ABORT: .hue-key missing or unreadable" >> "$LOG"
    exit 2
fi
HUE_APPLICATION_KEY=$(cat "$HUE_IAC_HOME/.hue-key")
export HUE_APPLICATION_KEY

echo "[$(ts)] re-anchor start" >> "$LOG"
if "$HUE_IAC_HOME/.venv/bin/hue-iac" -c "$HUE_IAC_HOME/hue.yaml" apply --yes >> "$LOG" 2>&1; then
    echo "[$(ts)] re-anchor OK" >> "$LOG"
else
    rc=$?
    echo "[$(ts)] re-anchor FAILED rc=$rc" >> "$LOG"
    exit "$rc"
fi
