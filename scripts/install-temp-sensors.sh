#!/usr/bin/env bash
#
# Install the thermal sampler. MUST be run with sudo:
#
#   sudo ./scripts/install-temp-sensors.sh
#   sudo ./scripts/install-temp-sensors.sh --uninstall
#
# WHY THIS NEEDS ROOT AT ALL, and why the panel still does not have it.
# Apple silicon exposes thermal sensors only to privileged callers: powermetrics
# requires root, and the AppleSMC / AppleARMPMUTempSensor nodes in ioreg carry
# no readable values otherwise. The control panel runs as an ordinary
# LaunchAgent and should stay that way — granting a web server root so it can
# draw a temperature gauge would be a bad trade.
#
# So the split is: a tiny root LaunchDaemon samples powermetrics on an interval
# and writes one small JSON file, world-readable. The panel reads that file and
# gains nothing. The privileged part does one thing and has no network, no
# input, and no arguments.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This has to run as root:" >&2
    echo "  sudo $0" >&2
    exit 1
fi

# The invoking user, not root — the script lives in their checkout and writes
# into their logs directory.
REAL_USER="${SUDO_USER:-$(stat -f '%Su' /dev/console)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="/Library/LaunchDaemons/com.athena.tempsensors.plist"
LABEL="com.athena.tempsensors"

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout system/"$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed. Temperatures will read as unavailable again."
    exit 0
fi

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$ROOT/scripts/temp-sampler.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/tempsensors.log</string>
</dict>
</plist>
PLISTEOF

chown root:wheel "$PLIST"
chmod 644 "$PLIST"

launchctl bootout system/"$LABEL" 2>/dev/null || true
i=0
while launchctl print system/"$LABEL" >/dev/null 2>&1; do
    sleep 0.5; i=$((i+1)); [ $i -ge 20 ] && break
done
launchctl bootstrap system "$PLIST"

echo "Installed $LABEL."
echo "Sampling every 20s into $ROOT/logs/temperatures.json"
echo
echo "Give it a moment, then the control panel's Status page will show them."
