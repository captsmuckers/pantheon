#!/usr/bin/env bash
#
# Start everything: the TTS service, then the bot.
#
# Order matters. start-tts.sh waits until the service answers /health, so the
# bot comes up with speech already available - otherwise the first spoken reply
# after a restart hits a service still loading its model and is silently
# dropped (the text reply still posts, so it looks like speech is broken rather
# than warming up).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== TTS ==="
if ! "$HERE/start-tts.sh" "${1:-127.0.0.1}"; then
    echo
    echo "TTS did not start. Continuing anyway - the bot runs fine"
    echo "without speech, it just won't talk."
fi

echo
echo "=== Athena ==="
exec "$HERE/start-athena.sh"
