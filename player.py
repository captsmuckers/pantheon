"""mpv-backed playback.

This replaces the Plex Companion remote-control API entirely. The important
consequence: *we* own the queue and *we* decide what plays next, so an episode
transition is a deterministic loadfile call rather than a request that the
Plex client might silently drop.

Threading: python-mpv-jsonipc fires events on its own thread. Everything that
touches bot state is bounced back onto the asyncio loop with
call_soon_threadsafe. Plex calls are blocking and go through asyncio.to_thread.
"""

import asyncio
import atexit
import itertools
import logging
import os
import shutil
import subprocess
import sys
import time

from python_mpv_jsonipc import MPV

import config
import wm
import tracks as tk
from library import Library, describe

log = logging.getLogger("athena.player")

# Failed loads in a row before we stop rather than walk the rest of a series at
# full speed. mpv reports an unplayable file as end-file reason='error'.
MAX_CONSECUTIVE_ERRORS = 3
# Freeze-recovery reloads for one item before we accept it isn't coming back.
# A stream that never connects produces no end-file at all, so the watchdog is
# the only thing that notices it — and without a cap it retries forever.
MAX_FREEZE_RELOADS = 3

# How long to wait for an external source (YouTube) to prove it actually
# loaded. yt-dlp resolution inside mpv takes a couple of seconds, and a dead
# link only reveals itself once that finishes.
EXTERNAL_LOAD_TIMEOUT = 12

# How often the background loops wake. Module-level so tests can wind them down
# instead of waiting out real seconds.
WATCHDOG_INTERVAL = 5
PROGRESS_INTERVAL = 2

# A ceiling on every mpv IPC round trip. A healthy local named-pipe call
# takes milliseconds; anything this slow means mpv is wedged, not merely
# busy. Without this, a hung call (measured live 2026-08-14, during a
# crash-restart loop on a bad file) blocks whichever command triggered it —
# pause, stop, is_alive, all of them — until bot.py's blunt 120s top-level
# timeout finally catches it, with total silence in between. Timing out here
# instead means the caller gets a fast, honest failure and can treat it the
# same as mpv being dead.
IPC_TIMEOUT = 8.0


class Player:
    def __init__(self, lib: Library, loop: asyncio.AbstractEventLoop):
        self.lib = lib
        self.loop = loop
        self.mpv: MPV | None = None

        self.current = None           # plexapi item currently loaded
        self.queue: list[int] = []    # rating keys, next up first
        self.position = 0.0           # seconds
        self.duration = 0.0
        self.paused = False
        self.idle = True
        self.audio_language = None
        self.subtitle_language = None
        self.speed = 1.0
        # Each mpv launch gets its own pipe name and generation number. Without
        # this, an abandoned instance from a timed-out start can hold the pipe
        # and its quit callback can trigger a restart of the live one.
        self._socket_counter = itertools.count(1)
        self._generation = 0
        self._proc: subprocess.Popen | None = None
        self._mpv_log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs", "mpv-stderr.log"
        )
        os.makedirs(os.path.dirname(self._mpv_log_path), exist_ok=True)
        self._last_death_at = 0.0
        self._deaths_in_a_row = 0
        self._showing_idle = False
        self._parked: tuple = ()

        # Guards every mutation of current/queue/position. Public methods take
        # it; the _locked variants assume the caller already holds it.
        self._lock = asyncio.Lock()
        self._last_progress_value = -1.0
        self._last_progress_at = time.monotonic()
        self._restarting = False
        self._pending_seek: float | None = None
        # mpv's id for the loaded playlist entry. end-file events carry the
        # entry they belong to, so a late event from a file we already replaced
        # can be told apart from a genuine one instead of guessed at.
        self._entry_id = None
        # (entry_id, reason, file_error) of the most recent end-file, written
        # from mpv's thread so a load can be confirmed without the lock.
        self._last_end_file = None
        self._consecutive_errors = 0
        self._freeze_reloads = 0
        self._bg: set[asyncio.Task] = set()
        self.prefs = tk.Preferences(
            config.PREFS_FILE,
            config.DEFAULT_AUDIO_LANG,
            config.DEFAULT_SUBTITLE_LANG,
            {
                name: {"audio": config.ANIME_AUDIO_LANG, "subs": config.ANIME_SUBTITLE_LANG}
                for name in config.ANIME_LIBRARIES
            },
        )
        self._tasks: list[asyncio.Task] = []
        self.on_change = None  # optional async callback(player) for status updates
        # optional async callback(str) for problems a viewer needs to hear
        # about — giving up on a file, giving up on mpv. Log-only isn't enough
        # when the screen just goes idle with no explanation.
        self.on_notice = None
        # Last resort: if the process exits without running shutdown(), don't
        # leave an mpv behind holding the screen.
        atexit.register(self._kill_process)

    # ------------------------------------------------------------------
    # mpv IPC
    #
    # Every property read, property write and command here is a synchronous
    # round trip over a named pipe. Issued straight from a coroutine they block
    # the whole bot — Discord heartbeats included — and a wedged (rather than
    # dead) mpv would take the event loop down with it, which is exactly the
    # case the watchdog exists to recover from. So the blocking forms are
    # private and thread-only, and anything running on the loop goes through
    # the async wrappers.
    #
    # Names are the Python-side ones python-mpv-jsonipc expects (time_pos,
    # track_list); it does the mapping to mpv's hyphenated properties itself.
    # ------------------------------------------------------------------

    def _get(self, name: str, default=None):
        try:
            return getattr(self.mpv, name)
        except Exception:
            return default

    def _set(self, name: str, value) -> bool:
        try:
            setattr(self.mpv, name, value)
            return True
        except Exception as exc:
            log.debug("Could not set mpv %s=%r: %s", name, value, exc)
            return False

    def _cmd(self, *args) -> bool:
        try:
            self.mpv.command(*args)
            return True
        except Exception as exc:
            log.debug("mpv command %s failed: %s", args[0] if args else "?", exc)
            return False

    def _poll(self):
        """The three values the progress loop needs, in one thread hop."""
        if self.mpv is None:
            return None
        try:
            return self.mpv.time_pos, bool(self.mpv.pause), self.mpv.duration
        except Exception:
            return None

    async def _call(self, fn, *args, default=None, label: str = ""):
        """A blocking mpv call, bounded by IPC_TIMEOUT (see its comment)."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), IPC_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("mpv IPC timed out on %s — treating as wedged",
                        label or getattr(fn, "__name__", "?"))
            return default

    async def _aget(self, name: str, default=None):
        if self.mpv is None:
            return default
        return await self._call(self._get, name, default, default=default, label=f"get {name}")

    async def _aset(self, name: str, value) -> bool:
        if self.mpv is None:
            return False
        return await self._call(self._set, name, value, default=False, label=f"set {name}")

    async def _acmd(self, *args) -> bool:
        if self.mpv is None:
            return False
        return await self._call(self._cmd, *args, default=False,
                                 label=args[0] if args else "?")

    async def show_text(self, text: str, duration_ms: int = 1500) -> None:
        """A plain OSD toast message. Confirmed live 2026-08-14: this does
        NOT parse ASS override tags — {\\k50}word showed up on screen as
        those literal characters, not a styled fill. Anything needing real
        styling or animation has to go through osd_overlay() instead,
        which renders through mpv's actual subtitle/ASS engine.
        """
        await self._acmd("show-text", text, duration_ms)

    async def osd_overlay(self, overlay_id: int, data: str,
                           res_x: int = 1920, res_y: int = 1080) -> None:
        """A real ASS overlay — supports full override tags, including an
        animated \\k karaoke fill, because it's rendered by the same engine
        real subtitles are. Unlike show_text(), this does not expire on its
        own; it stays on screen until replaced or cleared.

        data is the ASS "Text" field content (override tags + text), not a
        full Dialogue: line — mpv wraps it. Line breaks must be the ASS
        escape \\N; a literal newline character is ignored by the renderer.
        """
        await self._acmd("osd-overlay", overlay_id, "ass-events", data, res_x, res_y, 0)

    async def clear_osd_overlay(self, overlay_id: int) -> None:
        await self._acmd("osd-overlay", overlay_id, "none", "", 0, 0, 0)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _mpv_options(self) -> dict:
        opts = {
            "idle": "yes",          # stay alive between files instead of exiting
            "force_window": "yes",  # keep the window up so there's no black-hole state
            "keep_open": "no",      # let end-file fire so we can advance the queue
            "osc": "yes",
            "input_default_bindings": "yes",
            "input_vo_keyboard": "yes",
            "title": config.MPV_WINDOW_TITLE,
            "cache": "yes",
            "demuxer_max_bytes": "150MiB",
            "user_agent": "Athena/1.0",
            "image_display_duration": "inf",  # idle screen must not time out
            # Never show the mouse pointer over the video.
            #
            # The default is 1000, meaning "hide it a second after it stops
            # moving", which sounds equivalent and is not: hiding is driven by
            # mouse ACTIVITY, so a pointer that is simply parked over the window
            # and never moves again is never hidden. That is exactly what a
            # remote-desktop session leaves behind when it disconnects — the
            # cursor stays wherever it was, on top of the film, indefinitely.
            # "always" hides it whenever it is over mpv rather than waiting for
            # motion that is not coming.
            #
            # Overridable through MPV_EXTRA_OPTS, which is applied after this,
            # for anyone who wants to drive mpv by hand.
            "cursor_autohide": "always",
        }
        if config.MPV_FULLSCREEN:
            opts["fullscreen"] = "yes"
            if sys.platform == "darwin":
                # Non-native fullscreen on macOS, which is a different thing
                # from the Windows notion and matters twice over.
                #
                # mpv defaults to --native-fs=yes, the green-button fullscreen
                # that moves the window to a Space of its own. A Space is a
                # poor place to be for something whose entire purpose is to be
                # visible in a screen share: switching to it switches the whole
                # desktop, and it is the same trap macctl.maximize avoids for
                # Spotify. Non-native fullscreen simply covers the current
                # screen and stays where the rest of the desktop is.
                #
                # It also decides whether the menu bar — and the privacy
                # indicators macOS draws in it — can appear over the film.
                opts["native_fs"] = config.MPV_NATIVE_FS
                if config.MPV_NATIVE_FS == "no":
                    # Non-native fullscreen fills the screen but does NOT claim
                    # the menu bar, so macOS keeps drawing it over the top of
                    # the film — which was the very first thing reported about
                    # this port ("mpv isn't fullscreen, I see the mac toolbar").
                    # Native fullscreen does hide it, but costs more than it is
                    # worth: it moves mpv to a Space of its own AND silently
                    # breaks window-minimized, which mpv then reports as having
                    # worked, taking the switch to music with it.
                    #
                    # Raising the window above the menu bar's level gets the
                    # menu bar out of the way without either of those costs.
                    # Safe alongside minimising for Spotify: a minimised window
                    # is not on screen no matter what level it claims.
                    opts["ontop"] = "yes"
                    opts["ontop_level"] = "system"
        if config.YTDL_PATH:
            # mpv's ytdl hook searches PATH, and a pip-installed yt-dlp lands
            # in Python's Scripts directory which usually isn't on it. Point
            # mpv straight at the binary rather than asking anyone to edit
            # their environment.
            opts["script_opts"] = f"ytdl_hook-ytdl_path={config.YTDL_PATH}"
            if config.YTDL_FORMAT:
                opts["ytdl_format"] = config.YTDL_FORMAT
            # Resolving the title is not enough — mpv fetches the media itself
            # through its own ytdl hook, so an age-gated video needs the
            # session here too or playback fails after a successful lookup.
            if config.YTDL_COOKIES_FROM_BROWSER:
                opts["ytdl_raw_options"] = (
                    f"cookies-from-browser={config.YTDL_COOKIES_FROM_BROWSER}"
                )
            elif config.YTDL_COOKIEFILE:
                opts["ytdl_raw_options"] = f"cookies={config.YTDL_COOKIEFILE}"
        for pair in config.MPV_EXTRA_OPTS.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            opts[key.strip().replace("-", "_")] = value.strip()
        return opts

    def _mpv_executable(self) -> str:
        return config.MPV_PATH or shutil.which("mpv") or shutil.which("mpv.exe") or "mpv"

    # ---------- stale instance reaping ----------
    #
    # A hard kill (Task Manager, a crash, Stop-Process -Force) runs neither our
    # shutdown() nor the atexit hook, so the mpv we spawned outlives us —
    # holding a fullscreen window that has to be closed by hand. We record the
    # pid we started and reap it on the next launch.

    def _pid_file(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mpv-pid")

    def _remember_pid(self, pid: int) -> None:
        try:
            with open(self._pid_file(), "w", encoding="utf-8") as fh:
                fh.write(str(pid))
        except Exception as exc:
            log.debug("Could not record the mpv pid: %s", exc)

    def _forget_pid(self) -> None:
        try:
            os.remove(self._pid_file())
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("Could not clear the mpv pid file: %s", exc)

    @staticmethod
    def _image_name(pid: int) -> str:
        """Executable name for a pid, or "" — so we never kill a recycled pid."""
        if os.name != "nt":
            return ""
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(260)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            )
            return os.path.basename(buf.value).lower() if ok else ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def _reap_stale_mpv(self) -> None:
        try:
            with open(self._pid_file(), "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip())
        except (FileNotFoundError, ValueError):
            return
        except Exception:
            return

        try:
            if os.name == "nt":
                # Only if that pid really is an mpv — pids get recycled.
                if not self._image_name(pid).startswith("mpv"):
                    self._forget_pid()
                    return
                import ctypes

                handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    ctypes.windll.kernel32.CloseHandle(handle)
                    log.warning("Killed an mpv (pid %d) left behind by a previous run", pid)
            else:
                import signal

                os.kill(pid, signal.SIGTERM)
                log.warning("Killed an mpv (pid %d) left behind by a previous run", pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        finally:
            self._forget_pid()

    def _spawn(self, pipe_path: str) -> subprocess.Popen:
        args = [self._mpv_executable(), f"--input-ipc-server={pipe_path}"]
        for key, value in self._mpv_options().items():
            args.append(f"--{key.replace('_', '-')}={value}")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # mpv's own error output used to go to DEVNULL, which is why a crash
        # loop (measured live 2026-08-14, Heat (1995) dying repeatedly at the
        # same point) left no trace of *why* mpv was exiting — only that it
        # had. The Python-side file object is closed right after Popen();
        # the child keeps its own duplicated handle, so this doesn't leak.
        with open(self._mpv_log_path, "a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"\n---- mpv launch {time.strftime('%Y-%m-%dT%H:%M:%S')} "
                            f"({os.path.basename(pipe_path)}) ----\n")
            log_file.flush()
            return subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                creationflags=creationflags,
            )

    def start(self) -> None:
        """Launch mpv and connect to it. Blocking; call via to_thread.

        We spawn the process ourselves instead of letting python-mpv-jsonipc do
        it, because that library waits a fixed 10 seconds and then abandons the
        process it started — which is how you end up with orphaned instances
        fighting over the same pipe.
        """
        self._generation += 1
        generation = self._generation
        name = f"{config.MPV_IPC_SOCKET}-{os.getpid()}-{next(self._socket_counter)}"
        pipe_path = rf"\\.\pipe\{name}" if os.name == "nt" else f"/tmp/{name}.sock"

        self._kill_process()
        self._reap_stale_mpv()
        log.info("Launching mpv (%s)...", name)
        started = time.monotonic()
        self._proc = self._spawn(pipe_path)
        self._remember_pid(self._proc.pid)

        last_error = None
        while time.monotonic() - started < config.MPV_START_TIMEOUT:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"mpv exited immediately with code {self._proc.returncode}. "
                    "Try running mpv by hand with the same options."
                )
            try:
                self.mpv = MPV(
                    start_mpv=False,
                    # python-mpv-jsonipc takes a BARE pipe name on Windows and
                    # prepends \\.\pipe\ itself, but on POSIX it uses what it
                    # is given verbatim as a filesystem path. Handing it the
                    # bare name on both — as this did — meant the client looked
                    # for a relative file in the working directory while mpv
                    # listened on /tmp, so they could never meet.
                    ipc_socket=name if os.name == "nt" else pipe_path,
                    quit_callback=lambda: self._on_mpv_quit(generation),
                )
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.4)
        else:
            self._kill_process()
            raise RuntimeError(
                f"mpv did not open its IPC channel within "
                f"{config.MPV_START_TIMEOUT}s (last error: {last_error}). "
                "Raise MPV_START_TIMEOUT if mpv is just slow to start here."
            )

        self.mpv.bind_event("end-file", self._on_end_file)
        self.mpv.bind_event("file-loaded", self._on_file_loaded)
        log.info("mpv is up after %.1fs", time.monotonic() - started)
        self._load_idle_screen()

    def _kill_process(self) -> None:
        """Make sure our mpv is gone. Nothing else should own this process."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self._forget_pid()
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _load_idle_screen(self) -> None:
        """Show a branded still instead of mpv's 'drop files here' logo.

        Viewers see this between videos and on startup, so it should say
        something useful rather than advertise the player.
        """
        path = (config.IDLE_IMAGE or "").strip()
        if not path:
            return
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if not os.path.exists(path):
            log.warning("Idle image not found at %s — using mpv's default screen", path)
            return
        try:
            self.mpv.start = "0"  # never "none" over IPC — see _play_locked
            loaded = self.mpv.command("loadfile", path, "replace")
            # Track this load too, so _entry_id never points at a video that
            # the idle screen has since replaced.
            self._entry_id = (
                loaded.get("playlist_entry_id") if isinstance(loaded, dict) else None
            )
            self.mpv.pause = False
            self._showing_idle = True
        except Exception as exc:
            log.debug("Could not load idle screen: %s", exc)

    async def show_idle_screen(self) -> None:
        if await self.is_alive():
            await asyncio.to_thread(self._load_idle_screen)

    def start_background_tasks(self) -> None:
        self._tasks = [
            self.loop.create_task(self._progress_loop()),
            self.loop.create_task(self._watchdog_loop()),
        ]

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.current:
            await asyncio.to_thread(
                self.lib.report_progress, self.current, self.position, "stopped"
            )
        if self.mpv:
            try:
                await asyncio.to_thread(self.mpv.terminate)
            except Exception:
                pass
        await asyncio.to_thread(self._kill_process)

    # ------------------------------------------------------------------
    # mpv event handlers (these run on mpv's thread!)
    # ------------------------------------------------------------------

    def _on_mpv_quit(self, generation: int) -> None:
        if generation != self._generation:
            # An older instance shutting down. Not our problem.
            log.debug("Ignoring quit from stale mpv generation %d", generation)
            return
        log.warning("mpv exited")
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(self._handle_player_death())
        )

    def _on_end_file(self, event) -> None:
        event = event or {}
        reason = event.get("reason", "eof")
        entry_id = event.get("playlist_entry_id")
        file_error = event.get("file_error")
        log.info("end-file: %s (entry %s)%s", reason, entry_id,
                 f" — {file_error}" if file_error else "")
        # Recorded on mpv's own thread, deliberately without the lock:
        # _play_locked polls this while holding it, and routing through the
        # async handler instead would deadlock.
        self._last_end_file = (entry_id, reason, file_error)
        # Only these two mean "this file is done with". Replacing a file mid
        # playback reports 'stop', which is our own doing and needs no action.
        if reason in ("eof", "error"):
            self.loop.call_soon_threadsafe(
                lambda: self.loop.create_task(
                    self._on_playback_finished(reason, entry_id, file_error)
                )
            )

    def _on_file_loaded(self, event) -> None:
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(self._after_load())
        )

    async def _after_load(self) -> None:
        """Belt and braces on resume: --start usually lands it, but if the
        file opened at the beginning anyway, seek explicitly."""
        # A file that opened is proof the *load* errors are over. Freeze
        # recovery deliberately isn't reset here: a stream that connects and
        # then stalls would clear the counter on every reload and retry
        # forever. Only real progress clears that one — see _watchdog_loop.
        self._consecutive_errors = 0
        target = self._pending_seek
        self._pending_seek = None
        if target and target > 10:
            pos = await self._aget("time_pos") or 0.0
            if pos < target - 10:
                await self._acmd("seek", target, "absolute")
        if self.current is not None and not self._showing_idle:
            await self._add_external_subs(self.current)
            await self.apply_track_prefs()
        await self._notify_change()

    # ------------------------------------------------------------------
    # queue / advance
    # ------------------------------------------------------------------

    async def _on_playback_finished(
        self, reason: str, entry_id=None, file_error: str | None = None
    ) -> None:
        async with self._lock:
            # Re-checked here rather than at event time: this coroutine may
            # have waited on the lock while a skip loaded something else, and
            # acting on a superseded file would advance the queue twice.
            if self._is_stale_entry(entry_id):
                log.debug(
                    "Ignoring %s for entry %s — entry %s is loaded now",
                    reason, entry_id, self._entry_id,
                )
                return

            finished = self.current
            if reason == "error":
                self._consecutive_errors += 1
                log.warning(
                    "mpv could not play %s (%s) — %d in a row",
                    describe(finished), file_error or "no detail",
                    self._consecutive_errors,
                )
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    await self._give_up_locked(
                        f"Stopping — {self._consecutive_errors} files in a row failed to play "
                        f"(last: {describe(finished)}, {file_error or 'no detail'}). "
                        f"Check that the bot's host can reach Plex directly."
                    )
                    return
            elif finished is not None and not getattr(finished, "external", False):
                await asyncio.to_thread(self.lib.mark_played, finished)

            # A live stream reaching end-file means the broadcaster went
            # offline. There is no "next" for it, and advancing the queue here
            # would start something nobody asked for — possibly hours after
            # they asked for the stream. Say so and go idle.
            if finished is not None and getattr(finished, "is_live", False):
                self.current = None
                self.idle = True
                self.position = 0.0
                self._freeze_reloads = 0
                log.info("Live stream ended: %s", describe(finished))
                await self.show_idle_screen()
                await self._notify_change()
                await self._notice(f"**{describe(finished)}** ended — the stream went offline.")
                return

            next_item = await self._pop_next(finished)
            if next_item is None:
                self.current = None
                self.idle = True
                self.position = 0.0
                log.info("Queue empty — going idle")
                await self.show_idle_screen()
                await self._notify_change()
                return
            await self._play_locked(next_item)

    def _is_stale_entry(self, entry_id) -> bool:
        """Whether an end-file event belongs to a file we've already replaced.

        Older mpv builds omit playlist_entry_id; without it we can't tell, so
        the event is treated as current (the pre-existing behaviour).
        """
        if entry_id is None or self._entry_id is None:
            return False
        return entry_id != self._entry_id

    async def _give_up_locked(self, message: str) -> None:
        """Stop cleanly and say why. Caller holds the lock."""
        self.queue.clear()
        self.current = None
        self.idle = True
        self.position = 0.0
        self._consecutive_errors = 0
        self._freeze_reloads = 0
        await self.show_idle_screen()
        await self._notify_change()
        await self._notice(message)

    async def _notice(self, message: str) -> None:
        log.warning("%s", message)
        if self.on_notice is None:
            return
        try:
            await self.on_notice(message)
        except Exception:
            log.exception("on_notice callback failed")

    async def _pop_next(self, finished):
        """Next item from the queue, or the following episode of a show."""
        while self.queue:
            key = self.queue.pop(0)
            try:
                return await asyncio.to_thread(self.lib.fetch, key)
            except Exception:
                log.warning("Queued item %s no longer exists, skipping", key)

        if (
            config.AUTOPLAY_NEXT_EPISODE
            and finished is not None
            and getattr(finished, "type", None) == "episode"
        ):
            return await asyncio.to_thread(self.lib.next_episode, finished)
        return None

    # ------------------------------------------------------------------
    # public controls
    # ------------------------------------------------------------------

    async def play(self, item, offset: float | None = None) -> str:
        """Load and play a Plex item immediately."""
        async with self._lock:
            return await self._play_locked(item, offset)

    async def _play_locked(self, item, offset: float | None = None) -> str:
        """The body of play(). Caller must hold self._lock.

        Exists so the paths that already hold the lock — advancing the queue,
        skipping, unparking, freeze recovery — can load a file without
        deadlocking on a non-reentrant lock.
        """
        if not await self._ensure_alive():
            return "mpv isn't running and wouldn't restart. Check the logs."

        if self.current is not None and self.current is not item:
            self._report_later(self.current, self.position, "stopped")

        # Items from outside Plex (YouTube) carry their own URL and have no
        # resume position, watch state or timeline to report against.
        external = getattr(item, "external", False)
        if external:
            url = getattr(item, "stream_url", "")
            offset = offset or 0.0
        else:
            url = await asyncio.to_thread(self.lib.stream_url, item)
            if offset is None:
                offset = await asyncio.to_thread(self.lib.resume_offset, item)
        if not url:
            return f"Couldn't get a playable file for {describe(item)}."

        self._showing_idle = False
        self._pending_seek = offset or None
        # Setting `start` before loadfile is version-stable, unlike passing
        # options positionally to loadfile. If it doesn't take, _after_load
        # seeks instead.
        #
        # "0" rather than "none" to clear a previous resume position: assigning
        # the string "none" to this property at runtime over IPC makes mpv
        # reject EDL sources, which is how YouTube arrives once yt-dlp has
        # split video and audio. It failed with "no audio or video data
        # played" while the same value on the command line was harmless.
        await self._aset("start", str(int(offset)) if offset else "0")
        # Not via _acmd: a failure here is the one worth a full traceback,
        # and _ensure_alive above means self.mpv is real.
        try:
            loaded = await asyncio.to_thread(self.mpv.command, "loadfile", url, "replace")
        except Exception:
            log.exception("loadfile failed for %s", describe(item))
            return "mpv rejected the play command. Try `restart player`."
        self._entry_id = loaded.get("playlist_entry_id") if isinstance(loaded, dict) else None
        await self._aset("pause", False)

        # A Plex file either loads or the command fails outright. Anything
        # external is resolved by mpv *after* loadfile returns, so a dead link
        # surfaces seconds later as an end-file error — long after we'd have
        # cheerfully announced "Playing". Wait for the verdict before claiming
        # anything happened.
        if external:
            confirmed = await self._await_load(self._entry_id)
            if confirmed is not None:
                return confirmed

        # A new file always starts at normal speed — otherwise a
        # forgotten 4x carries into the next episode.
        await self._aset("speed", 1.0)
        self.speed = 1.0

        self.current = item
        self.duration = (getattr(item, "duration", 0) or 0) / 1000.0
        self.position = offset or 0.0
        self.paused = False
        self.idle = False
        # Whatever was set aside for music is moot once a video is on screen
        # again. unpark() clears this before calling us, so it only fires when
        # something *else* started — otherwise a stale park lingers and a later
        # "resume video" jumps to a film nobody asked about.
        self._parked = ()
        self._last_progress_value = -1.0
        self._last_progress_at = time.monotonic()

        await self._notify_change()

        resumed = f" (resuming at {_fmt(offset)})" if offset else ""
        return f"Playing **{describe(item)}**{resumed}"

    async def _await_load(self, entry_id) -> str | None:
        """Wait for an external load to prove itself. Caller holds the lock.

        Returns an error message if the load failed, or None if it looks fine
        (tracks appeared, or we ran out of patience — a slow link is not a
        failure and shouldn't be reported as one).
        """
        deadline = time.monotonic() + EXTERNAL_LOAD_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            failed = self._last_end_file
            if failed and failed[0] == entry_id and failed[1] == "error":
                detail = failed[2] or "it wouldn't play"
                log.warning("External load failed for entry %s: %s", entry_id, detail)
                self.current = None
                self.idle = True
                await self.show_idle_screen()
                await self._notify_change()
                return f"That wouldn't play — {detail}."
            if await self._aget("track_list"):
                return None
        log.debug("External load unconfirmed after %ss; assuming it's just slow",
                  EXTERNAL_LOAD_TIMEOUT)
        return None

    async def _add_external_subs(self, item) -> None:
        subs = await asyncio.to_thread(self.lib.external_subtitles, item)
        for sub in subs[:8]:
            await self._acmd(
                "sub-add", sub["url"], "auto", sub["title"] or sub["lang"], sub["lang"]
            )

    async def pause(self) -> str:
        # Cheap check first — no point paying for a round trip to tell someone
        # nothing is playing.
        if self.current is None or not await self.is_alive():
            return "Nothing is playing."
        await self._aset("pause", True)
        self.paused = True
        self._report_later(self.current, self.position, "paused")
        await self._notify_change()
        return "Paused."

    async def resume(self) -> str:
        if self.current is None or not await self.is_alive():
            return "Nothing is loaded."
        await self._aset("pause", False)
        self.paused = False
        await self._notify_change()
        return "Playing."

    async def toggle(self) -> str:
        return await (self.resume() if self.paused else self.pause())

    async def stop(self) -> str:
        async with self._lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> str:
        if not await self.is_alive():
            return "Nothing is playing."
        self._report_later(self.current, self.position, "stopped")
        self.queue.clear()
        await self._acmd("stop")
        self.current = None
        self.idle = True
        self.position = 0.0
        await self.show_idle_screen()
        await self._notify_change()
        return "Stopped and cleared the queue."

    async def skip(self) -> str:
        """Skip to the next queued item (or next episode)."""
        async with self._lock:
            if self.current is None and not self.queue:
                return "Nothing to skip to."
            finished = self.current
            next_item = await self._pop_next(finished)
            if next_item is None:
                return await self._stop_locked()
            return await self._play_locked(next_item, offset=0.0)

    async def seek(self, delta_seconds: float) -> str:
        if self.current is None or not await self.is_alive():
            return "Nothing is playing."
        if getattr(self.current, "is_live", False):
            return "That's a live stream — there's nothing to skip through."
        if not await self._acmd("seek", delta_seconds, "relative"):
            return "Seek failed."
        direction = "Forward" if delta_seconds >= 0 else "Back"
        return f"{direction} {_fmt(abs(delta_seconds))}."

    async def seek_to(self, seconds: float) -> str:
        if self.current is None or not await self.is_alive():
            return "Nothing is playing."
        if getattr(self.current, "is_live", False):
            return "That's a live stream — there's no position to jump to."
        if not await self._acmd("seek", max(0.0, seconds), "absolute"):
            return "Seek failed."
        return f"Jumped to {_fmt(seconds)}."

    async def set_speed(self, rate: float) -> str:
        """Variable-speed playback. 1.0 is normal, 2.0 is double, 0.5 is half."""
        if self.current is None or not await self.is_alive():
            return "Nothing is playing."
        asked = float(rate)
        rate = max(0.1, min(8.0, asked))
        if not await self._aset("speed", rate):
            return "Couldn't change playback speed."
        clamped = "" if rate == asked else f" (asked for {asked:g}x, clamped to 0.1–8x)"
        self.speed = rate
        # The watchdog measures progress against wall clock; at high speed the
        # position moves fine, but make sure a slow rate isn't read as a stall.
        self._last_progress_at = time.monotonic()
        await self._notify_change()
        if rate == 1.0:
            return f"Back to normal speed.{clamped}"
        return f"Playing at {rate:g}x.{clamped}"

    async def park(self) -> str | None:
        """Set the current video aside for later and show the idle screen.

        Used when switching to music: the screen shouldn't sit on a frozen
        frame of a half-watched movie.
        """
        async with self._lock:
            if self.current is None:
                return None
            item, position = self.current, self.position
            self._parked = (item, position)
            self._report_later(item, position, "paused")
            self.current = None
            self.idle = True
            self.position = 0.0
            await self.show_idle_screen()
            await self._notify_change()
            return f"{describe(item)} at {_fmt(position)}"

    @property
    def has_parked(self) -> bool:
        return bool(self._parked)

    @property
    def parked_description(self) -> str | None:
        """What is set aside, in words — or None.

        The model needs the title, not just a flag: told only that *something*
        is parked, "put the Dune movie back on" becomes a fresh library search
        that can easily land on a different cut of the same film.
        """
        if not self._parked:
            return None
        item, position = self._parked
        return f"{describe(item)} at {_fmt(position)}"

    @property
    def would_start_now(self) -> bool:
        """Whether queueing something right now would start it playing.

        False while a video is parked for music: the queue should fill up
        quietly and wait for *resume video*, not hijack the screen.
        """
        return self.current is None and not self._parked and not self.queue

    async def unpark(self) -> str:
        """Resume whatever was set aside by park()."""
        async with self._lock:
            if not self._parked:
                return "Nothing was paused."
            item, position = self._parked
            self._parked = ()
            return await self._play_locked(item, offset=max(0.0, position - 5))

    async def restart_current(self) -> str:
        async with self._lock:
            if self.current is None:
                return "Nothing is playing."
            return await self._play_locked(self.current, offset=0.0)

    # ---------- queue ----------

    async def queue_add(self, item) -> str:
        """Append to the queue. This is now just a list append — no PlayQueue
        rebuild, nothing for the player to lose track of."""
        async with self._lock:
            if self.would_start_now:
                return await self._play_locked(item)
            if getattr(item, "external", False):
                # The queue holds Plex rating keys and _pop_next fetches them
                # from the server; a YouTube video has no key to store.
                return (
                    f"I can't queue **{describe(item)}** — YouTube plays "
                    f"immediately only. Say *play* to start it now."
                )
            self.queue.append(int(item.ratingKey))
            position = f"#{len(self.queue)} up"
            if self._parked:
                return (
                    f"Added **{describe(item)}** to the queue ({position}) — "
                    f"say *resume video* to start watching."
                )
            return f"Added **{describe(item)}** to the queue ({position})."

    async def queue_remove(self, index: int) -> str:
        async with self._lock:
            if index < 1 or index > len(self.queue):
                return f"The queue has {len(self.queue)} items."
            key = self.queue.pop(index - 1)
        try:
            item = await asyncio.to_thread(self.lib.fetch, key)
            return f"Removed **{describe(item)}** from the queue."
        except Exception:
            return "Removed that item from the queue."

    async def queue_clear(self) -> str:
        async with self._lock:
            count = len(self.queue)
            self.queue.clear()
        return f"Cleared {count} queued item(s). Current playback is untouched."

    async def queue_titles(self) -> list[str]:
        out = []
        for key in self.queue[:15]:
            try:
                out.append(describe(await asyncio.to_thread(self.lib.fetch, key)))
            except Exception:
                out.append(f"(missing item {key})")
        return out

    # ---------- tracks ----------

    def _track_list_blocking(self, retries: int = 4) -> list[dict]:
        """mpv's track list. Retries briefly because tracks can lag file-loaded.

        Thread context only — the retry sleeps total 2.5s.
        """
        if not self._alive():
            return []
        for attempt in range(retries):
            rows = self._get("track_list") or []
            if rows:
                return rows
            time.sleep(0.25 * (attempt + 1))
        return []

    async def track_list(self, retries: int = 4) -> list[dict]:
        return await asyncio.to_thread(self._track_list_blocking, retries)

    async def subtitle_tracks(self) -> list[dict]:
        return [t for t in await self.track_list() if t.get("type") == "sub"]

    async def audio_tracks(self) -> list[dict]:
        return [t for t in await self.track_list() if t.get("type") == "audio"]

    def current_library(self) -> str | None:
        return getattr(self.current, "librarySectionTitle", None)

    async def apply_track_prefs(self) -> str:
        """Pick audio and subtitle tracks per this library's preferences."""
        audio_lang, sub_lang = self.prefs.for_library(self.current_library())
        return await self.apply_languages(audio_lang, sub_lang)

    async def apply_languages(self, audio_lang: str | None, sub_lang: str | None) -> str:
        rows = await self.track_list()
        if not rows:
            return "No tracks to choose from."

        chosen_audio = tk.selected_language(rows, "audio")
        aid = tk.pick_audio(rows, audio_lang)
        if aid is not None and await self._aset("aid", aid):
            picked = next(
                (t for t in rows if int(t.get("id") or 0) == aid and t.get("type") == "audio"),
                None,
            )
            if picked is not None:
                chosen_audio = tk.track_language(picked)

        sid = tk.pick_subtitle(rows, sub_lang, chosen_audio)
        await self._aset("sid", sid)

        self.audio_language = chosen_audio
        self.subtitle_language = None if sid == "no" else tk.normalize_lang(sub_lang)
        log.info(
            "Tracks: audio=%s subtitles=%s (library=%s)",
            tk.display_lang(chosen_audio),
            tk.display_lang(self.subtitle_language),
            self.current_library(),
        )
        return (
            f"Audio: {tk.display_lang(chosen_audio)} · "
            f"Subtitles: {tk.display_lang(self.subtitle_language)}"
        )

    async def set_audio_language(self, lang: str, remember: bool = True) -> str:
        if self.current is None:
            return "Nothing is playing."
        _, sub_lang = self.prefs.for_library(self.current_library())
        if remember:
            self.prefs.set(self.current_library(), audio=lang)
        result = await self.apply_languages(lang, sub_lang)
        scope = f" for {self.current_library()}" if remember and self.current_library() else ""
        return f"{result}{scope}"

    async def set_subtitle_language(self, lang: str, remember: bool = True) -> str:
        if self.current is None:
            return "Nothing is playing."
        audio_lang, _ = self.prefs.for_library(self.current_library())
        if remember:
            self.prefs.set(self.current_library(), subs=lang)
        result = await self.apply_languages(audio_lang, lang)
        scope = f" for {self.current_library()}" if remember and self.current_library() else ""
        return f"{result}{scope}"

    async def set_subtitle(self, track_id) -> str:
        """Explicit track id, bypassing language preferences."""
        if not await self.is_alive():
            return "Nothing is playing."
        if track_id in (None, "off", "no", 0, "0"):
            if not await self._aset("sid", "no"):
                return "Couldn't switch subtitle track."
            self.subtitle_language = None
            return "Subtitles off."
        try:
            sid = int(track_id)
        except (TypeError, ValueError):
            return "Couldn't switch subtitle track."
        if not await self._aset("sid", sid):
            return "Couldn't switch subtitle track."
        return "Subtitles on."

    async def set_audio(self, track_id) -> str:
        if not await self.is_alive():
            return "Nothing is playing."
        try:
            aid = int(track_id)
        except (TypeError, ValueError):
            return "Couldn't switch audio track."
        if not await self._aset("aid", aid):
            return "Couldn't switch audio track."
        return "Audio track switched."

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------

    def _alive(self) -> bool:
        """Blocking liveness probe — a real IPC round trip. Thread context only."""
        if self.mpv is None:
            return False
        try:
            _ = self.mpv.idle_active
            return True
        except Exception:
            return False

    async def show_window(self) -> bool:
        """Bring mpv's window back and put it in front.

        Not done through wm/macctl, and on macOS it CANNOT be: mpv registers no
        windows with the Accessibility API at all. `count of windows of process
        "mpv"` is 0 while a film is plainly playing fullscreen — verified
        against a plain `mpv` launched by hand, so it is mpv's nature and not
        how the bot starts it. find_window(title=MPV_WINDOW_TITLE) therefore
        always returned None here and _show_video_window silently did nothing,
        which is why asking for a film while music played left the music
        running and the film audible behind Spotify.

        mpv's own IPC has what the window manager could not offer — the socket
        is already open, no Accessibility grant is involved, and there is no
        window-list race. Raising the process still needs AX, but that works on
        the process even with no windows on it.
        """
        if not await self.is_alive():
            return False
        # Minimise and restore, rather than just restoring.
        #
        # This is what gets rid of the mouse pointer, and it is the only thing
        # that does. mpv is told --cursor-autohide=always and hides the pointer
        # correctly when it starts, but once anything makes the pointer visible
        # again it never re-hides it — autohide is driven by motion, and a
        # pointer that has stopped moving generates none. A remote session
        # disconnecting leaves exactly that: a visible pointer, stranded, on top
        # of the film. Cycling the window state makes mpv re-assert the hide.
        #
        # What NOT to do, learned the hard way: move the pointer. An earlier
        # version warped it into the middle of the window on the theory that
        # mpv could only hide it there. Moving the pointer is precisely what
        # makes macOS show it, and mpv does not take it back — so that "fix"
        # reliably turned an invisible pointer into a visible one, in the worst
        # possible place. wm.park_cursor still exists for callers that want the
        # pointer somewhere specific, but the video path must not call it.
        if await self._aget("window-minimized") is False:
            await self._aset("window-minimized", True)
            await asyncio.sleep(0.35)
        await self._aset("window-minimized", False)
        await self.ensure_fullscreen()
        await asyncio.to_thread(wm.focus_process, "mpv")
        # Report what actually happened rather than that the calls were made.
        # The Windows side learned this the hard way: restore/bring_to_front
        # can both fail silently, and a karaoke reply once said "lyrics on the
        # mpv window" while mpv sat minimised. Ask mpv, which knows.
        return await self._aget("window-minimized") is False

    async def hide_window(self) -> None:
        """Get mpv out of the way so Spotify can be seen. Same reasoning."""
        if not await self.is_alive():
            return
        await self._aset("window-minimized", True)

    async def ensure_fullscreen(self) -> None:
        """Re-assert fullscreen after the window has been away.

        `fullscreen` is passed once, at launch, and mpv is launched once and
        reused for every file. Minimising it for Spotify and bringing it back
        with SW_RESTORE returns the window but not mpv's own fullscreen state,
        so a stream started after a music detour arrived windowed.

        Cheap and idempotent — setting it when it is already set costs nothing.
        """
        if not config.MPV_FULLSCREEN:
            return
        if not await self.is_alive():
            return
        await self._aset("fullscreen", True)

    async def is_alive(self) -> bool:
        if self.mpv is None:
            return False
        return await self._call(self._alive, default=False, label="liveness probe")

    async def _ensure_alive(self) -> bool:
        if await self.is_alive():
            return True
        return await self._restart_mpv()

    async def restart_player(self) -> bool:
        """Relaunch mpv and pick up where we left off."""
        item, at = self.current, self.position
        if not await self._restart_mpv():
            return False
        if item is not None:
            await self.play(item, offset=max(0.0, at - 5))
        return True

    async def _restart_mpv(self) -> bool:
        if self._restarting:
            return False
        self._restarting = True
        try:
            try:
                if self.mpv:
                    await asyncio.to_thread(self.mpv.terminate)
            except Exception:
                pass
            await asyncio.to_thread(self._kill_process)
            self.mpv = None
            for attempt in range(1, 4):
                try:
                    await asyncio.to_thread(self.start)
                    log.info("mpv restarted (attempt %d)", attempt)
                    self._deaths_in_a_row = 0
                    return True
                except Exception as exc:
                    log.warning("mpv start attempt %d failed: %s", attempt, exc)
                    await asyncio.sleep(3 * attempt)
            log.error(
                "Could not start mpv after 3 attempts. Check that MPV_PATH is "
                "correct and that mpv runs on its own."
            )
            return False
        finally:
            self._restarting = False

    async def _handle_player_death(self) -> None:
        """mpv died. Bring it back and resume where we were."""
        if self._restarting:
            return

        # Back off if mpv keeps dying — restarting in a tight loop just spawns
        # orphan processes and buries the real cause in the log.
        now = time.monotonic()
        self._deaths_in_a_row = (
            self._deaths_in_a_row + 1 if now - self._last_death_at < 30 else 1
        )
        self._last_death_at = now
        if self._deaths_in_a_row > 3:
            await self._notice(
                f"mpv has died {self._deaths_in_a_row} times in quick succession, so I've "
                f"stopped relaunching it. Try running mpv by hand to see why, then /restart."
            )
            return
        if self._deaths_in_a_row > 1:
            await asyncio.sleep(5 * self._deaths_in_a_row)

        was_playing = self.current
        resume_at = self.position
        log.warning(
            "Player died during %s at %s — restarting",
            describe(was_playing),
            _fmt(resume_at),
        )
        if not await self._restart_mpv():
            await self._notice("mpv died and wouldn't come back up. Check the console.")
            return
        if was_playing is not None:
            await self.play(was_playing, offset=max(0.0, resume_at - 5))

    async def _watchdog_loop(self) -> None:
        """Catches both hard crashes and soft freezes (stalled stream, hung
        decode) — the black-screen case that used to need a manual kick."""
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                if self._restarting:
                    continue

                if not await self.is_alive():
                    await self._handle_player_death()
                    continue

                if self.current is None or self.paused:
                    self._last_progress_at = time.monotonic()
                    continue

                stalled = self.position == self._last_progress_value
                if not stalled:
                    # Loading a file resets _last_progress_value to the -1
                    # sentinel, so the first pass after any load looks like
                    # progress. Counting that as recovery would clear the
                    # counter after every freeze reload and defeat the cap —
                    # only a move between two real observations counts.
                    progressed = self._last_progress_value >= 0.0
                    self._last_progress_value = self.position
                    self._last_progress_at = time.monotonic()
                    if progressed:
                        self._freeze_reloads = 0
                elif time.monotonic() - self._last_progress_at > config.FREEZE_TIMEOUT:
                    self._last_progress_at = time.monotonic()
                    self._freeze_reloads += 1
                    if self._freeze_reloads > MAX_FREEZE_RELOADS:
                        # A stream that never connects emits no end-file at all,
                        # so nothing else will ever stop this.
                        async with self._lock:
                            await self._give_up_locked(
                                f"Gave up on {describe(self.current)} — it stalled and "
                                f"{MAX_FREEZE_RELOADS} reload attempts didn't help. "
                                f"The file may be unreachable."
                            )
                        continue
                    log.warning(
                        "Playback frozen at %s — reloading (attempt %d/%d)",
                        _fmt(self.position), self._freeze_reloads, MAX_FREEZE_RELOADS,
                    )
                    async with self._lock:
                        item, at = self.current, self.position
                        if item is not None:
                            # A live stream has no meaningful stored position:
                            # reloading "5 seconds ago" either lands outside the
                            # DVR window or walks steadily further behind on
                            # every retry. Rejoin at the live edge instead.
                            live = getattr(item, "is_live", False)
                            resume = 0.0 if live else max(0.0, at - 5)
                            await self._play_locked(item, offset=resume)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Watchdog error")

    async def _progress_loop(self) -> None:
        """Poll mpv locally (cheap) and push progress to Plex every ~10s."""
        tick = 0
        while True:
            try:
                await asyncio.sleep(PROGRESS_INTERVAL)
                tick += 1

                snapshot = await self._call(self._poll, default=None, label="progress poll")
                if snapshot is None:
                    continue
                pos, paused, dur = snapshot

                if pos is not None:
                    self.position = float(pos)
                if dur:
                    self.duration = float(dur)
                if paused != self.paused:
                    self.paused = paused
                    await self._notify_change()

                if tick % 5 == 0 and self.current is not None and not self.paused:
                    await asyncio.to_thread(
                        self.lib.report_progress, self.current, self.position, "playing"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Progress loop error")

    # ------------------------------------------------------------------

    def _fire(self, coro) -> None:
        """Run something in the background without making the user wait for it.
        Keeps a reference so the task isn't garbage collected mid-flight."""
        task = self.loop.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _report_later(self, item, position: float, state: str) -> None:
        # Nothing outside Plex has a timeline to report to.
        if item is None or getattr(item, "external", False):
            return
        self._fire(asyncio.to_thread(self.lib.report_progress, item, position, state))

    async def _notify_change(self) -> None:
        if self.on_change is None:
            return
        self._fire(self._safe_notify())

    async def _safe_notify(self) -> None:
        try:
            await self.on_change(self)
        except Exception:
            log.exception("on_change callback failed")

    def status(self) -> dict:
        return {
            "playing": self.current is not None,
            "paused": self.paused,
            "title": describe(self.current) if self.current else None,
            "position": _fmt(self.position),
            "duration": _fmt(self.duration),
            "remaining": _fmt(max(0.0, self.duration - self.position)),
            "queue_length": len(self.queue),
            "audio_language": tk.display_lang(self.audio_language),
            "subtitle_language": tk.display_lang(self.subtitle_language),
            "library": self.current_library(),
            "speed": self.speed,
        }


def _fmt(seconds: float | None) -> str:
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
