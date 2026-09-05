#!/usr/bin/env bash
#
# The VOICES service: a second speech server, pinned to a cloning checkpoint.
#
# Exists because Athena's own voice and the saved voices are usually different
# engines — she may speak with Kokoro while the library is Qwen clones, and one
# process cannot serve both. This one answers /tts <voice> <phrase> and backs
# the voice lab, so testing a voice and using it in Discord are the same code
# path rather than two approximations of each other.
#
# Its reference clip is per request, so this single process speaks as ANY saved
# recording with no reload. The library therefore costs nothing to grow.
#
# Deliberately separate from com.athena.tts: bouncing this to try a different
# checkpoint must never interrupt her.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PYTHON="$ROOT/tts/.venv-mlx/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "FATAL: no MLX venv at $PYTHON" >&2
    echo "  /opt/homebrew/bin/python3.12 -m venv tts/.venv-mlx" >&2
    echo "  tts/.venv-mlx/bin/python -m pip install mlx-audio soundfile" >&2
    exit 1
fi

PORT="$(env_value VOICES_URL http://127.0.0.1:8087 | sed 's|.*:||')"
MODEL="$(env_value VOICES_MODEL mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit)"
LOG="$(new_log voices)"
cd "$ROOT/tts"
echo "$(date +%FT%T)  ---- starting voices ($MODEL on :$PORT) ----" >> "$LOG"
exec "$PYTHON" tts_server.py --engine qwen --qwen-model "$MODEL" \
     --host 127.0.0.1 --port "$PORT" --preload >> "$LOG" 2>&1
