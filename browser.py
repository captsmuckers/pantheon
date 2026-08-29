"""Browser fallback for YouTube links yt-dlp can't get past the age gate.

Loads the real youtube.com page in a signed-in Firefox instead of asking
yt-dlp to extract a stream URL — sidestepping the "authenticated client gets
zero usable formats" wall entirely (see config.py's YTDL_COOKIES_FROM_BROWSER
comment), because nothing here extracts a format at all. YouTube's own
player handles the age gate the same way it would for a person.

Deliberately the simple version: this opens a window, nothing more. There is
no playback control from Discord once it's up — no pause, seek, or stop.
That trade was made on purpose; a scripted version (Playwright driving the
same profile, with real control) is the fallback if this isn't enough.

Uses a dedicated Firefox profile (BROWSER_PROFILE) rather than whichever
profile happens to be the default, so the bot's YouTube login is its own.
That profile also needs autoplay allowed by hand (a fresh profile has no
interaction history with youtube.com, and Firefox silently blocks autoplay
without one) — see the user.js note in the README/setup notes.
"""

import logging
import os
import shutil
import subprocess
import sys
import time

import config
import wm

log = logging.getLogger("athena.browser")

# A fresh Firefox profile is a cold start; give the window time to appear
# before giving up on focusing it.
_WINDOW_WAIT_ATTEMPTS = 20
_WINDOW_WAIT_INTERVAL = 0.5

# -kiosk fullscreens the browser chrome, not the video: the page still lays
# out as an ordinary youtube.com/watch page (player, description, comments),
# just with no toolbar around it. Filling the screen with the video itself
# needs YouTube's own player fullscreen, which is a keypress ('f'), not a
# window state — and the player has to have actually finished initialising
# before that keypress means anything, which the window's mere existence
# doesn't guarantee.
_PLAYER_READY_DELAY = 2.5

# Firefox doesn't put itself on PATH the way most CLI tools do, so
# shutil.which alone misses a completely ordinary install. Checked in the
# order the platform itself would resolve the app.
#
# The macOS entries point at the real binary inside the bundle rather than the
# .app directory, because everything below needs to pass Firefox its own
# command-line flags. `open -a Firefox` would launch it but swallow -P,
# -no-remote and -kiosk, which are the entire point of this module.
if sys.platform == "darwin":
    _COMMON_INSTALL_PATHS = (
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        os.path.expanduser("~/Applications/Firefox.app/Contents/MacOS/firefox"),
    )
else:
    _COMMON_INSTALL_PATHS = (
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    )

# What the window manager calls the process. wm strips a trailing .exe on
# macOS, so one name covers both platforms.
_PROCESS = "firefox.exe"


def _executable() -> str | None:
    if config.BROWSER_PATH:
        return config.BROWSER_PATH
    found = shutil.which("firefox") or shutil.which("firefox.exe")
    if found:
        return found
    for path in _COMMON_INSTALL_PATHS:
        if os.path.exists(path):
            return path
    return None


def _profile_dir() -> str | None:
    """Where BROWSER_PROFILE actually lives, per profiles.ini.

    Read from the ini rather than guessed by globbing for "*.<name>": the
    directory prefix is random, and a profile someone made by hand need not
    follow that convention at all.
    """
    ini = os.path.expanduser(
        "~/Library/Application Support/Firefox/profiles.ini"
        if sys.platform == "darwin"
        else "~/AppData/Roaming/Mozilla/Firefox/profiles.ini")
    if not os.path.exists(ini):
        return None
    want, path, in_profile = f"Name={config.BROWSER_PROFILE}", None, False
    try:
        for line in open(ini, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("["):
                in_profile = False
            elif line == want:
                in_profile = True
            elif in_profile and line.startswith("Path="):
                path = line[5:]
                break
    except OSError:
        return None
    if not path:
        return None
    if os.path.isabs(path):
        return path
    return os.path.expanduser(
        os.path.join("~/Library/Application Support/Firefox", path)
        if sys.platform == "darwin"
        else os.path.join("~/AppData/Roaming/Mozilla/Firefox", path))


def _clear_stale_lock() -> None:
    """Remove a lock left by a Firefox that did not exit cleanly.

    Firefox that is killed - which is exactly how this module's windows get
    closed - leaves .parentlock behind. The next launch then shows a "Close
    Firefox" dialog INSTEAD of the page, and the window-diffing below happily
    returns that dialog as the window to control: open_video reports success
    and hands back a handle to an error box.

    Only done when no Firefox is running at all, so this can never yank the
    lock out from under a live instance.
    """
    if wm.find_windows(_PROCESS):
        return
    profile = _profile_dir()
    if not profile:
        return
    for name in (".parentlock", "lock", "parent.lock"):
        target = os.path.join(profile, name)
        try:
            if os.path.exists(target) or os.path.islink(target):
                os.unlink(target)
                log.info("Cleared a stale Firefox lock: %s", target)
        except OSError as exc:
            log.debug("Could not clear %s: %s", target, exc)


def open_video(url: str) -> tuple[str, int | None]:
    """Open url in the dedicated profile, maximized. Blocking; call via a thread.

    Returns (error, None) on failure, or ("", hwnd) on success — hwnd is the
    window a caller needs to send further keypresses (play/pause) to. A
    launch we couldn't confirm landed a window in time counts as failure
    here even though it may well be fine, unlike the old contract: without a
    hwnd there is nothing to send a play/pause keypress to later.
    """
    exe = _executable()
    if not exe:
        return ("Firefox isn't set up for this — install it, or set "
                "BROWSER_PATH in .env if it's not on PATH."), None

    # -no-remote so a Firefox that's already running (any profile) can't just
    # absorb this as a new window in ITS process and let our spawn exit —
    # without it the launch looks like it did nothing.
    #
    # Even with that, matching by the pid subprocess.Popen hands back doesn't
    # work: Firefox's own process is a short-lived "launcher" that re-execs
    # itself as a new child under a different pid, and the real browser
    # window belongs to that child, not the one we spawned. Measured live —
    # a window opened, fully usable, owned by a pid that was never ours.
    # Diffing the set of Firefox windows before and after the launch instead
    # of chasing a specific pid sidesteps that entirely.
    _clear_stale_lock()
    before = set(wm.find_windows(_PROCESS))

    args = [exe, "-no-remote", "-new-instance", "-P", config.BROWSER_PROFILE]
    if config.BROWSER_KIOSK:
        args.append("-kiosk")
    else:
        args.append("-new-window")
    args.append(url)
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("Could not launch Firefox: %s", exc)
        return "Couldn't launch a browser for that.", None

    hwnd = None
    for _ in range(_WINDOW_WAIT_ATTEMPTS):
        time.sleep(_WINDOW_WAIT_INTERVAL)
        new = [h for h in wm.find_windows(_PROCESS) if h not in before]
        # A window titled "Close Firefox" is the profile-in-use dialog, not the
        # page. Taking it would report success and leave the user looking at an
        # error box the bot believes is a video.
        real = [h for h in new if "close firefox" not in h.lower()]
        if new and not real:
            log.warning("Firefox opened its profile-in-use dialog instead of the page")
            return ("Firefox is already running with that profile — close it "
                    "and try again."), None
        if real:
            hwnd = real[0]
            break
    if not hwnd:
        log.warning("Firefox launch requested but no new window showed up in time")
        return "Opened it, but couldn't confirm the window came up.", None

    wm.maximize(hwnd)
    wm.bring_to_front(hwnd)
    if config.BROWSER_KIOSK:
        time.sleep(_PLAYER_READY_DELAY)
        wm.send_key(wm.VK_F)
    return "", hwnd
