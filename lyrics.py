"""Karaoke mode: synced lyrics displayed over mpv's idle screen.

Two tiers, best available wins:

1. Real word-level timing from Musixmatch's unofficial "richsync" endpoint
   (via the syncedlyrics package) — genuine per-word \\k fill, because the
   timestamps are measured, not guessed. Coverage is partial even among
   popular tracks (measured live 2026-08-14: 1 of 3 mainstream hits had
   it) and this is a reverse-engineered endpoint, not Musixmatch's public
   API, so it's optional and can fail or disappear without breaking
   anything else here.
2. LRCLIB, a free keyless database of line-level LRC — reliable, but only
   ever times whole lines. A prior version of this file estimated
   within-line word timing by splitting a line's duration proportionally
   to word length; that was a guess dressed up as sync and measured live
   to look exactly like one (it does not track actual vocal timing at
   all). Lines from this tier get the current line highlighted flat the
   instant it's correct, nothing animated — accurate is better than
   fancy-looking-wrong.

Spotify has no lyrics API of its own — the in-app lyrics are licensed from
Musixmatch and aren't exposed there either.

Sync strategy: ask Spotify where it is only every few seconds, then
interpolate locally from a monotonic clock. Polling once a second would
work but wastes requests and drifts no less.
"""

import asyncio
import logging
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("athena.lyrics")

LRCLIB = "https://lrclib.net/api/get"
USER_AGENT = "Athena/1.0 (Discord Plex+Spotify bot)"

_LRC_LINE = re.compile(r"\[(?P<m>\d{1,3}):(?P<s>\d{2})(?:[.:](?P<cs>\d{1,3}))?\]")
# Musixmatch's enhanced format: [line-ts] <word-ts> word <word-ts> word ...
_ENHANCED_LINE = re.compile(
    r"^\[(?P<m>\d{1,3}):(?P<s>\d{2})(?:[.:](?P<cs>\d{1,3}))?\]\s*(?P<body>.*)$"
)
_WORD_TAG = re.compile(
    r"<(?P<m>\d{1,3}):(?P<s>\d{2})(?:[.:](?P<cs>\d{1,3}))?>\s*(?P<word>[^<]*)"
)

# How often to re-anchor against Spotify's reported position, and to refresh
# the "up next" row from the live queue.
RESYNC_SECONDS = 8
# How often to check whether what's on screen needs to change.
TICK_SECONDS = 0.5
# The overlay only ever shows the next few — Spotify's queue endpoint
# returns up to 20, but a wall of song titles stops being readable fast.
QUEUE_DISPLAY_LIMIT = 5
# A word-level line's last word has no measured end time (no next word to
# take it from); capped so a long gap to the next line doesn't stretch the
# final word's fill unnaturally.
MAX_LAST_WORD_SECONDS = 3.0

# The karaoke display is rendered through player.osd_overlay() (real ASS
# rendering), not show_text() (a plain toast that doesn't parse override
# tags at all — confirmed live 2026-08-14: {\k50}word showed up on screen
# as those literal characters). mpv supports several simultaneous overlays
# at different screen positions, distinguished by id — three here, since
# the lyric line, the "Now Playing" label and the queue list each occupy
# their own part of the screen and change on different, independent
# cadences (a line change shouldn't have to redraw the queue list too).
_LYRIC_OVERLAY_ID = 1
_NOWPLAYING_OVERLAY_ID = 2
_QUEUE_OVERLAY_ID = 3
_ALL_OVERLAY_IDS = (_LYRIC_OVERLAY_ID, _NOWPLAYING_OVERLAY_ID, _QUEUE_OVERLAY_ID)

# Bottom-center, bigger than mpv's small default OSD would be — plain text
# in the corner doesn't read as "karaoke" even when it's working correctly.
_LYRIC_STYLE = r"{\an2\fs64}"
_DIM = r"{\c&H888888&}"
_HIGHLIGHT = r"{\c&H00FFFF&}"
# Top-right — top-center crowded the top-left queue list once a track name
# ran long. A plain, secondary label, smaller than the lyric line since
# it isn't the point of the screen.
_NOWPLAYING_STYLE = r"{\an9\fs40}"
# Top-left, vertical list — smaller and dimmer still, context rather than
# the point of the screen.
_QUEUE_STYLE = r"{\an7\fs32\c&HAAAAAA&}"


def _stamp(m: str, s: str, cs: str | None) -> float:
    seconds = int(m) * 60 + int(s)
    if cs:
        seconds += int(cs.ljust(3, "0")) / 1000.0
    return seconds


def fetch_synced(artist: str, track: str, album: str, duration_s: int) -> str | None:
    """Get LRC text from LRCLIB, or None. Blocking."""
    params = {
        "artist_name": artist,
        "track_name": track,
        "album_name": album or "",
        "duration": str(int(duration_s)),
    }
    url = f"{LRCLIB}?{urllib.parse.urlencode(params)}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=8) as response:
            import json

            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log.debug("LRCLIB lookup failed for %s - %s: %s", artist, track, exc)
        return None
    if data.get("instrumental"):
        return ""
    return data.get("syncedLyrics") or None


def fetch_enhanced(artist: str, track: str) -> str | None:
    """Real word-level LRC via Musixmatch's unofficial endpoint, or None.

    Optional at every level: the syncedlyrics package may not be
    installed, the endpoint may be unreachable, and even when it answers
    this specific track may have no rich-sync data — all the same
    "nothing available" outcome from here, since none of them are errors
    worth surfacing over the reliable LRCLIB fallback.
    """
    try:
        import syncedlyrics
    except ImportError:
        return None
    try:
        return syncedlyrics.search(f"{artist} {track}", enhanced=True, providers=["Musixmatch"])
    except Exception:
        log.debug("Enhanced lyrics lookup failed for %s - %s", artist, track, exc_info=True)
        return None


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Plain LRC text -> [(seconds, line)], sorted. Lines can carry several
    stamps."""
    out: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        stamps = list(_LRC_LINE.finditer(raw))
        if not stamps:
            continue
        content = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            out.append((_stamp(stamp.group("m"), stamp.group("s"), stamp.group("cs")), content))
    out.sort(key=lambda pair: pair[0])
    return out


def parse_enhanced_lrc(text: str) -> list[tuple[float, str, list[tuple[float, str]]]] | None:
    """Musixmatch's word-level format -> [(line_seconds, full_text,
    [(word_seconds, word), ...])], sorted. None if the text has no actual
    word tags — syncedlyrics silently falls back to plain LRC internally
    when a track has no rich-sync data, and that plain text reaching here
    is the normal, expected "not available for this track" outcome, not a
    parse failure.
    """
    if "<" not in (text or ""):
        return None
    out: list[tuple[float, str, list[tuple[float, str]]]] = []
    for raw in (text or "").splitlines():
        line_m = _ENHANCED_LINE.match(raw)
        if not line_m:
            continue
        line_stamp = _stamp(line_m.group("m"), line_m.group("s"), line_m.group("cs"))
        words: list[tuple[float, str]] = []
        for word_m in _WORD_TAG.finditer(line_m.group("body")):
            word = word_m.group("word").strip()
            if word:
                words.append((_stamp(word_m.group("m"), word_m.group("s"), word_m.group("cs")),
                              word))
        if words:
            out.append((line_stamp, " ".join(w for _, w in words), words))
    out.sort(key=lambda row: row[0])
    return out or None


class Karaoke:
    """Drives the lyrics display. One instance, started and stopped by Controls."""

    def __init__(self, spotify, player):
        self.spotify = spotify
        self.player = player
        self.enabled = False
        self._task: asyncio.Task | None = None
        self._track_id = None
        # Each entry is (line_seconds, full_text, words_or_None). words is
        # only set for a line with real Musixmatch timing; LRCLIB lines
        # always carry None there.
        self._lines: list[tuple[float, str, list[tuple[float, str]] | None]] = []
        self._anchor_position = 0.0
        self._anchor_at = 0.0
        self._paused = False
        self._label = ""
        self._queue: list[str] = []
        # -2 is "nothing built yet", distinct from -1 ("before the first
        # lyric line") and from None ("no synced lyrics for this track").
        self._rendered_index = -2

    # ------------------------------------------------------------------

    async def start(self) -> str:
        self.enabled = True
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
        return "Karaoke mode on — lyrics will show on screen when they're available."

    async def stop(self) -> str:
        self.enabled = False
        if self._task:
            self._task.cancel()
            self._task = None
        self._track_id = None
        self._lines = []
        self._queue = []
        self._rendered_index = -2
        await self._clear()
        return "Karaoke mode off."

    def status(self) -> str:
        if not self.enabled:
            return "Karaoke mode is off."
        if not self._lines:
            return f"Karaoke on, but no synced lyrics found for {self._label or 'this track'}."
        synced = sum(1 for _, _, words in self._lines if words)
        if synced:
            return (f"Karaoke on — {len(self._lines)} lines loaded for {self._label} "
                     f"({synced} with real word-level sync).")
        return f"Karaoke on — {len(self._lines)} lines loaded for {self._label}."

    # ------------------------------------------------------------------

    async def _clear(self) -> None:
        for overlay_id in _ALL_OVERLAY_IDS:
            await self.player.clear_osd_overlay(overlay_id)

    def _position(self) -> float:
        if self._paused:
            return self._anchor_position
        return self._anchor_position + (time.monotonic() - self._anchor_at)

    def _current_index(self, position: float) -> int:
        """Which lyric line is current, -1 if before the first one."""
        index = -1
        for i, (stamp, _, _) in enumerate(self._lines):
            if stamp <= position:
                index = i
            else:
                break
        return index

    def _word_fill(self, index: int, position: float) -> str:
        """Real \\k fill from measured word timestamps — each word's
        duration is the gap to the NEXT word's actual timestamp, not an
        estimate. Only reachable for a line that has word-level data.

        Compares `position` directly against each word's own absolute
        timestamp — NOT a line-relative offset. word timestamps are
        already absolute seconds into the track, same as `position`.
        """
        _, _, words = self._lines[index]
        next_line_stamp = self._lines[index + 1][0] if index + 1 < len(self._lines) else None
        n = len(words)
        segments = []
        for i, (start, word) in enumerate(words):
            if i + 1 < n:
                end = words[i + 1][0]
            elif next_line_stamp is not None:
                end = min(next_line_stamp, start + MAX_LAST_WORD_SECONDS)
            else:
                end = start + MAX_LAST_WORD_SECONDS
            duration = max(0.05, end - start)
            if position <= start:
                cs = max(1, round(duration * 100))
            elif position >= end:
                cs = 0
            else:
                cs = max(1, round((end - position) * 100))
            segments.append(f"{{\\k{cs}}}{word} ")
        return "".join(segments).rstrip()

    def _build(self, position: float) -> str | None:
        """The lyric overlay text for the current position, or None to
        leave the existing overlay alone — nothing to redraw unless the
        visible line actually changed since the last push. Resending an
        unchanged word-synced line would restart its \\k fill from
        scratch; a flat-highlighted line has no animation to disturb, but
        there is no reason to repaint it either.
        """
        index = self._current_index(position) if self._lines else None
        if index == self._rendered_index:
            return None
        self._rendered_index = index

        rows: list[str] = []
        if index is None:
            if self._track_id is not None:
                rows.append(self._label or "")
                rows.append("(no synced lyrics found)")
        elif index < 0:
            rows.append("")
        else:
            previous = self._lines[index - 1][1] if index > 0 else ""
            following = self._lines[index + 1][1] if index + 1 < len(self._lines) else ""
            words = self._lines[index][2]
            current = (self._word_fill(index, position) if words
                       else f"{_HIGHLIGHT}{self._lines[index][1]}")
            rows.append(f"{_DIM}{previous}")
            rows.append(current)
            rows.append(f"{_DIM}{following}")

        if not rows:
            return ""
        # \N, not a literal newline: real ASS text (unlike show-text) only
        # breaks a line on its own escape sequence.
        return _LYRIC_STYLE + r"\N".join(rows)

    def _build_nowplaying(self) -> str:
        """Top-center "Now Playing: <track>" label. Empty when nothing's
        identified yet (before the first resync)."""
        if not self._label:
            return ""
        return f"{_NOWPLAYING_STYLE}Now Playing: {self._label}"

    def _build_queue_overlay(self) -> str:
        """Top-left vertical list of what's up next. Empty when nothing's
        queued — an empty overlay just means nothing shows there."""
        if not self._queue:
            return ""
        rows = [f"{i + 1}. {label}" for i, label in enumerate(self._queue)]
        return _QUEUE_STYLE + r"\N".join(["Up Next"] + rows)

    def _fetch_queue(self) -> list[str]:
        """Up to QUEUE_DISPLAY_LIMIT upcoming track labels, best effort —
        an empty list just means no "up next" row, not an error."""
        if self.spotify is None or self.spotify.sp is None:
            return []
        try:
            data = self.spotify.sp.queue() or {}
        except Exception:
            log.debug("Could not read the Spotify queue for the karaoke overlay",
                      exc_info=True)
            return []
        rows = [r for r in (data.get("queue") or []) if r][:QUEUE_DISPLAY_LIMIT]
        return [self.spotify._label(r, "track") for r in rows]

    def _fetch_lines(self, artist: str, track: str, album: str, duration: int
                      ) -> list[tuple[float, str, list[tuple[float, str]] | None]]:
        """Enhanced (real word-level) lyrics first, LRCLIB second. Blocking."""
        enhanced = fetch_enhanced(artist, track)
        parsed = parse_enhanced_lrc(enhanced) if enhanced else None
        if parsed:
            return parsed
        text = fetch_synced(artist, track, album, duration)
        if text:
            return [(stamp, line, None) for stamp, line in parse_lrc(text)]
        return []

    async def _resync(self) -> None:
        state = await asyncio.to_thread(self._playback)
        if not state:
            return
        track_id, position, is_playing, label, meta = state
        self._paused = not is_playing
        self._anchor_position = position
        self._anchor_at = time.monotonic()

        new_queue = await asyncio.to_thread(self._fetch_queue)
        if new_queue != self._queue:
            self._queue = new_queue
            await self.player.osd_overlay(_QUEUE_OVERLAY_ID, self._build_queue_overlay())

        if track_id != self._track_id:
            self._track_id = track_id
            self._label = label
            self._lines = await asyncio.to_thread(
                self._fetch_lines, meta["artist"], meta["track"], meta["album"], meta["duration"]
            )
            self._rendered_index = -2  # force the lyric overlay to rebuild
            await self.player.osd_overlay(_NOWPLAYING_OVERLAY_ID, self._build_nowplaying())
            if self._lines:
                synced = sum(1 for _, _, words in self._lines if words)
                log.info("Loaded %d lyric lines for %s (%d word-synced)",
                         len(self._lines), label, synced)
            else:
                log.info("No synced lyrics for %s", label)

    def _playback(self):
        if self.spotify is None or self.spotify.sp is None:
            return None
        try:
            current = self.spotify.sp.current_playback()
        except Exception:
            return None
        if not current or not current.get("item"):
            return None
        item = current["item"]
        artist = ", ".join(a["name"] for a in item.get("artists", []))
        return (
            item.get("id"),
            (current.get("progress_ms") or 0) / 1000.0,
            bool(current.get("is_playing")),
            f"{item.get('name')} — {artist}",
            {
                "artist": (item.get("artists") or [{}])[0].get("name", ""),
                "track": item.get("name") or "",
                "album": (item.get("album") or {}).get("name", ""),
                "duration": round((item.get("duration_ms") or 0) / 1000),
            },
        )

    async def _loop(self) -> None:
        last_resync = 0.0
        while self.enabled:
            try:
                await asyncio.sleep(TICK_SECONDS)
                if time.monotonic() - last_resync > RESYNC_SECONDS:
                    last_resync = time.monotonic()
                    await self._resync()
                text = self._build(self._position())
                if text is not None:
                    await self.player.osd_overlay(_LYRIC_OVERLAY_ID, text)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Karaoke loop error")
