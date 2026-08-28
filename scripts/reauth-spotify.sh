#!/usr/bin/env bash
#
# Re-authorise Spotify after the scope list changes.
#
# Spotipy caches the token with the scopes it was granted. Widening SCOPES makes
# that cache insufficient, and the next start needs an interactive browser round
# trip - which a backgrounded bot cannot do, because there is nothing to click.
# It hangs instead of failing.
#
# Run this by hand, with the bot stopped.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if [[ -n "$(find_pids 'python.*bot\.py')" ]]; then
    echo "Athena is still running (pid $(find_pids 'python.*bot\.py' | tr '\n' ' '))."
    echo "Stop it first:  scripts/stop-athena.sh"
    exit 1
fi

PYTHON="$(pick_python)"
cd "$ROOT"
exec "$PYTHON" "$(dirname "${BASH_SOURCE[0]}")/reauth_spotify.py"
