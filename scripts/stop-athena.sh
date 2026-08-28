#!/usr/bin/env bash
#
# Stop Athena properly.
#
# ORDER MATTERS, exactly as it did on Windows. Killing mpv first makes the
# player's watchdog immediately relaunch it - observed live, producing an
# orphaned athena-mpv-<pid> seconds after the stop. Python dies first so
# nothing is left to resurrect anything.
#
# The mpv sweep matches the IPC socket name (athena-mpv-*), which the bot sets,
# so an mpv the user opened themselves is left alone. It deliberately does NOT
# require the owning python to still exist, or orphans from an earlier crash
# would linger. Both the athena- and nyx- prefixes are matched, because an mpv
# started before the rename is still an orphan that has to be cleaned up.
#
# Gone from the Windows version: the cmd.exe parent hunt. That existed because
# the launcher had to wrap python in cmd to redirect stderr, and killing the
# wrapper orphaned the chain below it. Nothing wraps python here.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

killed=0

pythons="$(find_pids 'python.*bot\.py')"
if [ -n "$pythons" ]; then
    stop_pids "athena" "$pythons" && killed=1
    # Give the watchdog no chance to have already relaunched.
    sleep 1.5
fi

mpvs="$(find_pids 'mpv.*(athena|nyx)-mpv-')"
if [ -n "$mpvs" ]; then
    stop_pids "mpv" "$mpvs" && killed=1
fi

[ $killed -eq 1 ] || echo "nothing running"
