"""Spotify control via the Web API, targeting the desktop app on this machine.

The desktop app registers itself as a Spotify Connect device, so we can drive
it remotely without touching its window. Audio reaches viewers through the same
screen share as everything else.

Playback control requires Spotify Premium — the API returns 403 for free
accounts. media_key() below is the fallback: it works for anyone, but only for
play/pause/next/previous, with no search.

All calls here are blocking. Call them from a thread.
"""

import difflib
import logging
import random
import os
import re
import shlex
import subprocess
import sys
import time

import config

log = logging.getLogger("athena.spotify")

# spotify:track:4uLU6hMCjMI75M1A2tKUQC and friends. Used to refuse a uri before
# it reaches the API rather than after — see play_uri.
_PLAYABLE_URI = re.compile(
    r"^spotify:(?:track|album|artist|playlist|show|episode):[A-Za-z0-9]{16,}$"
)


def playable_uri(uri: str) -> bool:
    return bool(_PLAYABLE_URI.match((uri or "").strip()))


def _premium_required(exc: Exception) -> bool:
    """Is this 403 actually about the account's plan, or something else —
    no active device, a Connect restriction, an unsupported action on the
    current context? Every 403 used to be treated as "not Premium" here,
    which latches self.premium = False for the rest of the running
    process. Measured live 2026-08-14: resuming with no active device
    ("Player command failed: Restriction violated") tripped that on a
    genuinely Premium account, and silently broke queueing for the rest
    of the session — a device-state error, not a plan one. Spotify's own
    403 for an actual plan issue says so ("Premium required").
    """
    return "premium" in str(exc).lower()


# open.spotify.com/track/<id>, .../playlist/<id>, etc. Spotify sometimes
# inserts a locale segment (intl-en/) and always appends a share query
# string (?si=...) — the id's character class stops at the first
# non-alphanumeric on its own, so nothing extra needs stripping.
_WEB_URL = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?"
    r"(?P<kind>track|album|artist|playlist|show|episode)/(?P<id>[A-Za-z0-9]{16,})"
)


def find_uri(text: str) -> str | None:
    """A Spotify link (open.spotify.com/..., anywhere in the text — "play
    <link>" works the same as a bare paste) or an already-bare
    spotify:type:id uri, normalized to a uri. None if there isn't one.
    """
    text = (text or "").strip()
    if playable_uri(text):
        return text
    match = _WEB_URL.search(text)
    if not match:
        return None
    return f"spotify:{match.group('kind')}:{match.group('id')}"

# user-read-private is what makes the account's `product` field visible. Without
# it the field is simply absent, which is not the same as the account being free.
# user-library-read and user-follow-read were missing, which presented as
# endpoints looking deprecated when they were not: /me/tracks, /me/albums and
# /me/following all answered 403 "Insufficient client scope". That is a
# different failure from the ones Spotify genuinely closed to new apps
# (artist top-tracks, related-artists, browse categories and featured
# playlists all return a plain 403 Forbidden, and recommendations is a 404).
#
# CHANGING THIS INVALIDATES THE CACHED TOKEN. The next start needs an
# interactive browser round trip, which a hidden scheduled task cannot do —
# run scripts/reauth-spotify.ps1 by hand first.
SCOPES = (
    "user-read-private user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing playlist-read-private playlist-read-collaborative "
    "user-library-read user-follow-read"
)

# Windows virtual key codes for the media keys.
_VK = {"playpause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}

# The macOS equivalent, and a straight upgrade on the Windows one. A media key
# is a system-wide broadcast that lands on whichever app most recently claimed
# it — so on Windows this can pause a browser video instead of Spotify, and
# there is nothing to be done about it. AppleScript addresses Spotify by name,
# so it always reaches the right app and never touches anything else.
_APPLESCRIPT = {
    "playpause": "playpause",
    "next": "next track",
    "previous": "previous track",
    "stop": "pause",  # Spotify's dictionary has no `stop`; pause is the nearest
}


def _media_key_windows(name: str) -> bool:
    try:
        import ctypes

        code = _VK[name]
        ctypes.windll.user32.keybd_event(code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(code, 0, 2, 0)  # KEYEVENTF_KEYUP
        return True
    except Exception:
        log.exception("Media key press failed")
        return False


def _media_key_macos(name: str) -> bool:
    """Drive Spotify directly. Needs Automation permission, not Accessibility.

    Those are separate grants in System Settings and this is the one that gets
    missed, because window control asks for the other. macOS prompts for it the
    first time, once, and refuses silently forever after if that prompt was
    dismissed — hence the explicit complaint rather than a bare False.
    """
    script = f'tell application "Spotify" to {_APPLESCRIPT[name]}'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not reach Spotify via AppleScript: %s", exc)
        return False
    if result.returncode != 0:
        log.warning(
            "Spotify refused an AppleScript command (%s). If this says 'Not "
            "authorized', grant Automation permission under System Settings > "
            "Privacy & Security > Automation.",
            result.stderr.strip(),
        )
        return False
    return True


def media_key(name: str) -> bool:
    """Transport control without Premium and without the Web API.

    Same contract on both platforms: play/pause/next/previous only, no search,
    and it drives whatever the desktop app is already doing.
    """
    if name not in _VK:
        return False
    if sys.platform == "darwin":
        return _media_key_macos(name)
    if os.name == "nt":
        return _media_key_windows(name)
    return False


class SpotifyController:
    def __init__(self):
        self.enabled = bool(config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET)
        self.sp = None
        self.device_id = None
        self.premium = True  # until proven otherwise
        self.playing = False
        # Last thing we started, so the model can be told what "the music"
        # refers to without a network round trip on every request.
        self.last_label = ""

    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Authorise. First run opens a browser on this machine; the token is
        cached afterwards and refreshed automatically."""
        if not self.enabled:
            log.info("Spotify not configured — skipping")
            return False
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth

            auth = SpotifyOAuth(
                client_id=config.SPOTIFY_CLIENT_ID,
                client_secret=config.SPOTIFY_CLIENT_SECRET,
                redirect_uri=config.SPOTIFY_REDIRECT_URI,
                scope=SCOPES,
                cache_path=config.SPOTIFY_CACHE,
                open_browser=True,
            )
            self.sp = spotipy.Spotify(auth_manager=auth, requests_timeout=10)
            user = self.sp.current_user()
            product = user.get("product")
            # Only a definite "free" disables features. An absent product field
            # means we couldn't see it, not that the account lacks Premium — and
            # a real 403 will demote us later anyway.
            self.premium = product != "free"
            log.info(
                "Spotify connected as %s (plan: %s)",
                user.get("display_name") or user.get("id"),
                product or "unknown",
            )
            if product == "free":
                log.warning(
                    "Spotify account is not Premium — search and play are "
                    "unavailable; falling back to media keys for transport."
                )
            return True
        except Exception:
            log.exception("Spotify authorisation failed")
            self.sp = None
            return False

    # ------------------------------------------------------------------

    def _devices(self) -> list[dict]:
        try:
            return (self.sp.devices() or {}).get("devices", [])
        except Exception:
            log.exception("Could not list Spotify devices")
            return []

    def _pick_device(self, devices: list[dict]) -> dict | None:
        wanted = (config.SPOTIFY_DEVICE_NAME or "").strip().lower()
        if wanted:
            for d in devices:
                if wanted in (d.get("name") or "").lower():
                    return d
        for d in devices:
            if d.get("is_active"):
                return d
        for d in devices:
            if d.get("type") == "Computer":
                return d
        return devices[0] if devices else None

    def ensure_device(self) -> str | None:
        """Find the desktop app, launching it if it isn't running yet."""
        if self.sp is None:
            return None
        device = self._pick_device(self._devices())
        if device is None and config.SPOTIFY_EXE:
            log.info("No Spotify device found — launching the app")
            # Parsed as a command line, not a bare path: the macOS default is
            # `open -a Spotify`, because an .app bundle is a directory and
            # cannot be exec'd. shlex handles a Windows path with spaces in it
            # correctly as long as it is quoted, which is how anyone would
            # write it anyway.
            try:
                argv = shlex.split(config.SPOTIFY_EXE, posix=(os.name != "nt"))
                subprocess.Popen(argv or [config.SPOTIFY_EXE], close_fds=True)
            except Exception:
                log.exception("Could not launch Spotify from %s", config.SPOTIFY_EXE)
            for _ in range(20):  # the app takes a few seconds to register
                time.sleep(1)
                device = self._pick_device(self._devices())
                if device:
                    break
        if device is None:
            return None
        self.device_id = device.get("id")
        return self.device_id

    # ------------------------------------------------------------------

    _PREFIXES = (
        ("album ", "album"), ("playlist ", "playlist"), ("artist ", "artist"),
        ("track ", "track"), ("song ", "track"), ("band ", "artist"),
    )

    def _strip_prefix(self, query: str) -> tuple[str, str | None]:
        q = query.strip()
        for prefix, kind in self._PREFIXES:
            if q.lower().startswith(prefix):
                return q[len(prefix):].strip(), kind
        return q, None

    @staticmethod
    def _label(item: dict, kind: str) -> str:
        name = item.get("name") or "?"
        if kind == "track":
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            return f"{name} — {artists}" if artists else name
        if kind == "album":
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            return f"{name} — {artists}" if artists else name
        if kind == "playlist":
            owner = (item.get("owner") or {}).get("display_name") or ""
            return f"{name} — {owner}" if owner else name
        return name

    # Greedy title, so the split happens at the LAST "by" — "Stand By Me by
    # Ben E. King" has to keep its title intact.
    _BY = re.compile(r"^(?P<title>.+)\s+by\s+(?P<artist>\S.*)$", re.I)

    def _field_query(self, q: str, forced: str | None) -> tuple[str, str] | None:
        """Turn "<title> by <artist>" into Spotify field filters.

        Spotify matches "by" as an ordinary search word, which buries the
        intended result: "sometimes by one true god" ranks a GOSPEL READY
        track first, while track:"sometimes" artist:"one true god" returns the
        right song as the only hit.

        Returns (query, title, artist) or None.
        """
        m = self._BY.match(q.strip())
        if not m:
            return None
        title, artist = m.group("title").strip(), m.group("artist").strip()
        if not title or not artist:
            return None
        # "artist X by Y" is incoherent; leave anything artist-forced alone.
        if forced == "artist":
            return None
        field = "album" if forced == "album" else "track"
        return f'{field}:"{title}" artist:"{artist}"', title, artist

    @staticmethod
    def _artist_matches(item: dict, wanted: str) -> bool:
        """Is this result actually by roughly the artist that was asked for?

        Guards the plain-text fallback. "friday by vanessa black" finds nothing
        under a field filter — she has no such track — and free-text search
        then happily returns "Friday Night" by Eric Paslay. Playing a different
        artist's song and calling it a success is worse than admitting a miss.
        Deliberately loose, so a misspelled artist ("streeghtlight manifesto")
        still matches its real one.
        """
        names = ", ".join(a.get("name", "") for a in item.get("artists", []) if a)
        if not names:
            return True  # playlists and the like carry no artist to check
        wanted, found = wanted.lower().strip(), names.lower()
        if wanted in found or found in wanted:
            return True
        return difflib.SequenceMatcher(None, wanted, found).ratio() >= 0.6

    def genre_playlist(self, genre: str) -> tuple[dict | None, str | None]:
        """A playlist for a genre or mood, rather than a track named after one.

        "queue up some EDM" used to run a TRACK search for the literal phrase
        and return titles that happen to contain the words, which is how "some
        EDM" produced "Some Chords — deadmau5".

        Playlist search is used because the obvious alternatives are gone:
        /recommendations is a 404 for new apps, and browse categories,
        featured playlists and artist top-tracks all answer 403 Forbidden.
        Measured working: playlist search, and genre:"x" track search.

        Returns (option, note) in the shape play_uri/queue_uri expect. A random
        pick from the top few rather than always the first — the top result for
        a genre is the same list every time, which gets old.
        """
        if self.sp is None:
            return None, "Spotify isn't set up."
        genre = (genre or "").strip()
        if not genre:
            return None, None
        try:
            found = self.sp.search(q=genre, type="playlist", limit=10)
            items = [p for p in (found.get("playlists", {}).get("items") or []) if p]
        except Exception:
            log.exception("Playlist search failed for %r", genre)
            return None, None
        if not items:
            return None, None
        chosen = random.choice(items[:5])
        owner = (chosen.get("owner") or {}).get("display_name") or ""
        label = chosen.get("name") or genre
        if owner:
            label = f"{label} — {owner}"
        return {"uri": chosen["uri"], "label": label, "kind": "playlist"}, None

    def genre_tracks(self, genre: str, limit: int = 10) -> list[dict]:
        """Individual tracks for a genre, for queueing.

        A playlist cannot be queued: /playlists/{id}/items answers 403 for this
        app even for playlists the account OWNS, so there is no way to expand
        one into tracks. genre:"x" track search is the route that still works.

        Uses the genre: field rather than the bare word — searching "edm" as
        text returns titles containing the letters, including The Wreck of the
        Edmund Fitzgerald.
        """
        if self.sp is None:
            return []
        genre = (genre or "").strip()
        if not genre:
            return []
        try:
            found = self.sp.search(q=f'genre:"{genre}"', type="track", limit=limit)
            items = found.get("tracks", {}).get("items") or []
        except Exception:
            log.exception("Genre track search failed for %r", genre)
            return []
        out = []
        for track in items:
            if not track or not track.get("uri"):
                continue
            artists = ", ".join(a["name"] for a in track.get("artists", [])[:2])
            label = f"{track['name']} — {artists}" if artists else track["name"]
            out.append({"uri": track["uri"], "label": label, "kind": "track"})
        return out

    def search_options(self, query: str) -> tuple[list[dict], str | None]:
        """Find what the user might mean.

        Returns (candidates, note). A single candidate means we're confident.
        Several means the name is ambiguous — 'Toxicity' is both a song and an
        album, 'Wonderwall' is both a song and a band — and someone should pick.
        """
        if self.sp is None:
            return [], "Spotify isn't set up."
        q, forced = self._strip_prefix(query)
        if not q:
            return [], None

        types = [forced] if forced else ["track", "album", "artist", "playlist"]

        results = None
        lowered = q.lower()
        # Set only when we asked for a specific artist and had to fall back to
        # free-text search, which is where a wrong artist can creep in.
        wanted_artist = None
        narrowed = self._field_query(q, forced)
        if narrowed:
            # Only tracks and albums: someone naming an artist after "by" wants
            # their song, not the artist page.
            narrow_types = [forced] if forced else ["track", "album"]
            filtered_query, title, artist = narrowed
            wanted_artist = artist
            try:
                candidate = self.sp.search(
                    q=filtered_query, type=",".join(narrow_types), limit=5
                )
            except Exception:
                candidate = None
            if candidate and any(
                (candidate.get(t + "s") or {}).get("items") for t in narrow_types
            ):
                log.info("Narrowed Spotify search to %s", filtered_query)
                results, types = candidate, narrow_types
                # Match "exact" against the title alone now, not "<x> by <y>".
                lowered = title.lower()
                wanted_artist = None  # the filter already guaranteed the artist

        if results is None:
            try:
                results = self.sp.search(q=q, type=",".join(types), limit=5)
            except Exception:
                log.exception("Spotify search failed")
                return [], "Spotify search failed."

        def items(kind):
            rows = [i for i in ((results.get(kind + "s") or {}).get("items") or []) if i]
            if wanted_artist:
                rows = [i for i in rows if self._artist_matches(i, wanted_artist)]
            return rows

        def entry(item, kind):
            return {
                "uri": item["uri"],
                "kind": kind,
                "label": self._label(item, kind),
                "exact": (item.get("name") or "").lower() == lowered,
            }

        # An explicit prefix means they already told us what they want.
        if forced:
            top = items(forced)
            return ([entry(top[0], forced)] if top else []), None

        exact, fallback = [], []
        for kind in ("track", "album", "artist", "playlist"):
            rows = items(kind)
            hit = next((r for r in rows if (r.get("name") or "").lower() == lowered), None)
            if hit is not None:
                exact.append(entry(hit, kind))
            elif rows:
                fallback.append(entry(rows[0], kind))

        if len(exact) == 1:
            return exact, None
        if exact:
            return exact[:4], None
        return fallback[:4], None

    @staticmethod
    def kind_from_uri(uri: str) -> str:
        """'spotify:track:xyz' -> 'track'. Free, and always right."""
        parts = (uri or "").split(":")
        return parts[1] if len(parts) >= 3 and parts[0] == "spotify" else ""

    def describe_uri(self, uri: str) -> tuple[str, str]:
        """(label, kind) for a bare URI.

        Models routinely call music_play_uri with only the uri, dropping the
        label they were given moments earlier. Without this the reply shows a
        raw spotify:track:... string instead of a song name.
        """
        kind = self.kind_from_uri(uri)
        if self.sp is None or not kind:
            return "", kind
        lookup = {
            "track": lambda: self.sp.track(uri),
            "album": lambda: self.sp.album(uri),
            "artist": lambda: self.sp.artist(uri),
            "playlist": lambda: self.sp.playlist(uri),
        }.get(kind)
        if lookup is None:
            return "", kind
        try:
            return self._label(lookup(), kind), kind
        except Exception:
            log.debug("Could not look up %s", uri, exc_info=True)
            return "", kind

    def search_list(self, query: str, limit: int = 6) -> tuple[list[dict], str | None]:
        """Flat list of matches for a *search* command — nothing is played.

        Distinct from search_options, which is tuned for disambiguation and
        returns at most one candidate per kind. Here the point is to show what
        exists, so results come back in Spotify's own ranking.
        """
        if self.sp is None:
            return [], "Spotify isn't set up."
        q, forced = self._strip_prefix(query)
        if not q:
            return [], None

        types = [forced] if forced else ["track", "album", "artist", "playlist"]
        # Reuse the "<title> by <artist>" handling — a search for
        # "sometimes by one true god" should be as accurate as playing it.
        narrowed = self._field_query(q, forced)
        search_q = narrowed[0] if narrowed else q
        try:
            results = self.sp.search(q=search_q, type=",".join(types), limit=limit)
        except Exception:
            log.exception("Spotify search failed")
            return [], "Spotify search failed."
        if narrowed and not any(
            (results.get(t + "s") or {}).get("items") for t in types
        ):
            # The filter found nothing; fall back to the words as typed.
            try:
                results = self.sp.search(q=q, type=",".join(types), limit=limit)
            except Exception:
                return [], "Spotify search failed."

        out, seen = [], set()
        for kind in types:
            for item in ((results.get(kind + "s") or {}).get("items") or []):
                if not item:
                    continue
                label = self._label(item, kind)
                # Singles and album versions come back as separate entries with
                # identical labels; two indistinguishable rows help nobody.
                if (kind, label.lower()) in seen:
                    continue
                seen.add((kind, label.lower()))
                out.append({"uri": item["uri"], "kind": kind, "label": label})
        return out[:limit], None

    # Deliberately duplicated as a guard in brain.py's music_play_uri dispatch:
    # that one keeps a bad model argument from getting this far, this one keeps
    # any other caller from reaching the API with nonsense.
    def play_uri(self, uri: str, label: str = "", kind: str = "") -> str:
        """Play something already chosen. Tracks play as a single song; albums,
        artists and playlists play as a context so the music keeps going.

        Starting playback always replaces Spotify's live queue — there's no
        API call that means "play this now but keep what's queued behind
        it". The closest available: read the queue before interrupting it,
        then re-add those tracks after. Spotify's queue endpoint only
        returns up to ~20 upcoming tracks, so a long queue or a big
        playlist context won't survive intact — this covers the ordinary
        case (a handful of queued songs), not every one.
        """
        if self.sp is None:
            return "Spotify isn't set up."
        # An empty or malformed uri used to fall through to the context branch
        # below and reach the API as context_uri="", which Spotify answers with
        # 400 "Invalid context uri" and a traceback in the log — for what is
        # really just a bad argument. Refuse it here instead.
        if not playable_uri(uri):
            log.warning("play_uri refused an unusable uri %r", uri)
            return "That wasn't a playable Spotify link."
        device = self.ensure_device()
        if device is None:
            return "No Spotify device found. Is the desktop app running on that machine?"

        preserved = self._snapshot_queue()

        try:
            if uri.startswith("spotify:track:"):
                self.sp.start_playback(device_id=device, uris=[uri])
            elif uri.startswith("spotify:artist:"):
                self.sp.start_playback(device_id=device, context_uri=uri)
            else:
                self.sp.start_playback(device_id=device, context_uri=uri)
            self.playing = True
            self.last_label = label or uri
        except Exception as exc:
            if _premium_required(exc):
                self.premium = False
                return "Spotify refused — playback control needs Premium."
            log.exception("Spotify playback failed")
            return "Couldn't start Spotify playback."

        # Excludes uri defensively — the snapshot is taken before
        # start_playback, so it shouldn't contain the not-yet-started
        # track, but there's no reason to trust that absolutely.
        restored = self._restore_queue([u for u in preserved if u != uri], device)
        noun = {
            "track": "Song", "album": "Album",
            "artist": "Artist", "playlist": "Playlist",
        }.get(kind, "Now playing")
        result = f"{noun}: **{label or uri}** on Spotify."
        if restored:
            noun_plural = "track" if restored == 1 else "tracks"
            result += f"\nKept {restored} queued {noun_plural} — they'll play after this."
        return result

    def _snapshot_queue(self) -> list[str]:
        """Upcoming track uris, best effort and deduplicated. See
        play_uri's docstring for why this is a lookahead, not a full
        backup of a long queue.

        Deduplication isn't optional here — measured live 2026-08-14,
        Spotify's own queue endpoint padded its response with the same
        track repeated 9 times when there was little else queued (nothing
        this bot added; that's what GET /me/player/queue actually
        returned). Faithfully re-adding every entry compounded it: each
        interrupting play re-queued all N duplicates again, so a handful
        of "play X" calls turned nine copies into dozens. Keeping only the
        first occurrence of each uri is a blunt guard, but it holds
        regardless of why Spotify's response had duplicates in it.
        """
        if self.sp is None:
            return []
        try:
            data = self.sp.queue() or {}
        except Exception:
            log.debug("Could not read the Spotify queue before interrupting it", exc_info=True)
            return []
        seen: set[str] = set()
        out: list[str] = []
        for t in (data.get("queue") or []):
            uri = t.get("uri") if t else None
            if uri and uri not in seen:
                seen.add(uri)
                out.append(uri)
        return out

    def _restore_queue(self, uris: list[str], device: str) -> int:
        """Re-add tracks after an interrupting play. Best effort — a
        failure here isn't reported as the play itself failing, since the
        requested track is already playing by the time this runs."""
        restored = 0
        for uri in uris:
            try:
                self.sp.add_to_queue(uri, device_id=device)
                restored += 1
            except Exception:
                log.debug("Could not restore a queued track after interrupting playback",
                          exc_info=True)
                break
        return restored

    def _expand_tracks(self, uri: str, kind: str, limit: int = 30) -> list[str]:
        """Turn an album, playlist or artist into track URIs.

        Spotify's queue endpoint only accepts one track at a time, so anything
        that isn't a track has to be expanded and added track by track.
        """
        try:
            if kind == "album":
                rows = (self.sp.album_tracks(uri, limit=limit) or {}).get("items") or []
                return [r["uri"] for r in rows if r and r.get("uri")]
            if kind == "playlist":
                rows = (self.sp.playlist_items(uri, limit=limit) or {}).get("items") or []
                out = []
                for row in rows:
                    track = (row or {}).get("track") or {}
                    if track.get("uri"):
                        out.append(track["uri"])
                return out
            if kind == "artist":
                rows = (self.sp.artist_top_tracks(uri) or {}).get("tracks") or []
                return [r["uri"] for r in rows if r.get("uri")][:limit]
        except Exception:
            log.exception("Could not expand %s into tracks", uri)
        return []

    def queue_uri(self, uri: str, label: str = "", kind: str = "") -> str:
        """Add something to the Spotify queue without interrupting playback."""
        if self.sp is None:
            return "Spotify isn't set up."
        if not self.premium:
            return "Queueing needs Spotify Premium."
        device = self.ensure_device()
        if device is None:
            return "No Spotify device found. Is the desktop app running on that machine?"

        uris = [uri] if uri.startswith("spotify:track:") else self._expand_tracks(uri, kind)
        if not uris:
            return f"Nothing to queue from **{label or uri}**."

        added = 0
        try:
            for track_uri in uris:
                self.sp.add_to_queue(track_uri, device_id=device)
                added += 1
        except Exception as exc:
            if _premium_required(exc):
                self.premium = False
                return "Spotify refused — queueing needs Premium."
            log.exception("Spotify queue failed after %d tracks", added)
            if not added:
                return "Couldn't add that to the Spotify queue."

        if added == 1:
            return f"Queued **{label or uri}** on Spotify."
        return f"Queued {added} tracks from **{label or uri}** on Spotify."

    def queue_list(self, limit: int = 10) -> str:
        if self.sp is None:
            return "Spotify isn't set up."
        try:
            data = self.sp.queue() or {}
        except Exception:
            log.exception("Could not read the Spotify queue")
            return "Couldn't read the Spotify queue."
        rows = [r for r in (data.get("queue") or []) if r][:limit]
        if not rows:
            return "Spotify queue is empty."
        lines = [f"{i + 1}. {self._label(r, 'track')}" for i, r in enumerate(rows)]
        return "**Up next on Spotify:**\n" + "\n".join(lines)

    # A track, album, artist or playlist link/id, each with the spotipy call
    # that resolves it to a full item — used only to build a readable label
    # for a pasted link, since play_link() has no prior search step to get
    # one from the way play_uri()'s callers normally do.
    _LOOKUP = {
        "track": lambda sp, uri: sp.track(uri),
        "album": lambda sp, uri: sp.album(uri),
        "artist": lambda sp, uri: sp.artist(uri),
        "playlist": lambda sp, uri: sp.playlist(uri),
    }

    def _lookup_label(self, uri: str, kind: str) -> str:
        """Best-effort — a failed lookup still lets play_link() go ahead,
        just with the uri standing in for a name (play_uri's own fallback)."""
        fn = self._LOOKUP.get(kind)
        if self.sp is None or fn is None:
            return ""
        try:
            item = fn(self.sp, uri)
        except Exception:
            log.debug("Could not look up a label for %s", uri, exc_info=True)
            return ""
        return self._label(item, kind) if item else ""

    def play_link(self, uri: str) -> str:
        """Play an already-extracted spotify:type:id uri (see find_uri),
        looking up a label first so the reply names the thing instead of
        echoing the uri back."""
        kind = self.kind_from_uri(uri)
        label = self._lookup_label(uri, kind)
        return self.play_uri(uri, label, kind)

    def queue_link(self, uri: str) -> str:
        """Queue an already-extracted spotify:type:id uri (see find_uri) —
        the "queue <link>" counterpart to play_link."""
        kind = self.kind_from_uri(uri)
        label = self._lookup_label(uri, kind)
        return self.queue_uri(uri, label, kind)

    def clear_queue(self) -> str:
        """Empty the ad-hoc queue behind whatever's currently playing.

        There is no "clear queue" endpoint — Spotify's Web API only offers
        read (GET queue) and append (add to queue), nothing that removes.

        First attempt (kept here as the record of why it's gone): restart
        the currently playing track at its own position, on the theory
        that starting playback wipes the queue the same way an
        interrupting play does (see play_uri's docstring). Measured live
        2026-08-14 and it did NOT work — Spotify treats "start the track
        that's already playing, at the position it's already at" as a
        no-op, since nothing about the context actually changes, and the
        queue survived untouched.

        Skip is the only operation that actually consumes queued tracks,
        so it's the only thing that reliably works — at the cost of
        genuinely being what it sounds like: it plays through whatever was
        queued, same as pressing skip that many times by hand, not a
        silent wipe.
        """
        if self.sp is None:
            return "Spotify isn't set up."
        try:
            data = self.sp.queue() or {}
        except Exception:
            return "Couldn't reach Spotify."
        upcoming = [t for t in (data.get("queue") or []) if t]
        if not upcoming:
            return "Spotify queue is already empty."
        device = self.ensure_device()
        if device is None:
            return "No Spotify device found. Is the desktop app running on that machine?"
        cleared = 0
        try:
            for _ in upcoming:
                self.sp.next_track(device_id=device)
                cleared += 1
        except Exception as exc:
            if _premium_required(exc):
                self.premium = False
                return "Spotify refused — this needs Premium."
            log.exception("Could not clear the Spotify queue after %d skips", cleared)
            if not cleared:
                return "Couldn't clear the Spotify queue."
        noun = "track" if cleared == 1 else "tracks"
        return f"Skipped through {cleared} queued {noun} to clear them."

    def radio(self, query: str) -> str:
        """Approximate a radio station.

        Spotify killed the Recommendations and Related Artists endpoints for new
        apps in Nov 2024, so we can't build a station ourselves. Three fallbacks,
        best first: an existing radio-style playlist, the artist's own catalogue,
        or the seed track with Spotify's own Autoplay carrying on afterwards.
        """
        if self.sp is None:
            return "Spotify isn't set up."
        seed, _ = self._strip_prefix(query)
        if not seed:
            return "Radio for what?"

        try:
            found = self.sp.search(q=seed, type="artist,track", limit=5)
        except Exception:
            log.exception("Spotify search failed")
            return "Spotify search failed."

        lowered = seed.lower()

        def best(kind):
            rows = [i for i in ((found.get(kind + "s") or {}).get("items") or []) if i]
            exact = next((r for r in rows if (r.get("name") or "").lower() == lowered), None)
            return exact or (rows[0] if rows else None)

        artist = best("artist")
        track = best("track")
        name = (artist or track or {}).get("name") or seed

        # 1. A playlist someone already built for this.
        for probe in (f"{name} radio", f"This Is {name}", f"{name} mix"):
            try:
                rows = [
                    i
                    for i in (
                        (self.sp.search(q=probe, type="playlist", limit=5) or {})
                        .get("playlists", {})
                        .get("items")
                        or []
                    )
                    if i
                ]
            except Exception:
                rows = []
            for row in rows:
                if name.lower() in (row.get("name") or "").lower():
                    return self.play_uri(row["uri"], row["name"], "playlist")

        # 2. The artist's own catalogue, with Autoplay continuing afterwards.
        if artist:
            result = self.play_uri(artist["uri"], artist["name"], "artist")
            return f"{result}\n{self._autoplay_note()}"

        # 3. Just the seed track, same idea.
        if track:
            label = self._label(track, "track")
            result = self.play_uri(track["uri"], label, "track")
            return f"{result}\n{self._autoplay_note()}"

        return f"Couldn't find anything to build a station from for \u201c{seed}\u201d."

    @staticmethod
    def _autoplay_note() -> str:
        return (
            "_No true station available (Spotify closed that API to new apps), so "
            "this plays the catalogue. Turn on Autoplay in the Spotify desktop app "
            "settings and it'll keep going with similar music afterwards._"
        )

    def play(self, query: str) -> str:
        """Play the single best match without asking. Used by the LLM path and
        when only one candidate came back."""
        if self.sp is None:
            return "Spotify isn't set up."
        if not self.premium:
            return "Spotify search needs Premium. `music` alone still works via media keys."
        options, note = self.search_options(query)
        if note:
            return note
        if not options:
            return f"Nothing on Spotify matched \u201c{query}\u201d."
        best = options[0]
        return self.play_uri(best["uri"], best["label"], best["kind"])

    # ------------------------------------------------------------------

    def _simple(self, api_call, key_name: str, ok: str) -> str:
        if self.sp is not None and self.premium:
            try:
                api_call()
                return ok
            except Exception as exc:
                if _premium_required(exc):
                    self.premium = False
                elif "404" in str(exc):
                    return "Spotify has no active device right now."
                else:
                    log.debug("Spotify call failed, trying media key: %s", exc)
        return ok if media_key(key_name) else "Couldn't reach Spotify."

    def pause(self) -> str:
        self.playing = False
        return self._simple(
            lambda: self.sp.pause_playback(device_id=self.device_id),
            "playpause",
            "Music paused.",
        )

    def resume(self) -> str:
        """Resume playback, re-acquiring the device first.

        After a stop, Spotify often has no active device at all, and
        start_playback with a stale id 404s. ensure_device re-finds the desktop
        app (launching it if configured) so resume works from a cold stop.
        """
        device = self.ensure_device() if self.sp is not None else None
        self.playing = True
        return self._simple(
            lambda: self.sp.start_playback(device_id=device or self.device_id),
            "playpause",
            "Music playing.",
        )

    def next_track(self) -> str:
        return self._simple(
            lambda: self.sp.next_track(device_id=self.device_id), "next", "Next track."
        )

    def previous_track(self) -> str:
        return self._simple(
            lambda: self.sp.previous_track(device_id=self.device_id),
            "previous",
            "Previous track.",
        )

    def set_volume(self, percent: int) -> str:
        if self.sp is None or not self.premium:
            return "Volume needs Spotify Premium."
        percent = max(0, min(100, int(percent)))
        try:
            self.sp.volume(percent, device_id=self.device_id)
            return f"Spotify volume {percent}%."
        except Exception:
            return "Couldn't set the volume."

    def now_playing(self) -> str:
        if self.sp is None:
            return "Spotify isn't set up."
        try:
            current = self.sp.current_playback()
        except Exception:
            return "Couldn't reach Spotify."
        if not current or not current.get("item"):
            return "Nothing playing on Spotify."
        item = current["item"]
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        state = "Playing" if current.get("is_playing") else "Paused"
        self.playing = bool(current.get("is_playing"))
        return f"**{state} on Spotify:** {item['name']} — {artists}"
