"""Live stream playback: Twitch and Kick.

Deliberately live-only. A channel URL resolves to whatever is being broadcast
right now; if nobody is live, that is a normal answer ("they're not live"), not
an error and not a reason to fall back to an old VOD nobody asked for.

Shares the shape of youtube.py: yt-dlp resolves a title so the bot can say what
it's playing, then mpv is handed the *webpage* URL and its own ytdl hook picks
the format. Items are `external`, so the player skips everything Plex-specific.

The important difference from YouTube is `is_live`. A live stream has no
duration, cannot be seeked, and must never be resumed at a stored position —
the player branches on that flag rather than guessing from `duration == 0`.

Everything here is blocking. Call it from a thread.
"""

import logging
import re

import config

log = logging.getLogger("athena.streams")

# Channel pages, VODs and clips. Kept narrow so a random twitch.tv/p/... or a
# link to someone's about page doesn't get treated as playable.
_TWITCH_URL = re.compile(
    r"^https?://(?:www\.|m\.)?twitch\.tv/"
    r"(?:videos/\d+|\w[\w-]{2,24}(?:/clip/[\w-]+)?)/?(?:\?\S*)?$",
    re.I,
)
_KICK_URL = re.compile(
    r"^https?://(?:www\.)?kick\.com/"
    r"\w[\w-]{2,24}(?:/(?:videos|clips)/[\w-]+)?/?(?:\?\S*)?$",
    re.I,
)

# Bare channel names, for "twitch someone" / "kick someone".
_CHANNEL = re.compile(r"^[A-Za-z0-9][\w-]{2,24}$")

# A link pasted inside a sentence, or on the line under one. The anchored
# matching above treats the whole message as the URL, so
# "stream this from kick\nhttps://kick.com/someone" was not recognised at all
# and fell through to the music tool.
_URL_ANYWHERE = re.compile(
    r"https?://(?:www\.|m\.)?(?P<host>twitch\.tv|kick\.com)/\S+", re.I
)

SOURCES = ("twitch", "kick")
_BASE = {"twitch": "https://www.twitch.tv/", "kick": "https://kick.com/"}

# yt-dlp says this a dozen different ways depending on site and extractor
# version. Matching on the phrasing is fragile, so keep the list broad and
# treat anything unmatched as a genuine failure rather than silently claiming
# someone is offline.
_OFFLINE_HINTS = (
    "is offline", "offline", "not currently live", "no longer live",
    "is not live", "stream is not available", "channel is not live",
)

# Kick sits behind Cloudflare and answers yt-dlp with 403 unless it can
# impersonate a browser TLS fingerprint. Reporting that as "couldn't find the
# channel" blames the user for a channel that exists and is fine — it is worth
# a distinct answer, because the fix is on this end, not theirs.
_BLOCKED_HINTS = (
    "403", "forbidden", "cloudflare", "captcha", "access denied",
    "unable to download json metadata",
)


def source_of(text: str) -> str | None:
    """Which live source a URL belongs to, or None if it isn't one."""
    text = (text or "").strip()
    if _TWITCH_URL.match(text):
        return "twitch"
    if _KICK_URL.match(text):
        return "kick"
    return None


def is_url(text: str) -> bool:
    return source_of(text) is not None


def find_url(text: str):
    """(url, source) for a stream link anywhere in the message, else (None, None).

    People paste the link under a sentence rather than on its own, which the
    anchored match in source_of() cannot see.
    """
    match = _URL_ANYWHERE.search(text or "")
    if not match:
        return None, None
    # Trailing punctuation belongs to the sentence, not the link.
    url = match.group(0).rstrip(").,;:!?\"'")
    return (url, source_of(url)) if source_of(url) else (None, None)


def channel_url(source: str, channel: str) -> str | None:
    """A bare channel name -> its page URL. None if the name is implausible."""
    channel = (channel or "").strip().lstrip("@").rstrip("/")
    if source not in _BASE or not _CHANNEL.match(channel):
        return None
    return _BASE[source] + channel


class StreamItem:
    """Enough of a plexapi item for the player to handle it.

    `external` tells the player this carries its own URL and has no resume
    position, watch state or timeline. `is_live` additionally means it cannot
    be seeked and has no end to advance past.
    """

    external = True
    type = "video"
    ratingKey = None
    librarySectionTitle = None

    def __init__(self, url: str, title: str, duration_s: float | None = None,
                 uploader: str = "", is_live: bool = False, source: str = ""):
        self.stream_url = url
        self.title = title if not uploader else f"{title} — {uploader}"
        # Player expects milliseconds, like Plex. Live streams have no duration.
        self.duration = int((duration_s or 0) * 1000)
        self.is_live = bool(is_live)
        self.source = source

    def __repr__(self) -> str:
        live = " live" if self.is_live else ""
        return f"<StreamItem {self.source}{live} {self.title!r}>"


def resolve(query: str, source: str | None = None):
    """A channel name or URL -> (item, reason).

    reason is None on success, otherwise one of "offline", "notfound", or
    "unavailable" so the caller can say something useful. Never raises: a
    channel being offline is the most likely outcome of all.
    """
    query = (query or "").strip()
    if not query:
        return None, "notfound"

    src = source_of(query) or source
    if src not in SOURCES:
        return None, "notfound"

    target = query if source_of(query) else channel_url(src, query)
    if not target:
        return None, "notfound"

    try:
        import yt_dlp
    except ImportError:
        log.warning("yt-dlp isn't installed — live stream playback unavailable")
        return None, "unavailable"

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": config.YOUTUBE_TIMEOUT,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:
        text = str(exc).lower()
        # Offline is checked first: a 403 on an offline channel is still
        # usefully reported as offline, and "not currently live" is the more
        # specific signal of the two.
        if any(h in text for h in _OFFLINE_HINTS):
            log.info("%s channel %r is offline", src, query[:40])
            return None, "offline"
        if any(h in text for h in _BLOCKED_HINTS):
            log.warning("%s blocked the lookup for %r: %s", src, query[:40], exc)
            return None, "blocked"
        log.info("%s lookup failed for %r: %s", src, query[:40], exc)
        return None, "notfound"

    if not info:
        return None, "notfound"
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None, "offline"
        info = entries[0]

    # A channel URL for an offline streamer can also come back as a normal
    # entry with is_live false — that is the VOD we were asked not to play.
    is_live = bool(info.get("is_live"))
    if not is_live and not source_of(query):
        return None, "offline"

    url = info.get("webpage_url") or info.get("original_url") or target
    title = info.get("title") or f"{src.title()} stream"
    uploader = info.get("uploader") or info.get("channel") or ""
    return StreamItem(url, title, info.get("duration"), uploader, is_live, src), None
