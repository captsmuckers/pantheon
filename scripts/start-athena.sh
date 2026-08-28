#!/usr/bin/env bash
#
# Start Athena in the background, logging to its own file.
#
# Runs in the logged-in GUI session, and must keep doing so. The bot drives mpv
# and Spotify windows through the Accessibility API, none of which exists
# outside a real login session. That was the same constraint on Windows, where
# it meant "never convert this to a run-whether-logged-on-or-not task"; here it
# means run it from your own terminal, or as a LaunchAgent (~/Library/
# LaunchAgents), never as a LaunchDaemon.
#
# No -X utf8 and no PYTHONIOENCODING, both of which the Windows launcher needed:
# macOS is UTF-8 end to end, so the em-dashes in the bot's own strings arrive
# intact without being asked to.
#
# WRAPPED IN caffeinate, which the Windows launcher had no need of.
#
# This was originally load-bearing on its own: the machine is a laptop and
# `pmset -g custom` reported `sleep 1` on AC as well as battery, so the bot
# died a minute after whoever started it stopped typing. Sleep has since been
# disabled machine-wide (`pmset -a disablesleep 1`, and `pmset -g` now shows
# SleepDisabled 1), so that half is now belt and braces.
#
# The -d half still earns its place regardless:
#
#   -i  no idle sleep      -s  no system sleep (AC only)
#   -d  no display sleep — needed on its own account, not for the bot's sake:
#       the whole point is a screen being shared, and a blanked display shares
#       a blank screen.
#
# Keeping all three rather than trimming to -d, because the assertion lives
# exactly as long as the python it wraps and costs nothing — whereas a machine
# restored to its default power settings some day would silently take the bot
# down again, and this is the line that would have prevented it.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if [[ -n "$(find_pids 'python.*bot\.py')" ]]; then
    echo "Athena is already running (pid $(find_pids 'python.*bot\.py' | tr '\n' ' '))"
    exit 0
fi

PYTHON="$(pick_python)"
if [[ ! -x "$PYTHON" ]]; then
    echo "FATAL: no usable Python found. Create the venv:" >&2
    echo "  python3.13 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
fi

LOG="$(new_log athena)"
cd "$ROOT"
echo "$(date +%FT%T)  ---- starting Athena ($PYTHON) ----" >> "$LOG"
nohup caffeinate -dis "$PYTHON" bot.py >> "$LOG" 2>&1 &
echo "Athena starting (pid $!) - log: $LOG"
