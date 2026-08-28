#!/usr/bin/env bash
#
# Stop everything: the bot first, then the TTS service.
#
# Order matters here too, the other way round. Stopping TTS first would leave
# the bot briefly trying to speak into a service that has gone, which logs a
# warning for every reply until it is stopped as well.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Athena ==="
"$HERE/stop-athena.sh"
echo "=== TTS ==="
"$HERE/stop-tts.sh"
