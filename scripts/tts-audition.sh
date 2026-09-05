#!/usr/bin/env bash
#
# A SECOND speech server, for trying voices while she stays live in Discord.
#
#   scripts/tts-audition.sh design      describe a voice in words
#   scripts/tts-audition.sh voices      the 9 built-in timbres
#   scripts/tts-audition.sh clone       clone tts/voices/*.wav
#   scripts/tts-audition.sh stop
#
# Runs on 8090, NOT the 8085 the bot talks to, so nothing you do here can reach
# the Discord channel: this process is never given an output device, and
# /synthesize returns bytes rather than playing them. Auditioning is safe
# during a call.
#
# COSTS ~6.3GB of unified memory on top of whatever is already running. With
# Ollama's 7.5GB and a live Qwen service that is ~27GB of 32GB — fine for a
# while, but stop it when you are done rather than leaving it up.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# EXTRA is expanded as ${EXTRA[@]+"${EXTRA[@]}"} rather than "${EXTRA[@]}".
# macOS ships bash 3.2, where an empty array under `set -u` is an UNBOUND
# VARIABLE error, not an empty expansion — exactly the trap _common.sh warns
# about. An array is still right here because --voice-ref is a path and
# --ref-text is a whole sentence, neither of which survives word splitting.

PORT=8090
MODE="${1:-design}"
PIDFILE="/tmp/athena-audition.pid"

if [[ "$MODE" == "stop" ]]; then
    [[ -f "$PIDFILE" ]] && kill "$(cat "$PIDFILE")" 2>/dev/null && echo "stopped"
    rm -f "$PIDFILE"; exit 0
fi

case "$MODE" in
    design) MODEL="mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit"; EXTRA=() ;;
    voices) MODEL="mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"; EXTRA=() ;;
    clone)  MODEL="mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
            REF="$ROOT/$(env_value TTS_VOICE_REF tts/voices/athena.wav)"
            EXTRA=(--voice-ref "$REF" --ref-text "$(env_value TTS_VOICE_REF_TEXT '')") ;;
    *) echo "unknown mode: $MODE (design|voices|clone|stop)" >&2; exit 1 ;;
esac

PYTHON="$ROOT/tts/.venv-mlx/bin/python"
[[ -x "$PYTHON" ]] || { echo "FATAL: no MLX venv. python3.12 -m venv tts/.venv-mlx && tts/.venv-mlx/bin/python -m pip install mlx-audio" >&2; exit 1; }

cd "$ROOT/tts"
LOG="$(new_log audition)"
nohup "$PYTHON" tts_server.py --engine qwen --qwen-model "$MODEL" \
      --host 127.0.0.1 --port "$PORT" --preload ${EXTRA[@]+"${EXTRA[@]}"} >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "audition server starting (pid $(cat "$PIDFILE")) - $MODE - log: $LOG"

deadline=$(( $(date +%s) + 120 ))
while [[ $(date +%s) -lt $deadline ]]; do
    sleep 2
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ready": true'; then
        echo "ready on http://127.0.0.1:$PORT"
        echo
        case "$MODE" in
          design) cat <<TIP
Try a voice:
  scripts/say.sh 'A low, dry, aristocratic English woman. Bored.'
TIP
;;
          voices) echo "Timbres:"; curl -s "http://127.0.0.1:$PORT/voices" \
                  | "$ROOT/.venv/bin/python" -c "import json,sys
# Keys are id/note/lang_name, NOT name/description: /voices deliberately
# returns the same shape for every engine so the control panel's picker works
# unchanged, and this script reads that same endpoint. It used to say
# v['name'] and died with KeyError the moment the payload was unified.
for v in json.load(sys.stdin).get('voices', []):
    print('  %-10s %-9s %s' % (v['id'], v.get('lang_name',''), v.get('note','')))"
                  echo; echo "Try one:"; echo "  scripts/say.sh --voice Serena" ;;
          clone)  echo "Try it:"; echo "  scripts/say.sh" ;;
        esac
        exit 0
    fi
done
echo "did not come up in 120s - check $LOG" >&2
exit 1
