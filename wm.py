"""Window control, dispatched to whichever platform we're actually on.

Two backends implement the same names: winctl.py (Win32 via ctypes) and
macctl.py (AppleScript via osascript). Callers import this and never learn
which one they got.

The re-export is written out by name rather than `import *` on purpose — a
backend that grows a function the other doesn't have should fail here, at
import, rather than at the moment someone switches to music.

What a handle *is* differs between the two (an integer hwnd on Windows, a
"Process#Index" string on macOS) and no caller may assume either. Every one of
them already treats it as opaque: fetch it from find_window, hand it straight
back, never store it.

Anything that isn't Windows or macOS falls through to winctl, whose functions
all no-op off Windows — the same silent, non-fatal degradation this codebase
has always had on an unsupported platform.
"""

import sys

_NAMES = (
    "find_window", "find_windows", "minimize", "restore", "is_minimized",
    "maximize", "is_foreground", "is_showing", "bring_to_front", "focus",
    "send_key", "focus_process", "cursor_location", "park_cursor",
    "VK_F", "VK_SPACE",
)

if sys.platform == "darwin":
    import macctl as _backend
else:
    import winctl as _backend

_missing = [name for name in _NAMES if not hasattr(_backend, name)]
if _missing:
    raise ImportError(
        f"{_backend.__name__} is missing {', '.join(_missing)} — the window "
        "backends have drifted apart"
    )

for _name in _NAMES:
    globals()[_name] = getattr(_backend, _name)

BACKEND = _backend.__name__

__all__ = list(_NAMES) + ["BACKEND"]
