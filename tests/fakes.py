"""Shared stand-ins so the tests need no Plex, Spotify, mpv or Discord.

Every fake here mimics only the surface the bot actually touches.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IPC_DELAY = 0.0  # bump in a test to simulate a slow pipe


class FakeItem:
    """A plexapi movie/episode, near enough."""

    def __init__(self, key=1, title="Dune", year=2021, kind="movie",
                 duration=9_000_000, library="Movies"):
        self.ratingKey = key
        self.title = title
        self.year = year
        self.type = kind
        self.duration = duration
        self.librarySectionTitle = library
        self.viewOffset = 0

    def __repr__(self):
        return f"<FakeItem {self.ratingKey} {self.title!r}>"


class FakeMPV:
    """Records every property write and command; optionally slow."""

    def __init__(self, tracks=None, fail_load=False):
        object.__setattr__(self, "_tracks", tracks or [])
        object.__setattr__(self, "_fail_load", fail_load)
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "_entry", 0)
        object.__setattr__(self, "loads", [])

    def __getattr__(self, name):
        if IPC_DELAY:
            time.sleep(IPC_DELAY)
        if name == "track_list":
            return self._tracks
        if name == "time_pos":
            return 10.0
        if name == "duration":
            return 100.0
        if name in ("pause", "idle_active"):
            return False
        return None

    def __setattr__(self, name, value):
        if IPC_DELAY:
            time.sleep(IPC_DELAY)
        self.calls.append(("set", name, value))

    def command(self, *args):
        if IPC_DELAY:
            time.sleep(IPC_DELAY)
        self.calls.append(("cmd",) + args)
        if args and args[0] == "loadfile":
            if self._fail_load:
                raise RuntimeError("pipe closed")
            object.__setattr__(self, "_entry", self._entry + 1)
            self.loads.append(args[1])
            return {"playlist_entry_id": self._entry}
        return None

    @property
    def entry_id(self):
        return self._entry

    def sets(self, name):
        return [c[2] for c in self.calls if c[0] == "set" and c[1] == name]


class FakeLib:
    """The slice of Library that Player uses."""

    def __init__(self, items=None, url="http://plex/file.mkv"):
        self.items = {i.ratingKey: i for i in (items or [])}
        self.url = url
        self.marked_played = []
        self.progress = []

    def stream_url(self, item):
        return self.url

    def resume_offset(self, item):
        return 0.0

    def external_subtitles(self, item):
        return []

    def report_progress(self, item, position, state):
        self.progress.append((getattr(item, "title", None), state))

    def mark_played(self, item):
        self.marked_played.append(item.title)

    def fetch(self, key):
        if int(key) not in self.items:
            raise KeyError(key)
        return self.items[int(key)]

    def next_episode(self, episode):
        return None


class FakeSpotify:
    """Tracks whether the video/music handoff actually reached Spotify."""

    def __init__(self, enabled=True, connected=True):
        self.enabled = enabled
        self.sp = object() if connected else None
        self.paused = 0
        self.played = []
        # Mirrors SpotifyController: Controls.state() reads both.
        self.playing = False
        self.last_label = ""

    def pause(self):
        self.paused += 1
        self.playing = False
        return "Music paused."

    def resume(self):
        self.playing = True
        return "Music playing."

    def play_uri(self, uri, label="", kind=""):
        self.played.append(uri)
        self.playing = True
        self.last_label = label or uri
        return f"Playing {label}."

    def queue_uri(self, uri, label="", kind=""):
        return f"Queued {label}."

    def search_options(self, query):
        return [], None

    @staticmethod
    def kind_from_uri(uri):
        parts = (uri or "").split(":")
        return parts[1] if len(parts) >= 3 and parts[0] == "spotify" else ""

    def play_link(self, uri):
        return self.play_uri(uri, uri, self.kind_from_uri(uri))

    def queue_link(self, uri):
        return self.queue_uri(uri, uri, self.kind_from_uri(uri))

    def now_playing(self):
        if not self.playing:
            return "Nothing playing on Spotify."
        return f"**Playing on Spotify:** {self.last_label}"

    def clear_queue(self):
        return "Cleared the queue."


def make_player(lib=None, mpv=None, loop=None):
    """A Player wired to fakes, without touching the real mpv or Plex."""
    import asyncio

    from player import Player

    player = Player(lib or FakeLib(), loop or asyncio.get_event_loop())
    player.mpv = mpv or FakeMPV()
    return player
