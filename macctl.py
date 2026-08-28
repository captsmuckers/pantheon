"""Minimal macOS window control, via AppleScript. No-ops on other platforms.

The macOS counterpart to winctl.py, with the same function names and the same
contracts, so callers don't care which one they got — see wm.py, which picks.

The big difference is what a "handle" is. Windows has an integer hwnd that names
one window forever and can be handed back at any point in the future. macOS
exposes no such thing through the Accessibility API, so a handle here is a
string, "ProcessName#Index#Title", and both of the last two parts are needed.

Index alone is not enough, and this was measured rather than assumed. A process's
window list REORDERS when a window is minimised: minimising window 1 of a
two-window TextEdit left the list holding the other window at index 1 and the
minimised one at index 2. An index-only handle therefore starts pointing at a
different window the moment you act on it — `minimize()` then `is_minimized()`
on the same handle answered False, having minimised one window and then asked
about another. That is the exact failure this codebase already has scar tissue
from on the Windows side ("mpv ended up minimized AND holding foreground at
once, and the reply still said lyrics on the mpv window"), so it is worth
spending a little machinery to not reproduce it.

So every operation re-resolves the window by title first and falls back to the
index only if no window carries that title any more. Which of the two wins
matters per app:

  * mpv is launched with a fixed --title (MPV_WINDOW_TITLE), so the title is
    exact and stable; resolution is always correct.
  * Firefox's title is the page title — stable while a video plays, which is
    the whole window the browser fallback cares about.
  * Spotify rewrites its title to the current track, which is why winctl.py
    matches it by process rather than title in the first place. When the title
    has moved on, resolution falls back to the index.

Helper windows are excluded from every lookup, which the first version did not
do. Spotify in native fullscreen keeps an AXUnknown strip at 0,0 the height of
the menu bar, and it sorts FIRST — so find_window returned a 1512x33 sliver and
every minimize, maximize and raise went to it instead of the app. winctl.py has
always guarded against this by requiring a window to have a title; that guard
did not survive the port and is now back, alongside a subrole test.

Everything goes through System Events, which means the terminal or app running
the bot needs Accessibility permission:

    System Settings -> Privacy & Security -> Accessibility

Without it every call fails and returns the same "couldn't do it" answers a
missing window would, which is why _accessibility_ok() exists to say so once,
loudly, rather than leaving it looking like the windows aren't there.
"""

import logging
import subprocess
import sys

log = logging.getLogger("athena.macctl")

IS_MACOS = sys.platform == "darwin"

# Mirrors winctl's virtual key codes. Here they're literal characters, because
# System Events' `keystroke` takes text rather than a scan code.
VK_F = "f"
VK_SPACE = " "

# osascript is a process spawn per call, ~50-100ms. Fine: these fire on a
# source switch, not in any loop.
_TIMEOUT = 10

# Handles join their parts with "#". A window title may legitimately contain
# one, so the split below is bounded to two separators and the remainder is all
# title.
_SEP = "#"

# A window a person would point at, as opposed to the helper windows apps keep
# alongside them. Two tests, because either alone lets something through:
#
#   subrole AXStandardWindow  excludes overlays, popovers and sheets. Spotify
#                             keeps an AXUnknown strip at 0,0 sized to the menu
#                             bar height, and it sorts FIRST — so find_window
#                             returned it and every minimize/maximize/raise the
#                             bot issued went to a 1512x33 sliver instead of the
#                             app. The music switch would have looked broken
#                             while doing exactly what it was told.
#   a non-empty name          the same guard winctl.py has always applied
#                             ("skip invisible helper windows"), which did not
#                             survive the port.
_REAL_WINDOW = ('(subrole of window i of p is "AXStandardWindow") '
                'and ((name of window i of p) is not missing value) '
                'and ((name of window i of p as text) is not "")')



def _quote(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal."""
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str) -> str | None:
    """Run an AppleScript. Returns stdout, or None if it failed outright.

    None and "" are deliberately different: "" is a script that ran and found
    nothing, None is a script that could not run at all (no permission, no
    osascript, timeout). Only the second is worth complaining about.
    """
    if not IS_MACOS:
        return None
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("osascript failed: %s", exc)
        return None
    if result.returncode != 0:
        log.debug("osascript error: %s", result.stderr.strip())
        return None
    return result.stdout.strip()


def _osascript_jxa(script: str) -> str | None:
    """Run a JavaScript for Automation script. Same contract as _osascript.

    Separate because the two languages need different -l flags, and because the
    only thing JXA is used for here is its ObjC bridge, which AppleScript has no
    equivalent of.
    """
    if not IS_MACOS:
        return None
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("osascript (jxa) failed: %s", exc)
        return None
    if result.returncode != 0:
        log.debug("osascript (jxa) error: %s", result.stderr.strip())
        return None
    return result.stdout.strip()


_accessibility_warned = False


def _accessibility_ok() -> bool:
    """Whether System Events will actually answer us, warned about once.

    A denied permission looks exactly like an absent window from the outside,
    and that is a genuinely confusing hour to lose — so it gets said plainly,
    once, the first time something needs it.
    """
    global _accessibility_warned
    if not IS_MACOS:
        return False
    ok = _osascript(
        'tell application "System Events" to return count of processes'
    ) is not None
    if not ok and not _accessibility_warned:
        _accessibility_warned = True
        log.error(
            "macOS is refusing window control. Grant Accessibility permission to "
            "whatever runs the bot (Terminal, iTerm, or the Python binary) under "
            "System Settings > Privacy & Security > Accessibility, then restart it."
        )
    return ok


def _normalize(process: str | None) -> str:
    """Accept Windows-shaped process names so an unported .env still works.

    SPOTIFY_PROCESS defaults to "Spotify.exe" on the Windows side and the same
    value is perfectly likely to be sitting in someone's .env here.
    """
    name = (process or "").strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _split(handle):
    """"Proc#2#Title" -> ("Proc", 2, "Title").

    Tolerates the older two-part and bare forms, so a handle that predates the
    title being carried still resolves — by index, as it always did.
    """
    if not handle or not isinstance(handle, str):
        return None
    parts = handle.split(_SEP, 2)
    name = parts[0]
    if not name:
        return None
    index = 1
    if len(parts) > 1 and parts[1]:
        try:
            index = int(parts[1])
        except ValueError:
            index = 1
    title = parts[2] if len(parts) > 2 else ""
    return name, index, title


def _make_handle(process: str, index: int, title: str) -> str:
    return f"{process}{_SEP}{index}{_SEP}{title}"


def _script(handle, body: str) -> str | None:
    """Wrap an action in the resolution that binds `w` to the handle's window.

    The body is AppleScript operating on `w`, and runs inside `tell process`.
    Resolution order is: the index if it still carries the expected title, then
    any window that does, then the index regardless. See the module docstring
    for why the title has to be consulted at all.
    """
    parts = _split(handle)
    if not parts:
        return None
    proc, index, title = parts
    lines = [
        'tell application "System Events"',
        f'\ttell process "{_quote(proc)}"',
        '\t\tset w to missing value',
    ]
    if title:
        quoted = _quote(title)
        lines += [
            '\t\ttry',
            f'\t\t\tif name of window {index} is "{quoted}" then set w to window {index}',
            '\t\tend try',
            '\t\tif w is missing value then',
            '\t\t\trepeat with i from 1 to (count of windows)',
            f'\t\t\t\tif name of window i is "{quoted}" then',
            '\t\t\t\t\tset w to window i',
            '\t\t\t\t\texit repeat',
            '\t\t\t\tend if',
            '\t\t\tend repeat',
            '\t\tend if',
        ]
    lines += [
        f'\t\tif w is missing value then set w to window {index}',
        body,
        '\tend tell',
        'end tell',
    ]
    return "\n".join(lines)


def _run(handle, body: str) -> str | None:
    script = _script(handle, body)
    if script is None:
        return None
    return _osascript(script)


def find_window(title: str | None = None, process: str | None = None):
    """First window matching an exact title, or a process name. None if absent.

    Title matching wins when both are given, matching winctl. AppleScript's `is`
    ignores case, so "firefox" finds "Firefox" without extra work here.
    """
    if not IS_MACOS or not _accessibility_ok():
        return None

    if title:
        found = _osascript(f'''
tell application "System Events"
	repeat with p in (every process whose background only is false)
		try
			repeat with i from 1 to (count of windows of p)
				if {_REAL_WINDOW} and (name of window i of p) is "{_quote(title)}" then
					return (name of p as text) & "{_SEP}" & i & "{_SEP}" & (name of window i of p)
				end if
			end repeat
		end try
	end repeat
end tell
return ""''')
        if found:
            return found

    wanted = _normalize(process)
    if wanted:
        found = _osascript(f'''
tell application "System Events"
	repeat with p in (every process whose background only is false)
		if (name of p as text) is "{_quote(wanted)}" then
			try
				repeat with i from 1 to (count of windows of p)
					if {_REAL_WINDOW} then
						return (name of p as text) & "{_SEP}" & i & "{_SEP}" & (name of window i of p)
					end if
				end repeat
			end try
		end if
	end repeat
end tell
return ""''')
        if found:
            return found
    return None


def find_windows(process: str) -> list:
    """Every window of a process, not just the first.

    browser.py diffs this across a launch to spot the new window, because
    Firefox re-execs itself under a pid it never tells us about. That trick
    survives the port intact: the count going from one window to two is just as
    good a signal as a new hwnd was.
    """
    if not IS_MACOS or not _accessibility_ok():
        return []
    wanted = _normalize(process)
    if not wanted:
        return []
    out = _osascript(f'''
set found to {{}}
tell application "System Events"
	repeat with p in (every process whose background only is false)
		if (name of p as text) is "{_quote(wanted)}" then
			try
				repeat with i from 1 to (count of windows of p)
					if {_REAL_WINDOW} then
						set end of found to ((name of p as text) & "{_SEP}" & i & "{_SEP}" & (name of window i of p))
					end if
				end repeat
			end try
		end if
	end repeat
end tell
set AppleScript's text item delimiters to linefeed
return found as text''')
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _set_minimized(handle, value: bool) -> bool:
    literal = "true" if value else "false"
    return _run(
        handle,
        f'\t\tset value of attribute "AXMinimized" of w to {literal}',
    ) is not None


def minimize(handle) -> bool:
    if not IS_MACOS or not handle:
        return False
    return _set_minimized(handle, True)


def restore(handle) -> bool:
    """Un-minimise, and nothing else — same deliberate narrowness as winctl's.

    Un-zooming a window someone asked to be filled is never what a caller here
    wants, so a window that is merely not-minimised is already restored.
    """
    if not IS_MACOS or not handle:
        return False
    if not is_minimized(handle):
        return True
    return _set_minimized(handle, False)


def is_minimized(handle) -> bool:
    if not IS_MACOS or not handle:
        return False
    out = _run(
        handle,
        '\t\treturn (value of attribute "AXMinimized" of w) as text',
    )
    return out == "true"


def _visible_frame():
    """The usable screen area as (x, y, width, height), top-left origin.

    NSScreen.visibleFrame rather than the display's full frame, and on this
    hardware that is not a nicety. The machine is a 14-inch M1 Max, whose
    display has a notch: the frame is 1512x982 points but only 1512x859 at y=33
    is usable — 33pt taken at the top by the menu bar the notch sits in, 90pt at
    the bottom by the Dock. A window sized to the full frame puts its own title
    bar and controls up behind the notch, where they can be neither seen nor
    clicked, which for Spotify is exactly the part viewers are meant to be
    looking at.

    Reached through JavaScript for Automation, because AppleScript proper has no
    route to NSScreen. Cocoa's origin is bottom-left, so the y is flipped here
    into the top-left convention System Events uses for `position`.

    Re-queried per call rather than cached: plugging in an external display
    changes every number in it, and this runs rarely enough that one extra
    osascript spawn costs nothing worth saving.
    """
    out = _osascript_jxa(
        'ObjC.import("AppKit");\n'
        'var s = $.NSScreen.mainScreen;\n'
        'var f = s.frame, v = s.visibleFrame;\n'
        '[v.origin.x,\n'
        ' f.size.height - (v.origin.y + v.size.height),\n'
        ' v.size.width,\n'
        ' v.size.height].join(" ");'
    )
    if not out:
        return None
    try:
        x, y, w, h = (int(round(float(n))) for n in out.split())
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def maximize(handle) -> bool:
    """Fill the usable screen — not the whole screen, and not fullscreen.

    Not AXZoom: zoom is a toggle, so calling it on an already-zoomed window
    shrinks it, which is exactly backwards for a caller that wants the window
    big *now*, and the switch to music calls this every time. Not native
    fullscreen either: that moves the window to its own Space, taking it out of
    a screen share rather than into one.

    Setting bounds explicitly is idempotent, stays on the current Space, and is
    what viewers of a shared screen actually want. See _visible_frame for why
    those bounds are the visible frame and not the display's.
    """
    if not IS_MACOS or not handle:
        return False
    frame = _visible_frame()
    if not frame:
        log.debug("Could not read the screen's visible frame")
        return False
    x, y, w, h = frame
    restore(handle)
    exit_fullscreen(handle)
    return _run(
        handle,
        f'\t\tset position of w to {{{x}, {y}}}\n'
        f'\t\tset size of w to {{{w}, {h}}}',
    ) is not None


def is_fullscreen(handle) -> bool:
    """Is this window in macOS native fullscreen, on its own Space?"""
    if not IS_MACOS or not handle:
        return False
    return _run(handle,
                '\t\ttry\n'
                '\t\t\treturn (value of attribute "AXFullScreen" of w) as text\n'
                '\t\tend try\n'
                '\t\treturn "false"') == "true"


def exit_fullscreen(handle) -> bool:
    """Leave native fullscreen, so the window can be positioned at all.

    Not optional inside maximize(), and the reason is a silent failure rather
    than an awkward one. A fullscreen window reports AXPosition as not settable
    and quietly reverts any AXSize you set — but the AppleScript that sets them
    still SUCCEEDS. So maximize() returned True having changed nothing, which is
    precisely the "reported success it did not achieve" failure this module was
    already carrying scar tissue about from minimize().

    Observed live: Spotify put into native fullscreen by hand sat at 1512x949,
    ninety pixels of it behind the Dock, and refused every attempt to resize it
    while claiming each one worked.

    It also matters for the screen share in its own right. Native fullscreen
    moves a window to a Space of its own, which takes it OUT of a shared screen
    rather than filling one — the opposite of what every caller here wants.
    """
    if not IS_MACOS or not handle or not is_fullscreen(handle):
        return True
    ok = _run(handle,
              '\t\tset value of attribute "AXFullScreen" of w to false') is not None
    if ok:
        # Leaving fullscreen tears down the Space and rebuilds the window list —
        # Spotify's extra AXUnknown overlay disappeared with it — so anything
        # holding an index from before this is now stale.
        import time as _time
        _time.sleep(0.6)
    return ok


def is_foreground(handle) -> bool:
    """Does this window's process hold focus?

    Unlike Windows, macOS grants focus to a background process that asks, so
    this is usually true after bring_to_front rather than usually false.
    """
    if not IS_MACOS or not handle:
        return False
    parts = _split(handle)
    if not parts:
        return False
    out = _osascript(
        'tell application "System Events" to return name of first process '
        'whose frontmost is true'
    )
    return bool(out) and out.lower() == parts[0].lower()


def is_showing(handle) -> bool:
    """On screen, whether or not it has focus — the screen-share question."""
    if not IS_MACOS or not handle:
        return False
    out = _run(
        handle,
        '\t\tif visible of it is false then return "false"\n'
        '\t\treturn ((value of attribute "AXMinimized" of w) is false) as text',
    )
    if out is None:
        return False
    return out == "true"


def bring_to_front(handle, attempts: int = 6, delay: float = 0.4) -> bool:
    """Focus, then check — retried, matching winctl's contract.

    Kept even though macOS is far more cooperative about focus than Windows: an
    app still launching (Spotify cold-starting, in practice) can take a couple
    of seconds to have a window worth raising, and the retry costs nothing when
    the first attempt lands.

    Runs in a worker thread (see Controls._show_music_window), so the sleeps
    don't touch the event loop.
    """
    import time as _time

    if not IS_MACOS or not handle:
        return False
    for _ in range(attempts):
        focus(handle)
        _time.sleep(delay)
        if is_foreground(handle):
            return True
    return is_foreground(handle)


def focus(handle) -> bool:
    """Bring a window's process to the front, un-minimising the window first."""
    if not IS_MACOS or not handle:
        return False
    if is_minimized(handle):
        _set_minimized(handle, False)
    return _run(
        handle,
        '\t\tset frontmost of it to true\n'
        '\t\ttry\n'
        '\t\t\tperform action "AXRaise" of w\n'
        '\t\tend try',
    ) is not None


def focus_process(name: str) -> bool:
    """Bring an application to the front by process name, windows or not.

    Exists for mpv, which registers no windows with the Accessibility API on
    macOS — `count of windows of process "mpv"` is 0 while a film plays
    fullscreen. Every window-based route is therefore closed, but the PROCESS
    is still visible to AX and can still be made frontmost, which is all a
    caller wanting mpv on screen actually needs.
    """
    if not IS_MACOS or not name or not _accessibility_ok():
        return False
    wanted = _normalize(name)
    return _osascript(
        f'tell application "System Events"\n'
        f'\tif exists process "{_quote(wanted)}" then\n'
        f'\t\tset frontmost of process "{_quote(wanted)}" to true\n'
        f'\t\treturn "ok"\n'
        f'\tend if\n'
        f'end tell\n'
        f'return ""'
    ) == "ok"


def cursor_location():
    """Pointer position in top-left screen coordinates, or None.

    Pure ctypes against CoreGraphics rather than an osascript round trip,
    because this is cheap enough to poll and osascript is not.
    """
    if not IS_MACOS:
        return None
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))

        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = _Point
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        event = cg.CGEventCreate(None)
        if not event:
            return None
        try:
            pos = cg.CGEventGetLocation(event)
            return (pos.x, pos.y)
        finally:
            cf.CFRelease(event)
    except Exception:
        log.debug("Could not read the cursor position", exc_info=True)
        return None


def park_cursor() -> bool:
    """Put the pointer in the bottom-right corner, out of the way.

    This is damage limitation, not a fix, and the distinction is worth keeping
    because the fix was tried and does not work.

    mpv is told --cursor-autohide=always, which should hide the pointer
    whenever it is over mpv. It does not, and every condition that could
    explain it was measured and satisfied in turn: mpv frontmost, `focused`
    True, `mouse-pos.hover` True, and a real CGEvent mouse-move posted so mpv
    saw the motion rather than a silent CGWarp. The cursor stayed visible
    through all of it. Native fullscreen was tried too and is worse — it does
    not hide the cursor either AND it breaks window-minimized, which mpv then
    reports as having worked.

    So the pointer cannot be hidden; it can only be put somewhere it does not
    matter. The bottom-right corner is chosen over the centre because on a
    letterboxed film it sits in the black bar. Centre — which this did first —
    is the worst place on the screen.

    Why it is needed at all: a remote session disconnecting strands the pointer
    at the screen edge (measured at x=1512 on a 1512-wide screen, one pixel
    outside every window), and autohide is driven by motion that never comes.
    """
    if not IS_MACOS:
        return False
    frame = _visible_frame()
    if not frame:
        return False
    x, y, w, h = frame
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))

        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        cg.CGWarpMouseCursorPosition.argtypes = [_Point]
        cg.CGWarpMouseCursorPosition.restype = ctypes.c_int
        return cg.CGWarpMouseCursorPosition(
            _Point(float(x + w - 2), float(y + h - 2))) == 0
    except Exception:
        log.debug("Could not move the cursor", exc_info=True)
        return False


def send_key(key: str) -> None:
    """Press a key, sent to whatever holds focus.

    Same system-wide aim as winctl's keybd_event: there's no per-window
    targeting, so it only makes sense straight after focus() on the window that
    should receive it.
    """
    if not IS_MACOS or not key:
        return
    if _osascript(
        f'tell application "System Events" to keystroke "{_quote(key)}"'
    ) is None:
        log.debug("Could not send key %r", key)
