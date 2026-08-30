#!/usr/bin/env bash
#
# Install (or remove) the LaunchAgents that start Athena at login.
#
#   scripts/install-launchagents.sh            install and start
#   scripts/install-launchagents.sh --uninstall stop and remove
#
# Generates the plists rather than shipping them, because every path in them is
# absolute and specific to this checkout — a committed plist would be wrong for
# everyone but its author. This is why it exists at all: the plists on the
# development machine were written by hand, which meant "start on login" was a
# documented feature nobody else could actually perform.
#
# LaunchAgent, never LaunchDaemon. The bot drives mpv and Spotify windows
# through the Accessibility API, and none of that exists outside a logged-in
# GUI session. A daemon runs before login and would fail every window call.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"

plist_for() {
    # $1 label, $2 script name, $3 log prefix
    #
    # The log prefix is passed rather than derived from the label. Deriving it
    # gave "bot-launchd-stderr.log" for com.athena.bot, and the control panel's
    # Supervisor log looks for "athena-launchd-stderr.log" - so the panel would
    # have shown an empty log for a service that was writing one.
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$1</string>

    <key>ProgramArguments</key>
    <array>
        <string>$ROOT/scripts/$2</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <!-- Do not respawn faster than this after a crash. Without it a bot that
         fails at startup - a missing dependency, say - spins in a tight loop. -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>$ROOT/logs/$3-launchd-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>$ROOT/logs/$3-launchd-stderr.log</string>
</dict>
</plist>
PLIST
}

# Unload, and WAIT for it to actually happen.
#
# bootout is asynchronous. Bootstrapping a label that launchd is still tearing
# down fails with "Bootstrap failed: 5: Input/output error" - which is what
# this script did on its first run, leaving both services stopped and neither
# agent loaded. Polling until the label is gone is the fix; the sleep is not
# decoration.
unload() {
    for label in com.athena.bot com.athena.tts com.athena.gui com.athena.name com.athena.streamaudio; do
        launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
    done
    for label in com.athena.bot com.athena.tts com.athena.gui com.athena.name com.athena.streamaudio; do
        i=0
        while launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; do
            sleep 0.5
            i=$((i + 1))
            if [ $i -ge 40 ]; then
                echo "warning: $label is still loaded after 20s" >&2
                break
            fi
        done
    done
}

if [[ "${1:-}" == "--uninstall" ]]; then
    unload
    rm -f "$AGENTS/com.athena.bot.plist" "$AGENTS/com.athena.tts.plist" \
          "$AGENTS/com.athena.gui.plist" "$AGENTS/com.athena.name.plist" \
          "$AGENTS/com.athena.streamaudio.plist"
    echo "Removed. Athena will not start at login."
    exit 0
fi

mkdir -p "$AGENTS" "$LOGDIR"

# Booted out first: bootstrap refuses a label that is already loaded, and
# rewriting a plist under a running job leaves launchd using the old one.
unload

plist_for com.athena.tts    launchd-tts.sh    tts    > "$AGENTS/com.athena.tts.plist"
plist_for com.athena.bot    launchd-athena.sh athena > "$AGENTS/com.athena.bot.plist"
# The control panel too. Without this it survives exactly until the next
# reboot, which is precisely when someone needs it most: the panel is how you
# start everything else, and needing physical access to start the thing that
# gives you remote access defeats the point of it.
plist_for com.athena.gui    start-gui.sh      gui    > "$AGENTS/com.athena.gui.plist"
# Publishes <PANEL_HOSTNAME>.local so the panel has a URL rather than an IP.
# Supervised because dns-sd holds the registration only while it runs.
plist_for com.athena.name   publish-name.sh   name   > "$AGENTS/com.athena.name.plist"
echo "Wrote:"
echo "  $AGENTS/com.athena.tts.plist"
echo "  $AGENTS/com.athena.bot.plist"
echo "  $AGENTS/com.athena.gui.plist"
echo "  $AGENTS/com.athena.name.plist"

# Stream audio routing, only when it is actually turned on. It is a one-shot
# at login rather than a supervised service: it makes the device, selects it,
# and exits. KeepAlive would respawn it forever for nothing, and the script
# already waits for the virtual driver itself rather than needing a retry.
if [ "$(env_value STREAM_AUDIO_ENABLED false)" = "true" ]; then
    cat > "$AGENTS/com.athena.streamaudio.plist" <<STREAMPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.athena.streamaudio</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(pick_python)</string>
        <string>$ROOT/scripts/setup-stream-audio.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$ROOT/logs/streamaudio.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/streamaudio.log</string>
</dict>
</plist>
STREAMPLIST
    echo "  $AGENTS/com.athena.streamaudio.plist"
fi

LABELS="com.athena.tts com.athena.bot com.athena.gui com.athena.name"
[ -f "$AGENTS/com.athena.streamaudio.plist" ] && LABELS="$LABELS com.athena.streamaudio"
for label in $LABELS; do
    i=0
    until launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$label.plist" 2>/dev/null; do
        i=$((i + 1))
        if [ $i -ge 10 ]; then
            echo "FAILED to load $label. Try again in a moment:" >&2
            echo "  launchctl bootstrap gui/$UID_NUM $AGENTS/$label.plist" >&2
            exit 1
        fi
        sleep 1
    done
    echo "Loaded $label"
done

echo
echo "Both will now start at login and restart if they crash."
echo "Use the control panel's Start and Stop from here on - KeepAlive will undo"
echo "anything else within ten seconds."
