"""Minimal Windows window control, via ctypes. No-ops on other platforms.

Used to get mpv out of the way when music starts, and to bring it back when a
video starts. Matching is by process executable name rather than window title,
because Spotify rewrites its title to the current track.
"""

import ctypes
import logging
import os

log = logging.getLogger("athena.winctl")

IS_WINDOWS = os.name == "nt"

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
SW_SHOW = 5

KEYEVENTF_KEYUP = 0x0002
VK_F = 0x46
VK_SPACE = 0x20


def _win():
    return ctypes.windll if IS_WINDOWS else None


def _enum_windows() -> list[int]:
    handles: list[int] = []
    if not IS_WINDOWS:
        return handles
    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    user32.EnumWindows(proto(callback), 0)
    return handles


def _window_title(hwnd) -> str:
    if not IS_WINDOWS:
        return ""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _process_name(hwnd) -> str:
    if not IS_WINDOWS:
        return ""
    try:
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # PROCESS_QUERY_LIMITED_INFORMATION
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(260)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            )
            return os.path.basename(buf.value) if ok else ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return ""


def find_window(title: str | None = None, process: str | None = None) -> int | None:
    """First visible top-level window matching an exact title or process name."""
    if not IS_WINDOWS:
        return None
    wanted_proc = (process or "").lower()
    for hwnd in _enum_windows():
        if title and _window_title(hwnd) == title:
            return hwnd
        if wanted_proc and _process_name(hwnd).lower() == wanted_proc:
            if _window_title(hwnd):  # skip invisible helper windows
                return hwnd
    return None


def send_key(vk_code: int) -> None:
    """Press and release a key, via the old but simple keybd_event.

    Goes to whatever currently holds keyboard focus system-wide — there's no
    per-window targeting with this call, so it only makes sense right after
    focus() on the intended window.
    """
    if not IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk_code, 0, 0, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        log.debug("Could not send key %#x", vk_code, exc_info=True)


def find_windows(process: str) -> list[int]:
    """All visible top-level windows for an executable name, not just the first.

    Firefox specifically: the process a launch spawns is a short-lived
    "launcher" that re-execs itself as a new child under a different pid, so
    matching the pid subprocess.Popen hands back doesn't find the real
    browser window — measured live, a window opened and was fully usable
    under a pid that was never the one Popen returned. Matching by exe name
    and diffing against a before-launch snapshot (see browser.py) sidesteps
    that entirely, since it never needs to know which pid actually owns it.
    """
    if not IS_WINDOWS:
        return []
    wanted = (process or "").lower()
    return [hwnd for hwnd in _enum_windows()
            if _process_name(hwnd).lower() == wanted and _window_title(hwnd)]


def cursor_location():
    """Pointer position, or None."""
    if not IS_WINDOWS:
        return None
    try:
        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = _Point()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (float(pt.x), float(pt.y))
    except Exception:
        log.debug("Could not read the cursor position", exc_info=True)
    return None


def park_cursor() -> bool:
    """Put the pointer in the middle of the primary screen.

    Same purpose as the macOS one: mpv only hides the cursor while it is over
    mpv's own window, so a pointer left outside it stays visible on top of the
    video. Less often needed here — a remote session disconnecting does not
    reliably strand it at the screen edge the way it does on macOS — but the
    callers are shared, so the capability has to be.
    """
    if not IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        return bool(user32.SetCursorPos(w // 2, h // 2))
    except Exception:
        log.debug("Could not move the cursor", exc_info=True)
        return False


def focus_process(name: str) -> bool:
    """Bring an application to the front by process name.

    The macOS counterpart exists because mpv exposes no windows to the
    Accessibility API there; here mpv has an ordinary hwnd, so this is just
    find_windows + focus and behaves the same from the caller's side.
    """
    if not IS_WINDOWS or not name:
        return False
    handles = find_windows(name)
    return focus(handles[0]) if handles else False


def minimize(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)
        return True
    except Exception:
        return False


def restore(hwnd) -> bool:
    """Un-minimise. Deliberately a no-op otherwise: SW_RESTORE on a maximised
    window un-maximises it, which is not what any caller here wants."""
    if not IS_WINDOWS or not hwnd:
        return False
    if not is_minimized(hwnd):
        return True
    try:
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        return True
    except Exception:
        return False


def is_minimized(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsIconic(hwnd))
    except Exception:
        return False


def maximize(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return True
    except Exception:
        return False


def is_foreground(hwnd) -> bool:
    """Does this window hold keyboard focus?

    Rarely true for a window we raised: Windows only grants foreground to a
    process that already has it, and the bot is always a background process.
    For "can viewers see it", use is_showing instead.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def is_showing(hwnd) -> bool:
    """Visible and not minimised — on screen, whether or not it has focus.

    This is the question that matters for a screen share. ShowWindow with
    SW_MAXIMIZE raises and shows a window reliably from a background process,
    while SetForegroundWindow is blocked; judging the swap by foreground status
    reported failure every time for a screen that had in fact switched.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd)) and not is_minimized(hwnd)
    except Exception:
        return False


def bring_to_front(hwnd, attempts: int = 6, delay: float = 0.4) -> bool:
    """Focus, then give Windows a moment before believing the answer.

    Windows frequently refuses the first foreground request from a background
    process, and Spotify in particular may still be painting its window when we
    ask — so this retries.

    The settle *before* each check matters as much as the retry:
    SetForegroundWindow doesn't take effect synchronously, and callers maximise
    the window immediately beforehand, so a check issued in the same breath
    races the window manager. Doing it the other way round reported failure for
    swaps that actually landed a moment later, and sent a misleading "say *show
    spotify* to retry" for a screen that had already switched.

    Runs in a worker thread (see Controls._show_music_window), so the sleeps
    don't touch the event loop.
    """
    import time as _time

    for _ in range(attempts):
        focus(hwnd)
        _time.sleep(delay)
        if is_foreground(hwnd):
            return True
    return is_foreground(hwnd)


def focus(hwnd) -> bool:
    """Bring a window to the front.

    Windows blocks SetForegroundWindow from background processes, so we borrow
    the foreground thread's input state first. Still not guaranteed, which is
    why minimizing the window in front matters more than focusing this one.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    user32 = ctypes.windll.user32
    try:
        # Only un-minimise. An unconditional SW_RESTORE here would un-maximise
        # a maximised window — which is exactly what we just asked for.
        if is_minimized(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        current = user32.GetForegroundWindow()
        if current == hwnd:
            return True
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = user32.GetWindowThreadProcessId(current, None)
        attached = False
        if target_thread and current_thread and target_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, target_thread, False)
        return True
    except Exception:
        log.debug("Could not focus window", exc_info=True)
        return False