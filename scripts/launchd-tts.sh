#!/usr/bin/env bash
#
# Foreground TTS launcher for launchd (com.athena.tts).
#
# start-tts.sh backgrounds the server with nohup+& and exits once it answers
# /health, which is right for an interactive terminal but wrong under launchd:
# KeepAlive supervises whatever process launchd itself holds a handle to, and
# a script that exits immediately gives it nothing to supervise. This execs
# tts_server.py in the foreground instead, so a crash is visible to launchd
# and KeepAlive actually restarts it.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

TTS_DIR="$ROOT/tts"
PYTHON="$(tts_python)"
# Unquoted on purpose: this is a flag list, not one argument.
ENGINE_ARGS="$(tts_args)"

LOG="$(new_log tts)"
cd "$TTS_DIR"
echo "$(date +%FT%T)  ---- starting TTS (launchd, $PYTHON) $ENGINE_ARGS ----" >> "$LOG"
exec "$PYTHON" tts_server.py --preload --host 127.0.0.1 $ENGINE_ARGS >> "$LOG" 2>&1
