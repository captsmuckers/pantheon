"""Audio and subtitle track selection.

Two problems this solves:

1. Language tags in real files are a mess — "eng", "en", "English", or missing
   entirely with the language only hinted in the track title. normalize_lang
   flattens all of that.
2. Picking the *right* track of a language, not just the first. A commentary
   track is English. So is a 2.0 downmix next to a 5.1 main track. And an anime
   file's "Signs & Songs" track is English subtitles that are useless if you're
   watching subbed and exactly right if you're watching dubbed.
"""

import json
import logging
import os
import re

log = logging.getLogger("athena.tracks")

# Normalized to ISO 639-2/B three-letter codes.
_ALIASES = {
    "eng": "eng", "en": "eng", "english": "eng",
    "jpn": "jpn", "ja": "jpn", "jp": "jpn", "japanese": "jpn",
    "spa": "spa", "es": "spa", "esp": "spa", "spanish": "spa", "castellano": "spa",
    "fre": "fre", "fra": "fre", "fr": "fre", "french": "fre",
    "ger": "ger", "deu": "ger", "de": "ger", "german": "ger",
    "hin": "hin", "hi": "hin", "hindi": "hin",
    "kor": "kor", "ko": "kor", "korean": "kor",
    "chi": "chi", "zho": "chi", "zh": "chi", "chinese": "chi", "mandarin": "chi",
    "ita": "ita", "it": "ita", "italian": "ita",
    "por": "por", "pt": "por", "portuguese": "por",
    "rus": "rus", "ru": "rus", "russian": "rus",
    "ara": "ara", "ar": "ara", "arabic": "ara",
    "und": None, "unknown": None, "": None,
}

DISPLAY = {
    "eng": "English", "jpn": "Japanese", "spa": "Spanish", "fre": "French",
    "ger": "German", "hin": "Hindi", "kor": "Korean", "chi": "Chinese",
    "ita": "Italian", "por": "Portuguese", "rus": "Russian", "ara": "Arabic",
}

# Track titles that mean "not the main track"
_JUNK_AUDIO = re.compile(r"comment|descri|director|karaoke|instrumental", re.I)
# Subtitle tracks that only translate on-screen text and song lyrics
_SIGNS = re.compile(r"sign|song|forced|caption only|s&s|s & s", re.I)
_FULL = re.compile(r"dialog|full|complete|main", re.I)
_SDH = re.compile(r"sdh|\bcc\b|hearing", re.I)


def normalize_lang(value) -> str | None:
    """'English', 'en', 'eng' -> 'eng'. Unknown or untagged -> None."""
    if not value:
        return None
    key = str(value).strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    for word, code in _ALIASES.items():
        if code and word in key:
            return code
    return None


def display_lang(code: str | None) -> str:
    if not code or code == "off":
        return "off"
    return DISPLAY.get(code, code)


def track_language(track: dict) -> str | None:
    """Language of an mpv track, falling back to hints in the title."""
    return normalize_lang(track.get("lang")) or normalize_lang(track.get("title"))


def _channels(track: dict) -> int:
    for key in ("demux-channel-count", "demux_channel_count", "audio-channels"):
        value = track.get(key)
        if isinstance(value, int):
            return value
    return 0


def pick_audio(tracks: list[dict], want: str | None) -> int | None:
    """Best audio track id for the wanted language, or None to leave mpv alone."""
    want = normalize_lang(want)
    audio = [t for t in tracks if t.get("type") == "audio"]
    if not audio or not want:
        return None

    candidates = [t for t in audio if track_language(t) == want]
    if not candidates:
        # Untagged single track is almost always the main one — don't fight it.
        log.info("No %s audio track found; leaving selection alone", want)
        return None

    def score(t: dict) -> tuple:
        title = t.get("title") or ""
        return (
            0 if _JUNK_AUDIO.search(title) else 1,
            _channels(t),
            1 if t.get("default") else 0,
            -int(t.get("id") or 0),
        )

    best = max(candidates, key=score)
    return int(best["id"])


def pick_subtitle(tracks: list[dict], want: str | None, audio_lang: str | None):
    """Best subtitle track id, or the string 'no' to disable subtitles."""
    if not want or want == "off":
        return "no"
    want = normalize_lang(want)
    subs = [t for t in tracks if t.get("type") == "sub"]
    if not subs or not want:
        return "no"

    candidates = [t for t in subs if track_language(t) == want]
    if not candidates:
        # An untagged subtitle track is better than nothing when subs were asked for.
        candidates = [t for t in subs if track_language(t) is None]
    if not candidates:
        return "no"

    # If the audio is already in the language you asked subtitles for, you almost
    # certainly want signs-and-songs, not a full transcript of dialogue you can hear.
    signs_preferred = audio_lang is not None and audio_lang == want

    def score(t: dict) -> tuple:
        title = t.get("title") or ""
        is_signs = bool(_SIGNS.search(title))
        is_full = bool(_FULL.search(title))
        if signs_preferred:
            rank = 2 if is_signs else (0 if is_full else 1)
        else:
            rank = 0 if is_signs else (2 if is_full else 1)
        return (
            rank,
            0 if _SDH.search(title) else 1,
            1 if t.get("default") else 0,
            -int(t.get("id") or 0),
        )

    best = max(candidates, key=score)
    return int(best["id"])


def selected_language(tracks: list[dict], kind: str) -> str | None:
    for t in tracks:
        if t.get("type") == kind and t.get("selected"):
            return track_language(t)
    return None


def describe_tracks(tracks: list[dict], kind: str) -> str:
    rows = [t for t in tracks if t.get("type") == kind]
    if not rows:
        return f"No {kind} tracks on this file."
    lines = []
    for t in rows:
        mark = " <-- current" if t.get("selected") else ""
        lang = display_lang(track_language(t))
        title = t.get("title") or ""
        extra = f" — {title}" if title else ""
        lines.append(f"`{t.get('id')}` {lang}{extra}{mark}")
    label = "Audio" if kind == "audio" else "Subtitle"
    return f"**{label} tracks:**\n" + "\n".join(lines)


class Preferences:
    """Per-library language preferences, persisted to disk.

    Keyed by Plex library name, so your Anime library can default to Japanese
    audio with English subs while everything else stays English audio, no subs.
    """

    def __init__(self, path: str, default_audio: str, default_subs: str, seed: dict):
        self.path = path
        self.default_audio = normalize_lang(default_audio) or "eng"
        self.default_subs = "off" if default_subs in (None, "", "off") else (
            normalize_lang(default_subs) or "off"
        )
        self.data: dict[str, dict] = dict(seed)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except Exception:
            log.exception("Could not read %s — using defaults", self.path)

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
        except Exception:
            log.exception("Could not write %s", self.path)

    def for_library(self, library: str | None) -> tuple[str, str]:
        entry = self.data.get(library or "", {})
        return (
            entry.get("audio", self.default_audio),
            entry.get("subs", self.default_subs),
        )

    def set(self, library: str | None, audio: str | None = None, subs: str | None = None):
        key = library or ""
        entry = dict(self.data.get(key, {}))
        if audio is not None:
            entry["audio"] = normalize_lang(audio) or audio
        if subs is not None:
            entry["subs"] = "off" if subs == "off" else (normalize_lang(subs) or subs)
        self.data[key] = entry
        self._save()
        return entry
