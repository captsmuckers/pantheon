"""Prove mpv IPC no longer blocks the asyncio event loop.

Simulates a slow/wedged pipe and measures whether a heartbeat coroutine keeps
ticking while player methods are in flight.
"""
import asyncio
import sys
import time
import warnings
from pathlib import Path

# Resolve relative to this file, never a hardcoded path. A hardcoded
# D:\bots\plexbot_new made every checkout import production's modules, so a
# worktree silently tested prod's code instead of its own.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.simplefilter("error", RuntimeWarning)  # catch un-awaited coroutines

import player as player_mod
from player import Player
from lyrics import Karaoke

IPC_DELAY = 0.4  # how slow each simulated pipe round trip is
WEDGE_DELAY = 5.0  # far longer than any IPC_TIMEOUT used in these tests


class SlowMPV:
    """Every attribute touch and command costs a slow round trip."""

    def __init__(self, tracks=None):
        object.__setattr__(self, "_tracks", tracks if tracks is not None else [])
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        time.sleep(IPC_DELAY)
        self.calls.append(("get", name))
        if name == "track_list":
            return self._tracks
        if name == "time_pos":
            return 12.0
        if name == "duration":
            return 100.0
        if name == "pause":
            return False
        if name == "idle_active":
            return False
        return None

    def __setattr__(self, name, value):
        time.sleep(IPC_DELAY)
        self.calls.append(("set", name, value))

    def command(self, *args):
        time.sleep(IPC_DELAY)
        self.calls.append(("cmd",) + args)


class WedgedMPV:
    """Doesn't respond at all — every access hangs for WEDGE_DELAY, standing
    in for a genuinely wedged mpv rather than merely a slow one."""

    def __getattr__(self, name):
        time.sleep(WEDGE_DELAY)
        return None

    def __setattr__(self, name, value):
        time.sleep(WEDGE_DELAY)

    def command(self, *args):
        time.sleep(WEDGE_DELAY)


class DummyLib:
    def report_progress(self, *a, **k):
        pass

    def external_subtitles(self, item):
        return []


class Heartbeat:
    """Stand-in for Discord's gateway keepalive."""

    def __init__(self):
        self.ticks = 0
        self.worst_gap = 0.0
        self._task = None

    async def _run(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            self.worst_gap = max(self.worst_gap, now - last)
            last = now
            self.ticks += 1

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self):
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


async def measure(label, coro_factory, heartbeat):
    before = heartbeat.ticks
    heartbeat.worst_gap = 0.0
    started = time.monotonic()
    result = await coro_factory()
    elapsed = time.monotonic() - started
    ticks = heartbeat.ticks - before
    status = "OK  " if heartbeat.worst_gap < 0.15 else "BLOCKED"
    print(f"  {status} {label:<28} {elapsed:5.2f}s  heartbeats={ticks:>3}  "
          f"worst stall={heartbeat.worst_gap * 1000:6.1f}ms")
    assert heartbeat.worst_gap < 0.15, f"{label} stalled the loop"
    return result


async def main():
    loop = asyncio.get_running_loop()
    lib = DummyLib()
    player = Player(lib, loop)
    player.mpv = SlowMPV()
    player.current = object()

    hb = Heartbeat()
    hb.start()
    await asyncio.sleep(0.05)

    print("Player controls (each mpv round trip costs "
          f"{IPC_DELAY * 1000:.0f}ms):")
    await measure("is_alive()", player.is_alive, hb)
    await measure("pause()", player.pause, hb)
    await measure("resume()", player.resume, hb)
    await measure("seek(30)", lambda: player.seek(30), hb)
    await measure("seek_to(90)", lambda: player.seek_to(90), hb)
    await measure("set_speed(2.0)", lambda: player.set_speed(2.0), hb)
    await measure("set_subtitle('off')", lambda: player.set_subtitle("off"), hb)
    await measure("set_audio(2)", lambda: player.set_audio(2), hb)
    await measure("show_text('hi')", lambda: player.show_text("hi"), hb)

    # The worst offender: empty track list -> 4 retries with 2.5s of sleeps.
    print("\nWorst case — track_list() with no tracks (2.5s of retry sleeps):")
    rows = await measure("track_list()", player.track_list, hb)
    assert rows == [], rows

    # And with tracks present, through the real preference path.
    print("\nTrack selection through apply_languages():")
    player.mpv = SlowMPV(tracks=[
        {"id": 1, "type": "audio", "lang": "jpn", "title": "Japanese 5.1",
         "demux-channel-count": 6, "default": True},
        {"id": 2, "type": "audio", "lang": "eng", "title": "English commentary"},
        {"id": 3, "type": "audio", "lang": "eng", "title": "English 5.1",
         "demux-channel-count": 6},
        {"id": 4, "type": "sub", "lang": "eng", "title": "Full Dialogue"},
        {"id": 5, "type": "sub", "lang": "eng", "title": "Signs & Songs"},
    ])
    out = await measure("apply_languages(jpn,eng)",
                        lambda: player.apply_languages("jpn", "eng"), hb)
    print(f"       -> {out}")
    assert "Japanese" in out and "English" in out, out
    sets = dict((c[1], c[2]) for c in player.mpv.calls if c[0] == "set")
    assert sets.get("aid") == 1, sets            # the 5.1 Japanese track
    assert sets.get("sid") == 4, sets            # full dialogue, not signs-only

    # Karaoke used to reach into player.mpv directly, twice a second.
    print("\nKaraoke OSD path:")
    karaoke = Karaoke(None, player)
    await measure("Karaoke overlay push", lambda: player.osd_overlay(1, "a lyric"), hb)
    await measure("Karaoke._clear()", karaoke._clear, hb)
    assert ("cmd", "osd-overlay", 1, "ass-events", "a lyric", 1920, 1080, 0) in player.mpv.calls

    # Background loops must not stall either.
    print("\nBackground loops (2 progress ticks + 1 watchdog pass):")
    player.start_background_tasks()
    before = hb.ticks
    hb.worst_gap = 0.0
    await asyncio.sleep(6)
    print(f"  {'OK  ' if hb.worst_gap < 0.15 else 'BLOCKED'} "
          f"{'6s of loops running':<28} {6.0:5.2f}s  heartbeats={hb.ticks - before:>3}  "
          f"worst stall={hb.worst_gap * 1000:6.1f}ms")
    assert hb.worst_gap < 0.15, "background loops stalled the event loop"
    for t in player._tasks:
        t.cancel()

    # mpv gone entirely: nothing should raise.
    print("\nmpv is None (crashed, not yet restarted):")
    player.mpv = None
    assert await player.is_alive() is False
    assert await player.track_list() == []
    await player.show_text("x")
    print("  OK   all calls degrade quietly")

    # A genuinely wedged mpv — process alive, IPC not responding at all.
    # Measured live 2026-08-14: is_alive() hanging exactly like this left
    # pause/stop typed during a crash-restart loop silent for a full two
    # minutes, until bot.py's blunt top-level timeout finally caught it.
    # IPC_TIMEOUT exists so callers here fail fast instead.
    print("\nA genuinely wedged mpv (hung, not just slow):")
    saved_timeout = player_mod.IPC_TIMEOUT
    player_mod.IPC_TIMEOUT = 0.3
    player.mpv = WedgedMPV()
    player.current = object()
    try:
        started = time.monotonic()
        out = await player._call(lambda: time.sleep(WEDGE_DELAY), default="timed out")
        elapsed = time.monotonic() - started
        print(f"  {'OK  ' if elapsed < 1.0 else 'FAIL'} _call() returned in {elapsed:.2f}s -> {out!r}")
        assert out == "timed out", out
        assert elapsed < 1.0, f"_call() took {elapsed:.2f}s — IPC_TIMEOUT not applied"

        started = time.monotonic()
        alive = await player.is_alive()
        elapsed = time.monotonic() - started
        print(f"  {'OK  ' if elapsed < 1.0 else 'FAIL'} is_alive() returned in {elapsed:.2f}s -> {alive}")
        assert elapsed < 1.0, f"is_alive() took {elapsed:.2f}s — IPC_TIMEOUT not applied"
        assert alive is False

        started = time.monotonic()
        result = await player.pause()
        elapsed = time.monotonic() - started
        print(f"  {'OK  ' if elapsed < 1.0 else 'FAIL'} pause() returned in {elapsed:.2f}s -> {result!r}")
        assert elapsed < 1.0, f"pause() took {elapsed:.2f}s — a hung mpv must not hang the caller"
    finally:
        player_mod.IPC_TIMEOUT = saved_timeout

    await hb.stop()
    print("\nALL PASS")


asyncio.run(main())
