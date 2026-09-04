#!/usr/bin/env bash
#
# Speak a line through the AUDITION server and play it here, not in Discord.
#
#   scripts/say.sh "A low, dry, aristocratic English woman. Bored."   # design
#   scripts/say.sh --voice Serena                                     # timbre
#   scripts/say.sh                                                    # as-is
#   scripts/say.sh --text "Something else entirely" --voice Ryan
#
# Plays through afplay on the built-in output. It never touches BlackHole, so
# it cannot end up in the stream or the voice channel — which is the whole
# point of auditioning on 8090 rather than restarting the live service.
set -uo pipefail
PORT=8090
TEXT="Playing The Emperor's New Groove. Do try to keep up."
VOICE=""; INSTRUCT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --voice) VOICE="$2"; shift 2 ;;
        --text)  TEXT="$2";  shift 2 ;;
        *)       INSTRUCT="$1"; shift ;;
    esac
done

curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || {
    echo "no audition server on $PORT - start one:" >&2
    echo "  scripts/tts-audition.sh design|voices|clone" >&2
    exit 1
}

OUT="$(mktemp -t athena-say).wav"
BODY=$(TEXT="$TEXT" VOICE="$VOICE" INSTRUCT="$INSTRUCT" python3 -c '
import json, os
print(json.dumps({k.lower(): os.environ[k] for k in ("TEXT", "VOICE", "INSTRUCT")}))')

START=$(python3 -c 'import time; print(time.time())')
if ! curl -fsS --max-time 180 -X POST "http://127.0.0.1:$PORT/synthesize" \
        -H 'Content-Type: application/json' -d "$BODY" -o "$OUT"; then
    echo "synthesis failed" >&2; exit 1
fi
python3 -c "
import sys, time, wave
w = wave.open('$OUT')
print('  %.2fs to make %.2fs of audio' % (time.time() - $START,
                                          w.getnframes() / w.getframerate()))"
afplay "$OUT"
echo "  $OUT"
