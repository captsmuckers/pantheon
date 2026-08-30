#!/usr/bin/env bash
#
# Publish this machine on the LAN as <name>.local, so the panel has a URL
# instead of an IP address.
#
#   scripts/publish-name.sh          # uses PANEL_HOSTNAME from .env
#
# WHY BONJOUR RATHER THAN DNS. The alternatives all cost more:
#
#   A public DNS record works, but it puts the name in the global DNS and
#   routes LAN traffic out to the public IP and back. Wrong trade for something
#   deliberately not exposed to the internet.
#
#   A router DNS entry works and keeps traffic local, but it is per-router,
#   needs admin access, and cannot be set up from here.
#
#   /etc/hosts works and has to be repeated on every phone and laptop.
#
# Bonjour is already running on every Apple device and most others, needs no
# configuration anywhere else, and this machine can publish it for itself.
#
# Runs in the FOREGROUND on purpose: dns-sd holds the registration for as long
# as it lives, and the record vanishes when it exits. That is what makes it
# safe to supervise — launchd restarting it re-publishes the name.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

NAME="$(env_value PANEL_HOSTNAME pantheon)"
PORT="$(env_value PANEL_PORT 8086)"

# The address is resolved at start rather than hardcoded: DHCP can hand this
# machine a different one, and publishing a stale address is worse than not
# publishing at all — it resolves, and then nothing answers.
IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
IP="$(ipconfig getifaddr "${IFACE:-en0}" 2>/dev/null || true)"
if [ -z "$IP" ]; then
    echo "FATAL: no IPv4 address on ${IFACE:-en0}; nothing to publish." >&2
    exit 1
fi

echo "Publishing $NAME.local -> $IP (port $PORT) on $IFACE"
echo "Reach the panel at http://$NAME.local:$PORT"
exec dns-sd -P "$NAME" _http._tcp local "$PORT" "$NAME.local" "$IP"
