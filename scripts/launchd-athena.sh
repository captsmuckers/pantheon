#!/usr/bin/env bash
#
# Foreground Athena launcher for launchd (com.athena.bot).
#
# Same reasoning as launchd-tts.sh: execs bot.py in the foreground so launchd
# can see it exit/crash and KeepAlive can restart it, instead of backgrounding
# it the way start-athena.sh does for interactive use.
#
# Still waits on the TTS /health endpoint first, same as start-all.sh, so the
# first spoken reply after a restart doesn't land while Kokoro is still
# loading and get silently dropped. com.athena.tts is a separate LaunchAgent
# with its own RunAtLoad, so it's typically already coming up in parallel by
# the time this runs.
#
# Still wrapped in caffeinate -dis: system sleep is now disabled machine-wide
# (pmset -a disablesleep 1), so this is redundant for sleep itself, but it
# also blocks *display* sleep, which start-athena.sh's comment notes matters
# on its own account when the screen is being shared (e.g. via Parsec).
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PYTHON="$(pick_python)"
LOG="$(new_log athena)"

deadline=$(( $(date +%s) + 90 ))
while [[ $(date +%s) -lt $deadline ]]; do
    if health=$(curl -fsS --max-time 3 http://127.0.0.1:8085/health 2>/dev/null) \
        && [[ "$health" == *'"ready": true'* || "$health" == *'"ready":true'* ]]; then
        break
    fi
    sleep 2
done

cd "$ROOT"
echo "$(date +%FT%T)  ---- starting Athena (launchd, $PYTHON) ----" >> "$LOG"
exec caffeinate -dis "$PYTHON" bot.py >> "$LOG" 2>&1
