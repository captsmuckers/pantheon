#!/usr/bin/env bash
#
# Start the text-to-speech service and wait until it answers.
#
#   scripts/start-tts.sh [bind-host]
#
# 127.0.0.1 keeps it on this machine. Pass 0.0.0.0 to read the log page from
# another computer - a firewall rule alone will NOT do it, because loopback
# means nothing is listening on the network interface to allow.
#
# Understand what that opens: /synthesize takes text and speaks it into the
# Discord channel with no authentication. Only do it on a LAN you trust.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

BIND_HOST="${1:-127.0.0.1}"
TTS_DIR="$ROOT/tts"

# Its own venv, on its own interpreter. Not shareable with the bot's: kokoro
# declares Requires-Python <3.13 and the bot runs 3.13, so this needs 3.12
# whatever else changes. Falling back to the bot's Python is deliberate anyway
# — it gives the dependency check below something to fail on with a useful
# message, rather than this script dying on a missing path.
if [[ -x "$TTS_DIR/.venv/bin/python" ]]; then
    PYTHON="$TTS_DIR/.venv/bin/python"
else
    PYTHON="$(pick_python)"
fi

if [[ -n "$(find_pids 'python.*tts_server\.py')" ]]; then
    echo "TTS already running (pid $(find_pids 'python.*tts_server\.py' | tr '\n' ' '))"
    exit 0
fi

if ! "$PYTHON" -c "import torch, kokoro" 2>/dev/null; then
    echo "FATAL: TTS dependencies missing from $PYTHON" >&2
    echo "  brew install python@3.12" >&2
    echo "  /opt/homebrew/bin/python3.12 -m venv tts/.venv" >&2
    echo "  tts/.venv/bin/python -m pip install -r tts/requirements.txt" >&2
    echo "(3.12, not 3.13 - kokoro declares Requires-Python <3.13)" >&2
    exit 1
fi

LOG="$(new_log tts)"
cd "$TTS_DIR"
echo "$(date +%FT%T)  ---- starting TTS ($PYTHON) ----" >> "$LOG"
# --preload loads the model now rather than on the first request, so the first
# thing she says isn't delayed by several seconds of model load.
nohup "$PYTHON" tts_server.py --preload --host "$BIND_HOST" >> "$LOG" 2>&1 &
echo "TTS starting (pid $!) - log: $LOG"

deadline=$(( $(date +%s) + 90 ))
while [[ $(date +%s) -lt $deadline ]]; do
    sleep 2
    if health=$(curl -fsS --max-time 3 http://127.0.0.1:8085/health 2>/dev/null); then
        if [[ "$health" == *'"ready": true'* || "$health" == *'"ready":true'* ]]; then
            echo "TTS ready - $health"
            if [[ "$BIND_HOST" == "0.0.0.0" ]]; then
                # Prefer a real adapter. The Windows version had to filter out
                # Hyper-V and WSL virtual ones; here route(8) just names the
                # interface that actually carries traffic.
                iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
                ip=$(ipconfig getifaddr "${iface:-en0}" 2>/dev/null || true)
                echo "Logs in a browser: http://${ip:-<this-machine>}:8085/logs  (also /health)"
            else
                echo "Logs in a browser: http://127.0.0.1:8085/logs"
                echo "  (this machine only - rerun as 'start-tts.sh 0.0.0.0' to reach it from another computer)"
            fi
            exit 0
        fi
    fi
done
echo "TTS did not become ready within 90s - check $LOG" >&2
exit 1
