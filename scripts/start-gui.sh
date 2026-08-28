#!/usr/bin/env bash
#
# Start the control panel.
#
#   scripts/start-gui.sh [--port N]
#
# Runs in the foreground, unlike start-athena.sh: this is something you open,
# use and close, not something that should be supervised. Ctrl-C stops it.
#
# Deliberately does NOT require the bot's virtualenv. The panel is what you
# reach for when the bot will not start, and a broken venv is one of the
# commonest reasons for that — so it runs on whatever python3 is available and
# imports nothing outside the standard library. pick_python is still consulted
# first, because if the venv IS healthy it is the interpreter most likely to
# match the rest of the project.
#
# Python 3.9 is enough, which is deliberate and slightly hard-won: 3.9 is what
# macOS ships, so the panel opens on a machine where nothing has been installed
# yet. That is why schema.py carries `from __future__ import annotations` — its
# `float | None` would otherwise fail to import on exactly the interpreter this
# most needs to work on. Do not raise this floor without a reason.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PYTHON="$(pick_python)"
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    have="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown)"
    echo "FATAL: the control panel needs Python 3.9 or newer; $PYTHON is $have." >&2
    echo "    brew install python@3.13" >&2
    exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m gui.server "$@"
