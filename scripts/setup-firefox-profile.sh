#!/usr/bin/env bash
#
# Prepare the Firefox profile the age-gate fallback uses.
#
#   scripts/setup-firefox-profile.sh          create and configure
#   scripts/setup-firefox-profile.sh --open   ...then open it for signing in
#
# Creates the profile if it does not exist and writes a user.js into it. Only
# the YouTube sign-in is left to a person, because only a person can do it.
#
# WHY user.js AT ALL. browser.py's docstring has always said the profile "needs
# autoplay allowed by hand ... see the user.js note in the README" - and that
# note was never written. This is it, as a script rather than a paragraph,
# because every pref below is a thing that silently ruins the result:
#
#   autoplay          A fresh profile has no interaction history with
#                     youtube.com, and Firefox blocks autoplay without one. The
#                     video opens and simply sits there.
#   onboarding        First-run Firefox shows a welcome tour and a data notice
#                     ON TOP of the page. In kiosk mode there is no chrome to
#                     dismiss them from.
#   default-browser   The "make Firefox your default" prompt does the same.
#   fullscreen warn   The "you are now in full screen" overlay sits over the
#                     video for several seconds.
#
# None of these matter in ordinary browsing, which is exactly why a profile
# that works fine by hand can look broken when the bot drives it.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PROFILE="$(env_value BROWSER_PROFILE Athena)"
FF="$(env_value BROWSER_PATH "")"
if [ -z "$FF" ]; then
    for c in /Applications/Firefox.app/Contents/MacOS/firefox \
             "$HOME/Applications/Firefox.app/Contents/MacOS/firefox" \
             "$(command -v firefox 2>/dev/null || true)"; do
        [ -n "$c" ] && [ -x "$c" ] && { FF="$c"; break; }
    done
fi
if [ -z "$FF" ]; then
    echo "FATAL: Firefox not found. Install it first:" >&2
    echo "  brew install --cask firefox" >&2
    exit 1
fi

INI="$HOME/Library/Application Support/Firefox/profiles.ini"
if ! grep -q "^Name=$PROFILE$" "$INI" 2>/dev/null; then
    echo "Creating profile '$PROFILE'..."
    "$FF" -CreateProfile "$PROFILE" >/dev/null 2>&1
    sleep 2
fi

# Resolve the profile directory from profiles.ini rather than globbing for
# "*.$PROFILE": the directory prefix is random, and a profile created by hand
# may not follow the naming convention at all.
DIR="$(awk -v want="Name=$PROFILE" '
    /^\[/       { inprof=0 }
    $0 == want  { inprof=1 }
    inprof && /^Path=/ { sub(/^Path=/, ""); print; exit }
' "$INI")"
if [ -z "$DIR" ]; then
    echo "FATAL: created profile '$PROFILE' but cannot find it in $INI" >&2
    exit 1
fi
case "$DIR" in
    /*) PROFILE_DIR="$DIR" ;;
    *)  PROFILE_DIR="$HOME/Library/Application Support/Firefox/$DIR" ;;
esac
echo "Profile: $PROFILE_DIR"

cat > "$PROFILE_DIR/user.js" <<'PREFS'
// Written by scripts/setup-firefox-profile.sh. Safe to delete; rerun to restore.
//
// This profile exists only to play one YouTube page at a time, driven by a bot,
// with no toolbar to click anything away with.

// Play without a click. A fresh profile has no interaction history with
// youtube.com, and Firefox blocks autoplay without one - the video opens and
// then just sits there, which looks exactly like a broken integration.
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);

// Nothing on top of the video. In kiosk mode there is no chrome to dismiss
// any of these from.
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("full-screen-api.warning.timeout", 0);
user_pref("browser.tabs.warnOnClose", false);

// No update interruptions mid-film.
user_pref("app.update.auto", false);
PREFS

echo "Wrote $PROFILE_DIR/user.js"
echo
echo "One step left, and only you can do it:"
echo "  sign in to YouTube in this profile, and play one video by hand."
echo
if [ "${1:-}" = "--open" ]; then
    echo "Opening the profile at YouTube's sign-in page..."
    "$FF" -no-remote -new-instance -P "$PROFILE" \
        "https://accounts.google.com/ServiceLogin?service=youtube" \
        >/dev/null 2>&1 &
    echo "Sign in, then close Firefox. Nothing else is needed."
else
    echo "Run with --open to launch it there, or:"
    echo "  '$FF' -no-remote -new-instance -P '$PROFILE' https://youtube.com"
fi
