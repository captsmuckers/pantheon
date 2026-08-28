#!/usr/bin/env bash
#
# Stop the text-to-speech service.
#
# Matched on tts_server.py specifically, so this never touches the bot.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

pids="$(find_pids 'python.*tts_server\.py')"
if [ -z "$pids" ]; then
    echo "nothing running"
    exit 0
fi
stop_pids "tts" "$pids"
