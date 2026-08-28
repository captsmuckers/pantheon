"""YouTube playback.

Two halves, deliberately split:

* yt-dlp resolves a URL or a search phrase to a title and duration, so the bot
  can say what it's playing before mpv has opened anything.
* mpv then plays the *webpage* URL and lets its own ytdl hook pick formats.
  Resolving a direct stream URL here instead would be faster, but YouTube
  serves anything above 720p as separate video and audio streams, and mpv
  already knows how to merge them.

Everything here is blocking. Call it from a thread.
"""

import logging
import re

import config

log = logging.getLogger("athena.youtube")

_URL = re.compile(
    r"^https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?\S*v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[\w-]{6,}\S*$",
    re.I,
)


def is_url(text: str) -> bool:
    return bool(_URL.match((text or "").strip()))


# The same link with a verb in front of it, or inside a sentence. _URL is
# anchored, so "play https://youtu.be/x" was not a URL as far as it was
# concerned: it went to the model, which called play_media with the link as a
# query and searched Plex for it.
_URL_ANYWHERE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?\S*v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[\w-]{6,}\S*",
    re.I,
)


def find_url(text: str) -> str | None:
    """A YouTube link anywhere in the message, or None."""
    match = _URL_ANYWHERE.search(text or "")
    if not match:
        return None
    # Trailing punctuation belongs to the sentence, not the link.
    return match.group(0).rstrip(").,;:!?\"'")


class AgeRestricted(Exception):
    """Raised by resolve() for a link yt-dlp can't get past the age gate.

    Only raised when the query was already a URL — for a search phrase, an
    age-gate failure happens deep inside extraction and there's no reliable
    webpage_url to hand off to a browser instead, so that case falls back to
    the ordinary "couldn't find it" outcome.
    """

    def __init__(self, url: str):
        self.url = url
        super().__init__(f"age-restricted: {url}")


_AGE_GATE_PHRASES = ("confirm your age", "age-restricted", "inappropriate for some users")


def _looks_age_restricted(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _AGE_GATE_PHRASES)


class YouTubeItem:
    """Enough of a plexapi item for the player to handle it.

    `external` is the flag the player checks before doing anything Plex
    specific — building a stream URL, reading a resume position, reporting
    progress, marking watched. None of those mean anything here.
    """

    external = True
    type = "video"
    ratingKey = None
    librarySectionTitle = None

    def __init__(self, url: str, title: str, duration_s: float | None,
                 uploader: str = "", is_live: bool = False):
        self.stream_url = url
        self.title = title if not uploader else f"{title} — {uploader}"
        # Player expects milliseconds, like Plex. Live streams have no duration.
        self.duration = int((duration_s or 0) * 1000)
        # A YouTube live broadcast can't be seeked or resumed either; the
        # player branches on this the same way it does for Twitch and Kick.
        self.is_live = bool(is_live)

    def __repr__(self) -> str:
        return f"<YouTubeItem {self.title!r}>"


def resolve(query: str) -> YouTubeItem | None:
    """A URL or a search phrase -> a playable item, or None.

    A private or removed video is a normal outcome and returns None, reading
    as "couldn't find that" rather than a crash. An age-gated *link* is the
    one exception: it raises AgeRestricted so the caller can offer the
    browser fallback instead of just failing.
    """
    query = (query or "").strip()
    if not query:
        return None
    try:
        import yt_dlp
    except ImportError:
        log.warning("yt-dlp isn't installed — YouTube playback unavailable")
        return None

    target = query if is_url(query) else f"ytsearch1:{query}"
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # A whole playlist would silently become just its first entry; be
        # explicit that only one video is wanted.
        "playlist_items": "1",
        "socket_timeout": config.YOUTUBE_TIMEOUT,
    }
    # A signed-in session, if one is configured. Without it an age-gated video
    # fails with "Sign in to confirm your age" and reads to the user as though
    # the video does not exist.
    if config.YTDL_COOKIES_FROM_BROWSER:
        options["cookiesfrombrowser"] = tuple(
            config.YTDL_COOKIES_FROM_BROWSER.split(":")
        )
    elif config.YTDL_COOKIEFILE:
        options["cookiefile"] = config.YTDL_COOKIEFILE
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:
        log.info("YouTube lookup failed for %r: %s", query[:60], exc)
        if is_url(query) and _looks_age_restricted(exc):
            raise AgeRestricted(query) from None
        return None

    if not info:
        return None
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        info = entries[0]

    url = info.get("webpage_url") or info.get("original_url") or query
    title = info.get("title") or "Unknown video"
    return YouTubeItem(url, title, info.get("duration"),
                       info.get("uploader") or "", bool(info.get("is_live")))
