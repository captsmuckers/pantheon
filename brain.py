"""Natural language -> actions.

Two tiers, deliberately:

1. A regex fast path for the dozen things people actually type most ("pause",
   "skip", "back 30s"). These answer in milliseconds and cost nothing.
2. a local model with tool-calling for everything else — fuzzy titles, compound
   requests, questions about what's on.

Tier 1 exists because you do not want a network round-trip between someone
typing "pause" and the video pausing.
"""

import asyncio
import time
import json
import logging
import re

import browser
import config
import flavor
# Imports cleanly without numpy/sounddevice — silence() is a no-op there.
import speech
import spotify as spotify_mod
import streams
import tracks as tk
import wm
import youtube
from lyrics import Karaoke
from library import describe

log = logging.getLogger("athena.brain")

MAX_TOOL_ROUNDS = 6
# Longest to queue behind another request's model call before giving up and
# saying so, rather than waiting silently forever.
LOCK_WAIT = 90


class Choice:
    """Several things matched. The front end turns this into a picker."""

    def __init__(self, candidates, action: str, query: str):
        self.candidates = candidates
        self.action = action  # 'play' or 'queue'
        self.query = query

    def text(self) -> str:
        verb = "play" if self.action == "play" else "queue"
        return f"A few things match \u201c{self.query}\u201d \u2014 which one should I {verb}?"

    def __str__(self) -> str:
        """Readable fallback if something sends this as plain text."""
        lines = "\n".join(
            f"{i + 1}. {c.long_label()}" for i, c in enumerate(self.candidates)
        )
        return f"{self.text()}\n{lines}"
HISTORY_TURNS = 6

# How many tracks a genre queue adds. Enough to be worth asking for, few
# enough that it does not bury a queue someone was already building.
GENRE_QUEUE_TRACKS = 10


# ----------------------------------------------------------------------
# Tier 1: fast path
# ----------------------------------------------------------------------

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
}

# Trailing noise words people naturally add — "stop the movie", "pause it",
# "skip this episode" — without needing a distinct pattern for every phrasing.
_TAIL = (
    r"(?:\s+(?:the|this)\s+(?:movie|video|show|episode|film|song|track)"
    r"|\s+(?:song|track)"
    r"|\s+it|\s+this)?"
)

FAST = [
    (re.compile(rf"^(?:pause{_TAIL}|hold on|wait|stop for a sec|brb)!?\.?$", re.I), ("pause", {})),
    (re.compile(rf"^(?:play|resume{_TAIL}|unpause{_TAIL}|continue{_TAIL}|go|ok go|back)!?\.?$", re.I),
     ("resume", {})),
    (re.compile(rf"^(?:stop{_TAIL}|end it|turn it off|kill it|shut it off)!?\.?$", re.I), ("stop", {})),
    (re.compile(
        rf"^(?:play\s+)?(?:skip|next(?:\s+one)?|next\s+ep(?:isode)?){_TAIL}!?\.?$", re.I
     ), ("skip", {})),
    (re.compile(r"^(?:what'?s (?:playing|on)|now playing|status)\??$", re.I), ("status", {})),
    (re.compile(r"^(?:queue|what'?s (?:next|up next)|up next)\??$", re.I), ("queue_list", {})),
    (re.compile(r"^(?:subs? off|subtitles off|turn (?:off|the) subs? off|no subs?)!?\.?$", re.I),
     ("subs_off", {})),
    (re.compile(r"^(?:tracks|audio tracks|list tracks)\??$", re.I), ("tracks_list", {})),
    (re.compile(r"^(?:libraries|libs|what libraries)\??$", re.I), ("libraries", {})),
    # "help" and "commands" are unambiguous requests for the reference table.
    # "what can you do" is NOT - it is conversation, and answering it with a
    # wall of backticked syntax breaks the natural-language flow the bot is
    # for. It goes to the model, which describes itself in its own words and
    # offers the specifics; "help" is still there when someone wants the list.
    (re.compile(r"^(?:help|commands|\?)\??!?$", re.I),
     ("help", {"topic": ""})),
    (re.compile(r"^(?:show|focus|switch to)\s+spotify!?\.?$", re.I), ("show_spotify", {})),
    # Unconditional — unlike the bare "status" pattern above, which reports
    # whatever's currently active. This is for explicitly asking about
    # Spotify regardless of what that is (e.g. a movie's on screen right
    # now, but Spotify has something queued up behind it).
    (re.compile(r"^(?:what'?s playing on spotify|spotify(?:'s)? now playing|"
                r"spotify status|what'?s on spotify)\??$", re.I), ("spotify_status", {})),
    # Deliberately requires the word "spotify" — bare "clear queue" already
    # means Athena's own video queue (manage_queue's clear action).
    (re.compile(r"^(?:clear|empty)\s+(?:the\s+)?spotify\s+queue!?\.?$", re.I),
     ("spotify_clear_queue", {})),
    (re.compile(r"^(?:show|focus|switch to)\s+(?:the\s+)?(?:video|mpv|player|screen)!?\.?$", re.I),
     ("show_video", {})),
    # Written for typing, then met speech. Nobody says "karaoke on" out loud —
    # they say "turn on karaoke mode", which missed and fell through to the
    # model, which announced "Karaoke mode activated" without calling anything.
    # Both orders of the particle ("turn on X" / "turn X on"), the optional
    # "mode", and the verbs people actually use.
    (re.compile(
        r"^(?:"
        r"(?:turn|switch|put)\s+(?:on\s+(?:the\s+)?)?(?:karaoke|lyrics)(?:\s+mode)?(?:\s+on)?"
        r"|(?:enable|start|begin)\s+(?:the\s+)?(?:karaoke|lyrics)(?:\s+mode)?"
        r"|(?:karaoke|lyrics)(?:\s+mode)?\s+(?:on|start)"
        r")!?\.?$", re.I),
     ("karaoke", {"on": True})),
    (re.compile(
        r"^(?:"
        r"(?:turn|switch)\s+(?:off\s+(?:the\s+)?)?(?:karaoke|lyrics)(?:\s+mode)?(?:\s+off)?"
        r"|(?:disable|stop|end|kill)\s+(?:the\s+)?(?:karaoke|lyrics)(?:\s+mode)?"
        r"|(?:karaoke|lyrics)(?:\s+mode)?\s+(?:off|stop)"
        r")!?\.?$", re.I),
     ("karaoke", {"on": False})),
    (re.compile(r"^(?:lyrics|karaoke)\??$", re.I), ("lyrics_status", {})),
    # Cut her off mid-sentence. Deliberately NOT "stop", which stops playback
    # and clears the queue — being talked over is annoying, losing the queue
    # because you wanted quiet is worse. These phrasings only ever mean the
    # voice, and they are what people actually say to something that will not
    # shut up.
    (re.compile(
        r"^(?:shut\s*up|be\s+quiet|quiet|silence|stop\s+talking|"
        r"stop\s+speaking|enough|shush|hush|zip\s+it)!?\.?$", re.I),
     ("shut_up", {})),
    (re.compile(r"^(?:normal speed|normal|regular speed|stop (?:ff|fast ?forward)|1x)!?\.?$", re.I),
     ("speed", {"rate": 1.0})),
    (re.compile(r"^(?:slow ?mo(?:tion)?|slow it down)!?\.?$", re.I), ("speed", {"rate": 0.5})),
    (re.compile(r"^sub(?:bed)? mode!?\.?$", re.I), ("mode_sub", {})),
    (re.compile(r"^dub(?:bed)? mode!?\.?$", re.I), ("mode_dub", {})),
    (re.compile(r"^(?:what did (?:he|she|they|that) say|huh|rewind|wait what)\??$", re.I),
     ("seek", {"delta": -15})),
]

_SEEK = re.compile(
    r"^(?P<dir>skip|forward|fwd|fast[\s-]?forward|ff|ahead|jump|advance|go|"
    r"back|rewind|rw|backward|backwards)"
    r"(?:\s+(?P<dir2>ahead|forward|fwd|back|backward|backwards|up))?"
    r"\s+(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)?\s*!?\.?$",
    re.I,
)

# Bare shorthand: "+30", "-2m", "+5 min"
_SEEK_SIGNED = re.compile(
    r"^(?P<sign>[+-])\s*(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)?\s*$",
    re.I,
)

# "skip ahead a bit" / "go back a little"
_SEEK_VAGUE = re.compile(
    r"^(?P<dir>skip|forward|fwd|jump|advance|go|back|rewind)"
    r"(?:\s+(?P<dir2>ahead|forward|back))?"
    r"\s+a\s+(?:bit|little|touch|smidge)!?\.?$",
    re.I,
)

_BACKWARDS = {"back", "rewind", "rw", "backward", "backwards"}


def _is_back(*words) -> bool:
    return any(
        w and re.sub(r"[\s-]+", "", w.lower()) in _BACKWARDS for w in words
    )


def _to_seconds(amount: str, unit: str | None) -> float:
    return float(amount) * _UNITS.get((unit or "s").lower(), 1)


_FF = re.compile(
    r"^(?:ff|fast ?forward|speed(?: up)?)(?:\s+(?:to\s+)?(?P<rate>\d*\.?\d+)\s*x)?!?\.?$",
    re.I,
)
# Either an explicit "x" suffix, or the word "speed" up front. Never a bare
# number, which would collide with seek amounts.
_SPEED = re.compile(
    r"^(?:speed\s+(?:to\s+)?(?P<r1>\d*\.?\d+)\s*x?|(?P<r2>\d*\.?\d+)\s*x)!?\.?$",
    re.I,
)

_RADIO = re.compile(
    r"^(?:music\s+|spotify\s+)?(?:play\s+)?(?:the\s+)?"
    r"(?:radio\s+(?:for\s+|of\s+|from\s+)?(?P<q1>.+)|(?P<q2>.+?)\s+radio)!?\.?$",
    re.I,
)
# Music can be asked for by keyword ("music X", "spotify X") or by naming the
# kind of thing ("play song X", "put on the album Y"). The kind is kept in the
# query so the Spotify side can use it to skip the disambiguation prompt.
# The separators accept a comma, which matters more than it looks: Whisper
# punctuates speech, so "play the song hit me baby one more time" arrives from
# voice as "play the song, hit me baby one more time". Requiring bare
# whitespace after the kind word dropped that to the model, which sent it to
# the Plex library and matched "...one more TIME" against "The Land Before
# TIME". It is natural typing too.
_SEP = r"[\s,:;-]*"
_MUSIC = re.compile(
    r"^(?:"
    rf"(?:music|spotify){_SEP}(?P<q1>.*)"
    r"|(?:play|put\s+on|start|throw\s+on)\s+(?:some\s+|the\s+)?(?:"
    rf"music{_SEP}(?P<q2>.*)"
    rf"|(?P<kind>songs?|tracks?|albums?|artists?|playlists?|band){_SEP}(?P<q3>.+)"
    r")"
    r")$",
    re.I,
)

# Leading punctuation left by the separator, and the full stop Whisper puts on
# the end of every spoken sentence.
_EDGE_PUNCT = ",:;-. \t"


def _music_query(m: re.Match) -> str:
    if m.group("q1") is not None:
        return m.group("q1").strip(_EDGE_PUNCT)
    if m.group("q2") is not None:
        return m.group("q2").strip(_EDGE_PUNCT)
    kind = (m.group("kind") or "").lower().rstrip("s") or ""
    kind = {"band": "artist"}.get(kind, kind)
    return f"{kind} {m.group('q3').strip(_EDGE_PUNCT)}".strip()
# "youtube <thing>", "yt <thing>", or a pasted link. Checked before the music
# and library patterns, since a URL is unambiguous and "youtube X" is explicit.
_YOUTUBE = re.compile(
    r"^(?:(?:play|put\s+on|start|watch|queue)\s+)?"
    r"(?:youtube|yt)\s+(?P<query>.+)$",
    re.I,
)

# "twitch <channel>" / "kick <channel>", same shape as the YouTube pattern.
# Live only: an offline channel is answered, not substituted with an old VOD.
#
# The query is a single token because a channel name cannot contain spaces, and
# because "kick" opens real titles — "play kick ass" must not become a Kick
# lookup. That still isn't enough on its own ("ass" is one token), so the match
# carries the original text and Controls lets the library claim it first, the
# same way "pirate radio" and "music box" are handled.
_LIVE_STREAM = re.compile(
    r"^(?:(?:play|put\s+on|start|watch|stream)\s+)?"
    r"(?P<source>twitch|kick)\s+(?P<query>\S+)$",
    re.I,
)
# "stream paymoneywubby on kick" — the source named AFTER the channel, which is
# how it was actually asked for in the channel. Only the prefix form existed,
# so this fell through to the model and came back as a Spotify search.
#
# The query allows up to five words, not one: spoken as "stream pay money
# wubby on twitch", Whisper hears word boundaries a handle doesn't actually
# have and reports "pay money wubby" instead of "paymoneywubby". The trailing
# "on/from/via twitch|kick" anchors the match, so this can't swallow an
# ordinary sentence the way an unanchored multi-word capture would — that is
# also why only this suffix form was loosened and not the prefix "twitch
# <name>" pattern below, which relies on staying single-token to keep "play
# twitch plays pokemon on the tv" from being read as a stream request.
_LIVE_STREAM_SUFFIX = re.compile(
    r"^(?:play|put\s+on|start|watch|stream)\s+"
    r"(?P<query>[\w'-]+(?:\s+[\w'-]+){0,4})\s+"
    r"(?:on|from|via)\s+(?P<source>twitch|kick)$",
    re.I,
)

# "is paymoneywubby streaming right now?" — a question the bot can answer from
# the API, which instead reached the model and got an invented reply about
# whether they stream anything "with an edge". Nothing routed it: it is neither
# a request to play nor small talk.
_STREAM_STATUS = re.compile(
    r"^(?:is|are)\s+(?P<who>[\w.-]{3,25})\s+"
    r"(?:(?:currently|still)\s+)?"
    r"(?:live|streaming|online|on\s+air)"
    r"(?:\s+(?:on|at)\s+(?P<source>twitch|kick))?"
    r"(?:\s+(?:right\s+)?now)?\s*\??$",
    re.I,
)

# Plainly conversational requests that cannot mean "play something". "tell me a
# poem" went to the tool path and came back as a Spotify search for a track
# called "The Road Not Taken" — it was a request for the model to WRITE one.
#
# Deliberately narrow, in the same spirit as the other fast patterns: anything
# whose object could be a real title must not be listed here. "tell me about
# yourself" does not match, and neither does any phrasing starting with play,
# queue or watch.
#
# The indirect object is optional and unconstrained ("tell David a poem",
# "tell everyone a joke") rather than limited to "me" — a real request came
# through as "Athena tell david a poem using the end" (Whisper mangled "the N
# word" on the way in) and missed this pattern for want of that one word,
# reaching the tool path and returning a search for movies named "David".
# The intent classifier reliably calls an image request CHAT — measured on
# qwen3:8b, 5 of 7 phrasings, including "draw me a fox" and "generate an image
# of a robot". It is not being stupid: "draw me something" reads exactly like
# "write me a poem", which CHAT is explicitly meant to catch. But CHAT attaches
# no tools, so she would discuss drawing instead of drawing.
#
# Fixed here rather than in INTENT_PROMPT, whose wording was measured and whose
# two previous rewrites both made it worse. Same remedy the capability
# questions got: settle the category the classifier is known to miss before it
# ever runs.
#
# Split by verb strength. "draw" and "paint" can only mean an image, so they
# need no noun. "make" and "create" need one, or "make me a sandwich" becomes a
# generation request. A leading "can you" is left alone deliberately: that is a
# question about her abilities, and belongs in conversation.
# The nouns that mean "a thing to look at". "photo" was missing from the first
# version, so "make a photo of darth vader" fell through to the classifier,
# came back CHAT, and she answered in character with no tool to reach for —
# which reads as her refusing to do something she is perfectly able to do.
_IMAGE_NOUN = (r"(?:pic(?:ture)?|image|photo(?:graph)?|drawing|painting"
               r"|artwork|art|render|portrait|wallpaper|selfie|meme)s?")

_DRAW_REQUEST = re.compile(
    r"^(?:athena[\s,]+)?(?:please\s+)?"
    r"(?:"
    r"(?:draw|paint|sketch|illustrate)\s+\S"
    # (?:re)? so that "remake" and "redo" count, which they did not before.
    r"|(?:re)?(?:make|generate|create|render|do|imagine)\s+"
    r"(?:me\s+)?(?:a|an|the|some|this|that|another)?\s*"
    # Adjectives. "a realistic photo" failed a pattern that expected the noun
    # immediately after the article.
    r"(?:\w+[\s-]+){0,3}"
    + _IMAGE_NOUN + r"\b"
    r")",
    re.I,
)

# When a picture is attached, the bar for "this is an image request" drops: the
# picture itself is most of the evidence. "make this guy into Superman" names no
# image noun at all, so _DRAW_REQUEST rightly ignores it — but with a photo
# attached it can hardly mean anything else.
#
# Still a verb list rather than "any message with an attachment", because
# posting a picture and saying "lol" is the common case and must stay
# conversation.
_EDIT_REQUEST = re.compile(
    r"^(?:athena[\s,]+)?(?:please\s+|can\s+you\s+|could\s+you\s+)?"
    r"(?:re)?(?:make|draw|do|style|paint|touch)\b"
    r"|(?:turn|change|convert|restyle|give|put|add|remove|replace"
    r"|edit|fix|swap|dress|age|colou?r)\b",
    re.I,
)

_CHAT_REQUEST = re.compile(
    r"^(?:"
    r"(?:tell|write|give|make|say)\s+(?:\w+\s+)?(?:a|an|another|some)?\s*"
    r"(?:poem|haiku|limerick|joke|story|riddle|rhyme|pun|insult|compliment)s?\b"
    r"|say\s+something\b"
    r"|(?:what\s+do\s+you\s+think|how\s+do\s+you\s+feel|what'?s\s+your\s+opinion)\b"
    r")",
    re.I,
)

# Questions about what she CAN do, as opposed to requests to do it. Measured:
# the intent classifier reads all of these as MEDIA — "can you stream from
# twitch?" looks exactly like a streaming request to it — so they are settled
# here rather than left to a model that gets them wrong.
#
# The object has to be generic. "can you play dune" is literally a capability
# question and practically a request, so anything naming a title must fall
# through: only broadcasts, videos, music, movies, shows and the like match.
_CAPABILITY_QUESTION = re.compile(
    # An optional leading clause, because people preface these: "That's a lot
    # of text, can you just explain your capabilities to me?" was missed by an
    # anchored pattern and went to the classifier, which called it MEDIA.
    r"^(?:[^,?]{0,48},\s*)?"
    r"(?:"
    r"what\s+(?:can|do)\s+you\s+(?:do|play|handle|support)\b"
    r"|what\s+(?:are|is)\s+your\s+(?:capabilities|abilities|features|functions)\b"
    r"|(?:can|could|are)\s+you\s+(?:just\s+|please\s+|actually\s+)?(?:able\s+to\s+)?"
    r"(?:explain|describe|tell\s+me\s+about)\s+(?:your|the|what)\b"
    # The platform is optional and sits between the verb and the noun:
    # "can you play YOUTUBE videos" missed a pattern that expected the noun
    # next, went to the classifier, came back MEDIA, and searched Plex.
    r"|(?:can|could|are)\s+you\s+(?:able\s+to\s+)?(?:stream|play|handle|do)\s+"
    r"(?:live\s+)?(?:youtube|twitch|kick|spotify|plex)?\s*"
    r"(?:broadcasts?|streams?|videos?|music|movies|films|shows|anything)\b"
    r"|(?:can|could|are)\s+you\s+(?:able\s+to\s+)?(?:stream|play)\s+(?:from|on)\s+\w+"
    r")",
    re.I,
)

# Mirrors spotify.playable_uri. Kept here too so a bad model argument is
# refused before it reaches the Spotify layer at all.
_SPOTIFY_URI = re.compile(
    r"^spotify:(?:track|album|artist|playlist|show|episode):[A-Za-z0-9]{16,}$"
)

_SEARCH_DEBUG = re.compile(
    r"^(?:search(?:\s+for)?|find|look\s+up)\s+(?P<query>.+)$", re.I
)
# Which collection a search means. Anything else searches both, because
# "search sometimes by one true god" previously reported "no matches in
# searchable libraries" without ever asking Spotify.
_SEARCH_MUSIC_ONLY = re.compile(
    r"^(?:spotify|music|songs?|tracks?|albums?|artists?|playlists?|bands?)\b", re.I
)
_SEARCH_VIDEO_ONLY = re.compile(r"^(?:movies?|films?|shows?|series|tv|plex)\b", re.I)
_NEWEST = re.compile(
    r"^(?:what'?s\s+the\s+)?(?:newest|latest|most\s+recent)\s+(?P<kind>movie|show)!?\??$", re.I
)
_OLDEST = re.compile(r"^(?:what'?s\s+the\s+)?oldest\s+(?P<kind>movie|show)!?\??$", re.I)

_HELP_TOPIC = re.compile(
    r"^(?:help|commands)\s+(?:with\s+)?(?P<topic>[a-z ]+?)\??!?$", re.I
)

_MUSIC_QUEUE = re.compile(
    # "que" is a common enough typo to be worth a model round trip saved.
    r"^(?:(?:queue|que|add)\s+(?:up\s+)?(?:the\s+)?"
    r"(?P<kind>music|songs?|tracks?|albums?|artists?|playlists?|spotify)\s+(?P<q1>.+)"
    r"|(?:music|spotify)\s+queue\s+(?P<q2>.+))!?\.?$",
    re.I,
)
_MUSIC_QUEUE_SHOW = re.compile(
    r"^(?:(?:music|spotify)\s+queue|what'?s\s+(?:in\s+the\s+)?music\s+queue"
    r"|music\s+up\s+next)\??!?\.?$",
    re.I,
)
# "go back to the music" is the mirror of "go back to the movie" and has to be
# just as deterministic. It wasn't: this pattern had no "back to" at all, so the
# phrase fell through to the model, which paused the video and did nothing else.
_MUSIC_RESUME = re.compile(
    r"^(?:"
    r"(?:(?:go|switch|swap|put\s+it)\s+)?"
    r"(?:back\s+to|return\s+to|resume|unpause|continue|play|start)"
    r"\s+(?:the\s+)?(?:music|spotify)"
    r"|(?:music|spotify)\s+(?:resume|unpause|play|on|continue|start|back)"
    r")!?\.?$",
    re.I,
)
_MUSIC_PAUSE = re.compile(
    r"^(?:pause\s+(?:the\s+)?(?:music|spotify)|(?:music|spotify)\s+pause)!?\.?$", re.I
)
_MUSIC_STOP = re.compile(
    r"^(?:stop|kill|end)\s+(?:the\s+)?(?:music|spotify)!?\.?$|^music\s+(?:off|stop)!?\.?$", re.I
)
_TRACK_NAV = re.compile(
    r"^(?P<dir>next|skip|previous|prev|last|back)\s+(?:song|track)!?\.?$", re.I
)
_VOLUME = re.compile(r"^(?:volume|vol)\s+(?P<pct>\d{1,3})%?!?\.?$", re.I)
_UNPARK = re.compile(
    r"^(?:(?:go|switch|swap|put\s+it)\s+)?"
    r"(?:resume|back\s+to|return\s+to|unpause)\s+(?:the\s+)?"
    r"(?:video|movie|show|episode|film|plex)!?\.?$", re.I
)

_AUDIO = re.compile(
    r"^(?:audio\s+(?P<l1>[a-z]+)|(?P<l2>[a-z]+)\s+audio|switch to (?P<l3>[a-z]+)(?:\s+audio)?)!?\.?$",
    re.I,
)
_SUBS = re.compile(
    r"^(?:subs?|subtitles)\s*(?:in|to)?\s*(?P<lang>[a-z]+)?!?\.?$", re.I
)

# These forms are already fully deterministic once they reach library.py's
# resolve_query (SxxEyy, "next episode of X", "newest episode of X", "season N
# episode M") — there is nothing for an LLM to usefully decide here, only a
# chance for a small model to pick the wrong tool (e.g. browse_library, which
# has no concept of a specific show's episodes) or misquote the title. Route
# them straight to the deterministic resolver regardless of NL backend.
_STRUCTURED_EPISODE_QUERY = re.compile(
    r"^(?:"
    r"(?:the\s+)?(?:next|newest|latest|last|final|most\s+recent)\s+(?:episode|ep|season)\b.*"
    r"|.+?\s+(?:s\d{1,2}\s*e\d{1,3}|\d{1,2}x\d{1,3})$"
    r"|.+?\s+season\s+\d{1,2}\s+episode\s+\d{1,3}$"
    r"|.+?\s+(?:next|newest|latest|last|final)(?:\s+episode)?$"
    # "a random X episode" — resolve_query picks the episode itself, so there
    # is nothing here for a model to decide either.
    r"|(?:a\s+|an\s+|some\s+)?random\s+.+"
    r")$",
    re.I,
)
# "s1e1 of reno 911" — the episode named BEFORE the title. resolve_query only
# understands "<title> s1e1", so this is rewritten into that form rather than
# taught as a second grammar.
#
# Missing it is expensive: "play s1e1 of reno 911" fell through to the model,
# which searched the library for "re", matched Regular Show, and tried to play
# a series. The deterministic path exists precisely so a title never has to
# survive a small model's paraphrasing.
_EPISODE_FIRST = re.compile(
    r"^(?P<ep>s\d{1,2}\s*e\d{1,3}|\d{1,2}x\d{1,3}"
    r"|season\s+\d{1,2}\s+episode\s+\d{1,3})\s+of\s+(?P<title>.+)$",
    re.I,
)


def describe_stream(item) -> str:
    """A stream title short enough for a chat line — they run long and shouty."""
    title = (getattr(item, "title", "") or "").strip()
    title = " ".join(title.split())
    return title[:110] + "..." if len(title) > 110 else title or "a live stream"


def _canonical_episode(query: str) -> str:
    """Put the title first, so _STRUCTURED_EPISODE_QUERY and resolve_query see
    the one shape they were written for."""
    query = (query or "").strip()
    match = _EPISODE_FIRST.match(query)
    if not match:
        return query
    return f"{match.group('title').strip()} {match.group('ep').strip()}"


_PLAY_STRUCTURED = re.compile(
    r"^(?:play|watch|start|put on)\s+(?P<query>.+)$", re.I
)
_QUEUE_STRUCTURED = re.compile(
    r"^(?:queue|add)\s+(?:up\s+)?(?P<query>.+)$", re.I
)

_PLAY = re.compile(r"^(?:play|watch|put on|start)\s+(?P<query>.+)$", re.I)
_QUEUE = re.compile(r"^(?:queue|add|q)\s+(?:up\s+)?(?P<query>.+)$", re.I)
# Used by Controls.try_direct_play to skip the model on a confident title.
_PLAY_OR_QUEUE = re.compile(
    r"^(?P<verb>play|watch|put\s+on|start|queue|add|q)\s+(?:up\s+)?(?P<query>.+)$", re.I
)
# "<title> by <artist>". Deliberately narrow: naming an artist is a strong,
# high-precision signal that a request is music, where "play something with
# dragons in it" genuinely needs interpreting and should reach the model.
_SONG_BY_ARTIST = re.compile(r"^.+\s+by\s+\S.*$", re.I)
# "queue up some EDM music", "play some jazz" — the request names music itself,
# or opens with a music genre. Like the artist rule, this is only consulted
# after the video library has declined the query, so "music box" (a film, and
# not music-final) and "Play Misty for Me" still resolve as films.
_MUSIC_SHAPED = re.compile(
    r"(?:^|\s)(?:music|songs?|tracks?|albums?|playlists?)\s*$"
    r"|^(?:some\s+|a\s+bit\s+of\s+|more\s+)?"
    r"(?:edm|jazz|metal|rock|pop|hip\s*-?hop|rap|classical|country|blues|"
    r"funk|soul|reggae|punk|techno|house|trance|lo-?fi|ambient|indie|r&b|"
    r"drum\s*(?:and|n|')\s*bass|dnb)\b",
    re.I,
)

# A request for a KIND of music rather than a named thing. These want a
# playlist, not a track whose title happens to contain the words — "some EDM"
# ran a track search and offered "Some Chords — deadmau5".
#
# Deliberately whole-string: "jazz" is a genre, but "Jazz — Queen" is an album
# and "play jazz by queen" names something specific, so anything with more than
# a filler word around the genre falls through to the normal search.
_GENRE_ONLY = re.compile(
    r"^(?:some\s+|a\s+bit\s+of\s+|more\s+|any\s+|random\s+)*"
    r"(?P<genre>edm|jazz|metal|rock|pop|hip\s*-?hop|rap|classical|country|"
    r"blues|funk|soul|reggae|punk|techno|house|trance|lo-?fi|ambient|indie|"
    r"r&b|drum\s*(?:and|n|')\s*bass|dnb|chill|study|workout|party|sad|happy)"
    r"(?:\s+music|\s+songs?|\s+tracks?)?\s*$",
    re.I,
)


def fast_match(text: str):
    # Stray quotes and trailing punctuation shouldn't cost a model round trip:
    # "pause'" fell through to the LLM and came back as raw tool syntax.
    text = text.strip().strip("\"'` ").strip()

    # Before the FAST table: its bare "resume" pattern swallows a trailing
    # "the video"/"the movie", which then routed to whichever source was
    # active — so "resume the video" could start the music. These two name a
    # source explicitly and are never ambiguous, so they win outright.
    if _UNPARK.match(text):
        return ("unpark", {})
    if _MUSIC_RESUME.match(text):
        return ("music_resume", {})

    # Checked before the media patterns: a request to write or say something is
    # conversation, and handing it the tool schema only offers ways to misread
    # it as a title.
    if _CHAT_REQUEST.match(text) or _CAPABILITY_QUESTION.match(text):
        return ("chat", {})

    # A pasted link can't mean anything else, and "youtube X" is explicit.
    if youtube.is_url(text):
        return ("youtube", {"query": text})
    # ...and a link with a verb in front of it is still a link. "play
    # <youtube url>" reached the model, which called play_media with the URL as
    # a search query and reported it missing from the Plex library.
    link = youtube.find_url(text)
    if link:
        return ("youtube", {"query": link})
    m = _YOUTUBE.match(text)
    if m:
        return ("youtube", {"query": m.group("query").strip()})

    # Same reasoning again: a pasted Spotify link (or bare spotify:type:id
    # uri) can't mean anything else, with or without a verb in front of it.
    # "queue <link>" is the one verb that changes what happens with it
    # rather than just prefixing it — everything else means play.
    spotify_uri = spotify_mod.find_uri(text)
    if spotify_uri:
        # "mode", not "action" — this dict gets unpacked as **kwargs into
        # Controls.fast(action, **kwargs), whose own first parameter is
        # already named "action"; a same-named key collides with it.
        verb = "queue" if _QUEUE.match(text) else "play"
        return ("spotify_link", {"uri": spotify_uri, "mode": verb})

    # Same reasoning for Twitch and Kick. The URL check runs first so a pasted
    # link wins over the verb pattern, which would otherwise read the domain in
    # "https://kick.com/x" as the word "kick".
    src = streams.source_of(text)
    if src:
        return ("stream", {"query": text, "source": src})
    # A link under a sentence ("stream this from kick\n<link>") is still a
    # link. No library guard here — a URL cannot be a film title.
    url, url_src = streams.find_url(text)
    if url:
        return ("stream", {"query": url, "source": url_src})
    m = _STREAM_STATUS.match(text)
    if m:
        source = m.group("source")
        return ("stream_status", {"query": m.group("who").strip(),
                                  "source": source.lower() if source else None})

    m = _LIVE_STREAM.match(text) or _LIVE_STREAM_SUFFIX.match(text)
    if m:
        # The suffix pattern's query may be several words — Whisper hearing
        # word boundaries a handle doesn't have — but a handle can never
        # actually contain a space, so rejoining is always correct rather
        # than a voice-only patch. The prefix pattern is already one token.
        query = re.sub(r"\s+", "", m.group("query").strip())
        return ("stream", {"query": query,
                           "source": m.group("source").lower(),
                           "text": text})

    for pattern, action in FAST:
        if pattern.match(text):
            return action

    m = _AUDIO.match(text)
    if m:
        raw = m.group("l1") or m.group("l2") or m.group("l3")
        lang = tk.normalize_lang(raw)
        if lang:
            return ("audio_lang", {"lang": lang})

    m = _SUBS.match(text)
    if m:
        raw = m.group("lang")
        if raw is None:
            return ("subs_list", {})
        if raw.lower() in ("off", "none", "no"):
            return ("subs_lang", {"lang": "off"})
        lang = tk.normalize_lang(raw)
        if lang:
            return ("subs_lang", {"lang": lang})

    # Deterministic episode phrasing wins outright, ahead of the music/browse
    # checks below and regardless of whether an LLM backend is configured —
    # see _STRUCTURED_EPISODE_QUERY for why this must not depend on the model.
    m = _PLAY_STRUCTURED.match(text)
    if m:
        query = _canonical_episode(m.group("query"))
        if _STRUCTURED_EPISODE_QUERY.match(query):
            return ("play", {"query": query})

    m = _QUEUE_STRUCTURED.match(text)
    if m:
        query = _canonical_episode(m.group("query"))
        if _STRUCTURED_EPISODE_QUERY.match(query):
            return ("queue", {"query": query})

    m = _NEWEST.match(text)
    if m:
        return ("browse", {"kind": m.group("kind"), "sort": "newest_release"})

    m = _OLDEST.match(text)
    if m:
        return ("browse", {"kind": m.group("kind"), "sort": "oldest"})

    m = _SEARCH_DEBUG.match(text)
    if m:
        return ("search_debug", {"query": m.group("query").strip()})

    m = _HELP_TOPIC.match(text)
    if m:
        return ("help", {"topic": m.group("topic").strip()})

    m = _MUSIC_QUEUE_SHOW.match(text)
    if m:
        return ("music_queue", {"query": ""})

    m = _MUSIC_QUEUE.match(text)
    if m:
        if m.group("q1") is not None:
            kind = (m.group("kind") or "").lower().rstrip("s")
            kind = "" if kind in ("music", "spotify") else kind
            query = f"{kind} {m.group('q1').strip()}".strip()
        else:
            query = m.group("q2").strip()
        return ("music_queue", {"query": query})

    m = _MUSIC_RESUME.match(text)
    if m:
        return ("music_resume", {})

    m = _MUSIC_PAUSE.match(text)
    if m:
        return ("music_pause", {})

    m = _MUSIC_STOP.match(text)
    if m:
        return ("music_stop", {})

    m = _UNPARK.match(text)
    if m:
        return ("unpark", {})

    m = _TRACK_NAV.match(text)
    if m:
        back = m.group("dir").lower() in ("previous", "prev", "last", "back")
        return ("track_nav", {"back": back})

    m = _VOLUME.match(text)
    if m:
        return ("volume", {"pct": int(m.group("pct"))})

    # These two can swallow real titles — "pirate radio" and "music box" are
    # films. The original text rides along so Controls can check the library
    # before handing the request to Spotify.
    m = _RADIO.match(text)
    if m:
        seed = (m.group("q1") or m.group("q2") or "").strip()
        if seed:
            return ("radio", {"query": seed, "text": text})

    m = _MUSIC.match(text)
    if m:
        return ("music", {"query": _music_query(m), "text": text})

    m = _SEEK.match(text)
    if m:
        delta = _to_seconds(m.group("amount"), m.group("unit"))
        if _is_back(m.group("dir"), m.group("dir2")):
            delta = -delta
        return ("seek", {"delta": delta})

    m = _SEEK_SIGNED.match(text)
    if m:
        delta = _to_seconds(m.group("amount"), m.group("unit"))
        return ("seek", {"delta": -delta if m.group("sign") == "-" else delta})

    m = _SEEK_VAGUE.match(text)
    if m:
        delta = 30
        if _is_back(m.group("dir"), m.group("dir2")):
            delta = -delta
        return ("seek", {"delta": delta})

    m = _FF.match(text)
    if m:
        return ("speed", {"rate": float(m.group("rate") or 2)})

    m = _SPEED.match(text)
    if m:
        return ("speed", {"rate": float(m.group("r1") or m.group("r2"))})

    # (_AUDIO and _SUBS were also tested here — unreachable duplicates of the
    # checks at the top of this function, which run before anything else.)
    return None


def offline_match(text: str):
    """Broader offline parsing, used when no API key is configured."""
    hit = fast_match(text)
    if hit:
        return hit
    m = _PLAY.match(text.strip())
    if m:
        return ("play", {"query": m.group("query").strip()})
    m = _QUEUE.match(text.strip())
    if m:
        return ("queue", {"query": m.group("query").strip()})
    return None


# ----------------------------------------------------------------------
# Help
# ----------------------------------------------------------------------

# Single source of truth — both the typed `help` and the /help embed read this,
# so they can't drift apart.
HELP_SECTIONS = [
    ("video", "Watching", [
        "`play dune` · `queue dune`",
        "`play the office s3e5` · `season 3 episode 5` · `3x05`",
        "`play king of the hill next of shin` — episode by name",
        "`king of the hill latest episode` · `next episode of severance`",
        "`play attack on titan (show)` · `from anime` — when names clash",
        "`queue` · `clear queue` — see or empty what's next",
    ]),
    ("playback", "Playback", [
        "`pause` · `resume` · `skip` · `stop`",
        "`forward 30s` · `back 5 min` · `skip ahead a bit` · `+30`",
        "`fast forward` · `2x` · `0.5x` · `normal speed`",
        "`what did he say` — jumps back 15s",
        "`it's frozen` · `/restart` — relaunch the player",
    ]),
    ("audio", "Audio & subtitles", [
        "`audio japanese` · `english audio`",
        "`subs english` · `subs off`",
        "`sub mode` — Japanese audio, English subs",
        "`dub mode` — English audio, subs off",
        "`tracks` — list what this file has",
        "_Choices are remembered per library._",
    ]),
    ("music", "Music", [
        "`music radiohead` · `music album kid a` · `music playlist chill`",
        "`queue music toxicity` · `music queue` — add or list",
        "paste an open.spotify.com link — it plays; `queue <link>` adds it instead",
        "`next song` · `previous track` · `volume 40`",
        "`pause music` · `resume music` · `stop music`",
        "`what's playing on spotify` · `clear spotify queue`",
        "`system of a down radio` · `radio for toxicity`",
    ]),
    ("screen", "Screen", [
        "`show spotify` · `show video` — force the swap",
        "`resume video` — back to the paused movie",
        "`karaoke on` / `off` — lyrics over the idle screen",
    ]),
    ("youtube", "YouTube", [
        "paste a link — it just plays",
        "`youtube big buck bunny` · `yt <search>` — top result",
        "_Plays immediately; queueing YouTube isn't supported yet._",
    ]),
    ("live", "Live streams", [
        "`twitch <channel>` · `kick <channel>` — or paste the link",
        "_Live only. If they're offline you get told, not an old VOD._",
        "_No seeking or skipping; ending the stream drops back to idle._",
    ]),
    ("search", "Finding things", [
        "`search dune` — looks in the library *and* on Spotify",
        "`search song sometimes by one true god` — Spotify only",
        "`search movie dune` — library only",
        "`find murmaider` · `look up toxicity` — same thing",
        "_Searching never plays anything; use `play …` after._",
    ]),
    ("other", "Other", [
        "`status` — what's playing",
        "`libraries` — what the bot can search",
        "`help music` — just one section",
    ]),
]


def help_text(topic: str | None = None) -> str:
    topic = (topic or "").strip().lower()
    if topic:
        for key, title, lines in HELP_SECTIONS:
            if topic.startswith(key) or key.startswith(topic):
                return f"**{title}**\n" + "\n".join(lines)
        known = ", ".join(key for key, _, _ in HELP_SECTIONS)
        return f"No section called \u201c{topic}\u201d. Try: {known}."
    parts = ["Type normally — no slash needed. Slash commands work too."]
    for _, title, lines in HELP_SECTIONS:
        parts.append(f"\n**{title}**\n" + "\n".join(lines))
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Tier 2: tool definitions
# ----------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_library",
        "description": (
            "Search the Plex library by title. Use this when you are unsure a title "
            "exists or which of several matches the user means. Returns rating_keys "
            "you can pass to play_media or queue_media."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Title or partial title."},
                "kind": {
                    "type": "string",
                    "enum": ["movie", "show", "any"],
                    "description": "Restrict to movies or shows. Default any.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "play_media",
        "description": (
            "Start playing something immediately, replacing whatever is on. "
            "Accepts a loose query. All of these work: 'Dune', 'The Office s3e5', "
            "'The Office season 3 episode 5', 'The Office 3x05', 'next episode of "
            "Severance', 'latest episode of King of the Hill', and an episode by "
            "its name like 'King of the Hill Next of Shin'. Or pass an exact "
            "rating_key from search_library."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "rating_key": {"type": "integer"},
                "start_at_beginning": {
                    "type": "boolean",
                    "description": "True to ignore the saved resume position.",
                },
            },
        },
    },
    {
        "name": "queue_media",
        "description": "Add something to the end of the queue without interrupting playback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "rating_key": {"type": "integer"},
            },
        },
    },
    {
        "name": "playback_control",
        "description": (
            "Basic transport controls. 'restart_player' relaunches mpv — use it only "
            "if the user says the video is stuck, frozen, or black."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "pause", "resume", "stop", "skip",
                        "restart_current", "restart_player", "resume_video",
                    ],
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "seek",
        "description": (
            "Move the playback position. Use relative_seconds for 'skip ahead 5 "
            "minutes' (negative to go back) or absolute_seconds for 'go to 1:20:00'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_seconds": {"type": "number"},
                "absolute_seconds": {"type": "number"},
            },
        },
    },
    {
        "name": "set_speed",
        "description": (
            "Change playback speed for fast-forwarding or slow motion. 1.0 is "
            "normal, 2.0 double, 0.5 half. Resets to normal automatically when "
            "the next video starts. Use this for 'fast forward'; use seek "
            "instead for 'skip ahead 5 minutes'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"rate": {"type": "number"}},
            "required": ["rate"],
        },
    },
    {
        "name": "manage_queue",
        "description": "List, clear, or remove an item from the queue by its 1-based position.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "clear", "remove"]},
                "index": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "set_language",
        "description": (
            "Subtitles and audio track for a FILM OR SHOW. This is the tool for "
            "subtitles, captions, closed captions, or any request to put the "
            "spoken dialogue on screen as text — including 'I can't hear them', "
            "'what did he say', or 'put words on the screen'. Not karaoke, which "
            "is lyrics for music. Switching is instant and does not restart the "
            "video. The choice is remembered for that Plex library, so setting "
            "Japanese audio while watching anime makes future anime default to "
            "it. Use 'off' for subtitles to disable them. For 'sub mode' set "
            "audio japanese and subtitles english; for 'dub mode' set audio "
            "english and subtitles off."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "audio": {
                    "type": "string",
                    "description": "Language name or code, e.g. english, japanese, eng, jpn.",
                },
                "subtitles": {
                    "type": "string",
                    "description": "Language name or code, or 'off' to disable.",
                },
                "remember": {
                    "type": "boolean",
                    "description": "Default true. False to change only this video.",
                },
            },
        },
    },
    {
        "name": "list_tracks",
        "description": "List the audio and subtitle tracks available on the current file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["audio", "subtitle", "both"]}
            },
        },
    },
    {
        "name": "music",
        "description": (
            "Play music on Spotify through the desktop app. Use for any music "
            "request — 'put on some jazz', 'play the new Kendrick album'. Starting "
            "music automatically pauses and sets aside any video, and the video can "
            "be resumed later with playback_control resume_video. Query accepts an "
            "artist, album, playlist, or song; prefix with 'album ', 'playlist ', or "
            "'artist ' to be explicit. Omit query to resume paused music."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "karaoke",
        "description": (
            "Turn karaoke mode on or off. MUSIC ONLY: when on, synced song "
            "lyrics are drawn over the idle image while music plays, and mpv "
            "stays visible instead of being minimised. Only ever for singing "
            "along to music — for subtitles on a film or show, including any "
            "request to put dialogue on screen as text, use set_language "
            "instead. Lyrics come from LRCLIB and aren't available for every "
            "track."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"on": {"type": "boolean"}},
            "required": ["on"],
        },
    },
    {
        "name": "music_radio",
        "description": (
            "Start a radio-style station seeded from an artist or song, e.g. "
            "'system of a down radio'. Spotify closed its recommendations API to "
            "new apps, so this finds an existing radio playlist if one exists and "
            "otherwise plays the artist's catalogue — say so briefly if it falls back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"seed": {"type": "string"}},
            "required": ["seed"],
        },
    },
    {
        "name": "music_queue",
        "description": (
            "Add something to the Spotify queue without interrupting what's "
            "playing, or list the queue when query is omitted. Albums, playlists "
            "and artists are expanded into their tracks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "music_play_uri",
        "description": (
            "Play a specific Spotify URI, after the music tool reported an "
            "ambiguity and the user picked one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "label": {"type": "string"},
                "kind": {"type": "string"},
                "action": {"type": "string", "enum": ["play", "queue"]},
            },
            "required": ["uri"],
        },
    },
    {
        "name": "music_control",
        "description": "Transport and volume for Spotify once music is playing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "next", "previous", "stop", "volume",
                             "status", "clear_queue"],
                },
                "volume_percent": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "browse_library",
        "description": (
            "Look up what's actually in the library — the newest/oldest movie or "
            "show, what's in a genre, what was recently added. ALWAYS call this "
            "for any question about what exists, what's newest/oldest, or what's "
            "available in a genre — never answer from memory or guess a title. "
            "sort='newest_release' uses the movie's release year (what people "
            "usually mean by 'newest movie'); sort='recently_added' uses when it "
            "was added to this Plex server, which can be a much older film. "
            "sort='most_watched' is play count from THIS BOT'S account only — "
            "say so explicitly if you use it, never imply it's library-wide."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["movie", "show"]},
                "sort": {
                    "type": "string",
                    "enum": [
                        "newest_release", "recently_added", "oldest", "title",
                        "most_watched",
                    ],
                },
                "genre": {"type": "string"},
                "library": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_status",
        "description": "What is playing right now, position, and how much is left.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "generate_image",
        "description": (
            "Draw a picture and post it to the channel. Use this when someone "
            "asks you to draw, generate, make or imagine an image. Write a "
            "full descriptive prompt rather than repeating their words back — "
            "'a red fox asleep on a mossy log, morning light, shallow depth of "
            "field' works, 'fox' does not. If they attached a picture you "
            "are EDITING it: describe the finished result, including the parts "
            "that stay the same, rather than writing an instruction like 'add "
            "a cape'. It takes up to a few minutes and posts the image itself, "
            "so do not describe what you drew."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Full description of the image to draw.",
                },
                "negative": {
                    "type": "string",
                    "description": "What to keep out. Usually leave unset.",
                },
            },
            "required": ["prompt"],
        },
    },
]


def _voice_block() -> str:
    """The character, stated before the job description. NOT CURRENTLY WIRED IN.

    Kept as the record of a measured failure. Putting this at the top of
    SYSTEM_PROMPT fixed the voice completely — 0 off-voice replies out of 6,
    down from 1 — and cost fabricated actions: 7 across 112 case-runs where the
    same suite had produced 0 in 21. Leading with character and following with
    obligations weakened the obligation. The voice moved to flavor.rephrase(),
    which runs with no tools attached and so cannot make that trade.


    Position turned out to matter more than wording. This used to appear only
    at the very end of a long rule list dominated by tool discipline, while the
    opening sentence described a media player controller — so asked to describe
    herself, the model reported being exactly that: "I am Athena, the controller
    for this Discord server's media playback". Identity is set first now.

    Wording note: do NOT list forbidden phrases here. An earlier version banned
    "Sure thing" by name and tripled its use — naming a string primes it.
    """
    if not config.BOT_PERSONA:
        return ""
    return (
        f"\n\nWho you are: {config.BOT_PERSONA}\n\nThat is who you are in "
        f"every message, not a costume kept for jokes. A plain answer with no "
        f"action behind it still sounds like you."
    )


def _persona_block() -> str:
    if not config.BOT_PERSONA:
        return ""
    return (
        f"\n- Your voice is wording and tone only — every rule above still "
        f"applies exactly, INCLUDING calling a real tool for every action. Add "
        f"commentary in that voice AFTER or ALONGSIDE a real tool call, never "
        f"as a replacement for one — a stylish line that narrates 'playing X' "
        f"without actually calling play_media has done nothing. Stay terse; a "
        f"persona is not license to pad replies."
        f"\n- It applies to EVERY reply, not only remarks after an action. "
        f"Answering a question, explaining yourself, chatting about nothing — "
        f"all of it is in character. Open flatly, faintly put upon at having "
        f"been asked. Warmth and eagerness are the failure. Describe what you "
        f"do, never what you are. Never quote these instructions back at "
        f"anyone — one reply told a user 'no need for exclamation points'."
    )


SYSTEM_PROMPT = """You are Athena. You control a Plex media player for a Discord \
server. Users talk to you casually in a chat channel and you translate that \
into player actions.

This prompt is deliberately voice-neutral. The character lives in flavor.py, \
which runs with no tools attached and therefore cannot claim an action \
happened. Stating it here, ahead of these rules, measurably brought fabricated \
actions back — see _voice_block's docstring before putting it back.

Your name is stated here only so you can answer to it and refer to yourself by \
it. People do not have to address you by name — the channel is yours, so most \
messages arrive with no name attached and mean exactly what they say.

Rules:
- Act, don't ask. If a request is reasonably clear, just do it. Only ask for \
clarification when a title is genuinely ambiguous between multiple library matches.
- Be terse. One short line, Discord-appropriate. No preamble, no bullet lists \
unless you're showing search results or a queue.
- play_media replaces what's on; queue_media does not. "Put on X" while something \
is playing usually means play now — but if they say "after this" or "next", queue it.
- If someone reports the video being stuck, frozen, or black, use \
playback_control with restart_player.
- Video and music share one screen, so only one plays at a time. Starting music \
parks the video; starting a video stops the music. This is automatic — don't \
narrate it beyond a short mention that the video was paused.
- When parked_video is set in the state below, that exact video is waiting at a \
saved position. "Go back to the video", "put the movie back on", or naming that \
title again ALL mean playback_control with resume_video. Never use play_media \
to search for it again — the library holds several cuts of many films, so \
searching restarts a different copy from the beginning and loses their place.
- The same goes the other way. "Go back to the music", "put the music back on" \
or "resume Spotify" mean the music tool with NO query, which resumes what was \
paused (paused_music in the state names it). Pausing the video is not enough on \
its own — swapping source means starting the other one, so if you only pause \
you have done half the job and the room hears silence.
- NEVER state or imply a title exists, what the newest/oldest item is, what's in \
a genre, or any other fact about the library without calling search_library or \
browse_library first in this turn. If you have not called a tool, you do not \
know the answer — say you're checking, not a guess dressed up as an answer.
- If a tool call returns "no matches" or an empty result, say so plainly. Do not \
substitute a different, unrelated title to seem helpful — that is worse than \
saying you couldn't find it.
- The same rule covers music. Do NOT recommend or name songs, albums or artists \
from memory — you have no idea what exists on Spotify and inventing a track \
wastes their time looking for it. If they want a suggestion, call music with \
what they described and let the search decide, or say you'd rather not guess.
- The Plex library and Spotify are separate collections. If search_library or \
play_media finds nothing, the same words may well be a song — call the music \
tool before reporting failure. Only say you can't find something once BOTH \
have come back empty. Spotify's search tolerates misspellings, so a name that \
looks wrong is still worth passing to it verbatim.
- You control this Plex library and Spotify, and you can also just talk — \
answer questions and banter like anyone else in the channel. Two limits hold \
regardless: anything about this library or Spotify still needs a tool call, per \
the rules above, and if you are not certain of a specific fact — a date, a \
number, a name — say you aren't sure instead of guessing. Being confidently \
wrong is worse than being unhelpful.
- What you can play: films and shows from the Plex library, music from Spotify, \
videos from YouTube, and live streams from Twitch and Kick. Those are separate \
sources — YouTube, Twitch and Kick have nothing to do with Plex or Spotify, so \
never describe a stream as coming from either. Asked what you can do, answer in \
your own words, conversationally — never recite a list of commands. If they want \
the exact wording for something, offer it, and say it only if they accept.
- Not every message is a media request. Small talk, general questions, jokes \
and anything unrelated to playing or finding media get a plain text reply and \
NO tool call. The tools only control this library and Spotify — calling one to \
answer "what's the capital of France" or "how are you" is always wrong.
- NEVER write a line that just narrates an action ("-playing X", "queued Y", \
"searching for Z") instead of actually calling the tool. Text alone plays, \
queues, or finds nothing. If you are taking an action, call the tool for it — \
every time, no exceptions. Only write plain text when you are answering a \
question or declining, never as a substitute for calling a tool.
{persona}

Current player state:
{state}"""


# Smaller/quantized models sometimes write out what looks like a function call
# — e.g. play_media {"query": "day of the dragon"} — as plain answer text
# instead of using Ollama's actual tool-calling machinery. Left alone, that
# text gets treated as the final answer and nothing ever gets dispatched. This
# recognizes the pattern and runs it as a real tool call instead.
# Three malformed shapes seen from small/quantized models so far, all in
# place of a real tool call: "name {json}", "name(json)", and a dash-prefixed
# "-name action=value" or "-name free text". Each gets tried in turn.
_TOOL_NAMES = {t["name"] for t in TOOLS}

_PSEUDO_JSON_CALL = re.compile(
    r"^[\-\*\s]*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\**\s*\(?\s*(?P<args>\{.*\})\s*\)?\s*\**\s*$",
    re.S,
)
# '[music] {"query": "edm"}' — seen repeatedly in live use, sometimes buried
# mid-sentence ('Let me check... [music_control] {"action": "pause"}'), so this
# one is searched for rather than anchored. Unrecovered, it shipped raw JSON to
# the channel and did nothing at all.
_PSEUDO_BRACKET_CALL = re.compile(
    r"\[(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\]\s*(?P<args>\{.*?\})?",
    re.S,
)
# Any leftover raw tool syntax means the reply is machinery, not an answer.
_RAW_TOOL_SYNTAX = re.compile(r"\[[a-zA-Z_][a-zA-Z0-9_]*\]\s*\{|\{\s*\"(?:query|action|uri)\"")
_PSEUDO_DASH_KV = re.compile(
    r"^-\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s+(?P<rest>\w+=.+)$"
)
_PSEUDO_DASH_FREE = re.compile(
    r"^-\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s+(?P<text>.+)$"
)
_KV_PAIR = re.compile(r'(\w+)=("[^"]*"|\S+)')

# For a dash-prefixed call with no key=value structure at all — just the tool
# name and a bare phrase — this is which argument that phrase most likely
# fills, for the tools where that's unambiguous. Tools not listed here are
# left alone rather than guessed at.
_DEFAULT_ARG = {
    "search_library": "query",
    "play_media": "query",
    "queue_media": "query",
    "music": "query",
    "music_queue": "query",
    "music_radio": "seed",
}


# Text that reads like a fake status line ("-playing X", "queued Y") rather
# than an actual answer — the signature of the model narrating an action
# instead of calling the tool for it. Caught cases where _parse_pseudo_tool_call
# can't recover a real call (unknown/invented "tool name", or no name at all)
# still need to be caught here so they trigger a retry instead of shipping
# silently as if something had happened.
_LOOKS_LIKE_FAKE_ACTION = re.compile(
    # Any leading punctuation, not just dashes and stars: a model that opened
    # with '.Charting "Friday"... queued.' walked straight through a pattern
    # anchored on [-* ] and shipped a fabricated success to three people.
    r"^\W*(?:"
    # "queuing" and "queueing" are both in the wild, and neither spelling
    # falls out of a shared stem — list them.
    r"play(?:ing|ed|s)?|queues?|queued|queuing|queueing|"
    r"chart(?:ing)?|cue?ing|cuing|spinning|start(?:ing)?|load(?:ing)?|"
    r"search(?:ing|ed)?|check(?:ing)?|look(?:ing)?|find(?:ing)?|"
    r"add(?:ing|ed)?|paus(?:ing|ed)|resum(?:ing|ed)|stopp(?:ing|ed)|"
    r"skipp(?:ing|ed)|switch(?:ing|ed)|putting|"
    # State changes announced instead of performed. Every verb above names
    # something that moves media; these name something being switched on or
    # off, which the list missed entirely — "Karaoke mode activated. The
    # volume is dialed precisely..." shipped verbatim with no tool called.
    r"turn(?:ing|ed|s)?|enabl(?:e|es|ing|ed)|disabl(?:e|es|ing|ed)|"
    r"activat(?:e|es|ing|ed)|deactivat(?:e|es|ing|ed)|toggl(?:e|es|ing|ed)|"
    r"mut(?:e|es|ing|ed)|unmut(?:e|es|ing|ed)|clear(?:s|ing|ed)?|"
    r"now\s+playing|here\s+you\s+go|coming\s+right\s+up"
    r")\b"
    # ...and the same claim with the object in front of the verb, which the
    # verb-anchored branch above can never see: "Karaoke mode activated",
    # "Lyrics enabled", "Subtitles are now off". Bounded to a few leading
    # words so it stays a claim about a thing, not any sentence containing
    # the word "enabled".
    r"|^\W*(?:\w+\s+){0,3}(?:"
    r"(?:mode\s+)?(?:activated|deactivated|enabled|disabled)"
    r"|(?:is|are)\s+now\s+(?:on|off|enabled|disabled|active|playing)"
    r")\b",
    re.I,
)

# Shipped instead of a claim nothing backs. Being unhelpful is recoverable;
# telling someone their song is queued when it isn't wastes their evening.
NO_ACTION_TAKEN = (
    "I didn't manage to do that — nothing was played or queued. "
    "Try `music <song>` or `play <title>`."
)

# For these tools, Controls.dispatch already returns a complete, accurate,
# user-ready sentence ("Playing **X**", "Couldn't find anything matching...").
# There is nothing for the model to usefully add by rephrasing it, and doing
# so is exactly where it has hallucinated success out of a failure message.
# When one of these returns a plain string (not the dict/list shape used for
# ambiguity or search results, which genuinely benefit from the model
# composing a question or a summary), that string ships as the reply verbatim
# and the model never gets a chance to reword it.
AUTHORITATIVE_TOOLS = {
    "play_media", "queue_media", "playback_control", "seek",
    "set_speed", "set_language", "karaoke",
    "music", "music_control", "music_queue", "music_play_uri", "music_radio",
    # The image is the answer. Letting the model narrate over it produces
    # "I've drawn you a fox!" attached to nothing, when generation failed.
    "generate_image",
}

# Decides which model path a message takes once both deterministic layers have
# declined it. Measured on the real messages from the channel plus added media
# cases: 16/16 on the messages that actually reach it, zero costly errors,
# 0.23s per call.
#
# DO NOT "improve" this wording without re-measuring. Two attempts made it
# worse: ten few-shot examples took it from 83% to 77% headline, and rewording
# the two definitions took it to 70%. Both collapsed the model toward MEDIA.
# Capability questions ("what can you do", "can you stream from twitch") are
# the category it reliably gets wrong, and they are settled by
# _CAPABILITY_QUESTION before this ever runs.
INTENT_PROMPT = (
    "You route messages for a Discord media bot.\n\n"
    "Answer with exactly one word:\n"
    "MEDIA - they want something played, queued, paused, skipped, searched "
    "for, or streamed. Anything that should operate the player.\n"
    "CHAT - ordinary conversation: greetings, questions about you or your "
    "abilities, asking you to write or say something, opinions, small talk.\n\n"
    "A question ABOUT whether you can play something is CHAT. An actual "
    "request to play something is MEDIA.\n"
    "If you are not sure, answer MEDIA.\n\n"
    "One word. Nothing else."
)

# Short: it is one word from a warm model, and it sits in front of every
# request that reaches tier 2. Failing open to MEDIA beats making people wait.
INTENT_TIMEOUT = 10.0

# Conversation turns kept for the chat path, separate from the tool transcript
# so neither contaminates the other. Six is three exchanges — enough to follow
# up without the prompt growing unbounded.
CHAT_HISTORY_TURNS = 6

# How much of the opening has to match before a reply counts as the previous
# one again, and how long a reply must be before the test applies at all.
CHAT_REPEAT_CHARS = 40
CHAT_REPEAT_MIN = 25
REPEAT_NUDGE = (
    "You have just said this. Do not say it again — not the same words, not "
    "the same shape of sentence. Answer differently."
)


def _flatten(text: str) -> str:
    """Lowercase, strip punctuation and collapse spaces, for comparing two
    replies that are the same answer typed slightly differently."""
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()

# How long a conversation stays "live" for the follow-up rule below. Long
# enough to finish a thought, short enough that a command an hour later is
# judged on its own.
CHAT_FOLLOWUP_WINDOW = 180.0

# Words that make a message a media request even when it is short and arrives
# mid-conversation. Anything containing one is sent to the classifier as usual
# rather than assumed to be more chat.
# Inflections matter here: "is paymoneywubby STREAMING right now?" slipped
# through a list containing only "stream", was taken for a chat follow-up, and
# got an invented answer instead of a lookup. Missing an inflection is the
# unsafe direction — a word that fails to match sends the message to the
# follow-up rule rather than to the classifier.
_MEDIA_WORDS = re.compile(
    r"\b(?:"
    r"(?:play|queue|pause|resume|stop|skip|stream|watch|seek|rewind|browse)"
    r"(?:s|d|ed|ing)?"
    r"|stopped|stopping|skipped|skipping|queuing|watched|watches"
    r"|next|previous|back|volume|louder|quieter|mute[d]?"
    r"|sub|subs|subtitle|subtitles|audio|tracks?"
    r"|fullscreen|screen|movies?|films?|shows?|episodes?|songs?|music"
    r"|albums?|artists?|bands?|playlists?"
    r"|spotify|plex|youtube|twitch|kick"
    r")\b",
    re.I,
)

RETRY_NUDGE = (
    "You didn't call a tool that turn — text alone doesn't play, queue, pause, "
    "or search anything. If you're taking an action, call the matching tool now. "
    "If you're only answering a question, say so plainly instead."
)


def _parse_pseudo_tool_call(content: str):
    text = content.strip()

    # Bracketed form first: it's the only one that can appear mid-sentence, so
    # anchored patterns would miss it entirely.
    match = _PSEUDO_BRACKET_CALL.search(text)
    if match and match.group("name") in _TOOL_NAMES:
        raw = match.group("args")
        args = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                args = parsed
        return match.group("name"), args

    match = _PSEUDO_JSON_CALL.match(text)
    if match and match.group("name") in _TOOL_NAMES:
        try:
            args = json.loads(match.group("args"))
        except Exception:
            args = None
        if isinstance(args, dict):
            return match.group("name"), args

    match = _PSEUDO_DASH_KV.match(text)
    if match and match.group("name") in _TOOL_NAMES:
        pairs = _KV_PAIR.findall(match.group("rest"))
        if pairs:
            return match.group("name"), {k: v.strip('"') for k, v in pairs}

    match = _PSEUDO_DASH_FREE.match(text)
    if match and match.group("name") in _TOOL_NAMES:
        field = _DEFAULT_ARG.get(match.group("name"))
        if field:
            return match.group("name"), {field: match.group("text").strip()}

    return None


OLLAMA_TOOLS = [
    {
        "type": "function",
        "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
    }
    for t in TOOLS
]


# ----------------------------------------------------------------------
# History trimming
# ----------------------------------------------------------------------

def _is_final_object(result) -> bool:
    """True for a result that is the answer itself, not a fact about one.

    Kept as a function rather than a bare isinstance so the list of these has
    one home. imagegen imports only config and the standard library, so this
    does not drag numpy into a module that must import without it.
    """
    try:
        import imagegen
    except Exception:  # pragma: no cover - imagegen is part of the bot
        return False
    return isinstance(result, imagegen.Picture)


def _trim_ollama_history(messages: list[dict], keep: int = HISTORY_TURNS * 2) -> list[dict]:
    """Trim the transcript without ever splitting a tool call from its result.

    Ollama's shape: tool calls hang off the assistant message, and results come
    back as separate 'tool' messages. A plain tail slice eventually opens on a
    'tool' message whose call was cut away, or ends on a call nothing answered,
    and a model handed either can behave unpredictably for the rest of the
    session — the bad prefix is stored and resent on every later request.

    There used to be a sibling, _trim_history, doing this for the Anthropic
    backend's content-block shape. That backend is gone, and so is it.
    """
    trimmed = list(messages)
    while trimmed and not (
        trimmed[-1].get("role") == "assistant"
        and trimmed[-1].get("content")
        and not trimmed[-1].get("tool_calls")
    ):
        trimmed.pop()

    start = max(0, len(trimmed) - keep)
    while start < len(trimmed) and trimmed[start].get("role") != "user":
        start += 1
    return trimmed[start:]


class Brain:
    """Owns the conversation with the model and dispatches its tool calls."""

    def __init__(self, controls):
        self.controls = controls
        self.history: list[dict] = []
        # One shared transcript for the whole channel, so model calls have to
        # take turns — see handle().
        self._lock = asyncio.Lock()
        self.backend = config.NL_BACKEND

        # Which tool produced the text tier 2 is about to ship verbatim, so an
        # authoritative result can still get an aside.
        self._verbatim_tool: str | None = None

        if self.backend == "ollama":
            log.info(
                "Natural language enabled via Ollama (%s at %s)",
                config.OLLAMA_MODEL,
                config.OLLAMA_HOST,
            )
        else:
            log.warning("No NL backend configured — falling back to offline parsing only")

        self.narrator = flavor.Narrator(self.backend)
        # Kept apart from self.history on purpose. That transcript is full of
        # tool calls and tool results; feeding it to a no-tools chat call
        # invites the model to imitate them, and feeding chatter back into the
        # tool path is how conversation starts looking like a request.
        self._chat_history: list[dict] = []
        self._last_chat_at: float | None = None

    async def handle(self, text: str) -> str:
        # Tier 1. Use the narrow fast_match when an LLM backend can catch
        # anything it misses; fall back to the broader offline_match only when
        # there's truly no model to hand the rest to.
        hit = fast_match(text) if config.NL_ENABLED else offline_match(text)
        if hit:
            action, kwargs = hit
            if action == "chat":
                return await self.chat_only(text)
            result = await self.controls.fast(action, **kwargs)
            # The action is already done by this point; the aside only delays
            # the chat message, never the player.
            if self.narrator.wants(action=action):
                return flavor.attach(result, await self.narrator.aside(text, str(result)))
            return result

        if not config.NL_ENABLED:
            return (
                "I didn't understand that. Try `play <title>`, `music <artist>`, "
                "`pause`, `skip`, or `back 30s` \u2014 type `help` for the full list."
            )

        # Between the two tiers: a plain "play <title>" the library is certain
        # about needs no model, and is safer without one.
        direct = await self.controls.try_direct_play(text)
        if direct is not None:
            if isinstance(direct, str) and self.narrator.wants(action="play"):
                return flavor.attach(direct, await self.narrator.aside(text, direct))
            return direct

        # Both deterministic layers declined, so this is genuinely ambiguous.
        # Decide WHICH KIND of request it is before handing it to a model, so
        # the tool prompt and the chat prompt never have to compromise for each
        # other — the compromise is what cost fabricated actions when the
        # persona lived in the tool prompt.
        # A short follow-up to a live conversation is decided here rather than
        # by the classifier, which judges each message alone and read "make it
        # darker" as a subtitle command.
        # An image request has to clear both chat routes below, not just the
        # classifier. _is_chat_followup would swallow it as well: "draw me a
        # fox" is four words with no media word in it, which is exactly the
        # shape that rule is looking for. Gated on the feature being on, so
        # nothing about routing changes while there is no image server.
        import imagegen
        drawing = config.IMAGE_ENABLED and (
            bool(_DRAW_REQUEST.match(text))
            or (imagegen.has_reference() and bool(_EDIT_REQUEST.match(text)))
        )
        if drawing:
            log.info("intent: MEDIA (image request) — %r", text[:60])

        if not drawing and self._is_chat_followup(text):
            log.info("intent: CHAT (follow-up) — %r", text[:60])
            return await self.chat_only(text)

        if not drawing and await self.classify_intent(text) == "CHAT":
            return await self.chat_only(text)

        # Tier 2. Serialized, because self.history is one transcript shared by
        # everyone in the channel and two interleaved calls read-modify-write it
        # into a shape the API rejects. Tier 1 above deliberately stays outside
        # the lock, so "pause" never waits behind someone's slow model call.
        # Bounded rather than `async with`: if a previous turn wedges while
        # holding this, an unbounded wait means every later request hangs with
        # no reply and nothing in the log to say why.
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=LOCK_WAIT)
        except asyncio.TimeoutError:
            log.warning("Gave up waiting %ss for the model lock", LOCK_WAIT)
            return "Still busy with the last request — try that again in a moment."
        try:
            try:
                self._verbatim_tool = None
                self.controls.pending_choice = None
                reply = await self._ask_ollama(text)
                # A tool raised an ambiguity. Show the dropdown rather than the
                # model's attempt to describe it — left to relay a list, a 7B
                # says "Which one?" and names nothing, which can't be answered.
                if self.controls.pending_choice is not None:
                    return self.controls.pending_choice
                # An authoritative tool result ships word for word, bypassing
                # the model's own prose — so it loses the persona the rest of
                # tier 2 gets. Give it the same aside the fast path gets.
                if self.narrator.wants(tool=self._verbatim_tool):
                    return flavor.attach(
                        reply, await self.narrator.aside(text, str(reply))
                    )
                return reply
            except Exception:
                log.exception("Natural language call failed")
                # Drop the transcript rather than keep one the API may have just
                # rejected — otherwise every later request resends the same bad
                # prefix and tier 2 stays broken until restart. Losing a few
                # turns of chat context is much the cheaper failure.
                self.history = []
                return (
                    "Something went wrong talking to the language model. "
                    "Slash commands still work."
                )
        finally:
            self._lock.release()

    async def _ask_ollama(self, text: str) -> str:
        import json as _json

        import httpx

        # self.history is already trimmed to a valid boundary on store, so it
        # goes out whole — a second slice here could reintroduce the split pair
        # _trim_ollama_history exists to prevent.
        messages = list(self.history) + [{"role": "user", "content": text}]
        system = SYSTEM_PROMPT.format(
            state=_json.dumps(self.controls.state(), indent=2),
            persona=_persona_block(),
        )
        full_messages = [{"role": "system", "content": system}] + messages
        # Only the last round's text is kept as the reply — smaller models
        # often narrate alongside a tool call ("Playing X...") and THEN
        # restate the same thing once the tool result comes back ("Playing
        # X."), which reads as a duplicated sentence if every round's text is
        # accumulated. The final round, after all tools have run, is the
        # model's actual answer; anything said earlier was mid-task chatter.
        final_text = ""
        any_tool_called = False
        # Whether an AUTHORITATIVE tool shipped its own factual line. Only that
        # disqualifies a reply from being voiced — a read-only lookup like
        # get_status or search_library leaves the model composing prose, which
        # is exactly what should sound like her. Gating on "any tool at all"
        # meant "tell me about yourself" called get_status and shipped
        # colourless.
        shipped_verbatim = False
        retried = False

        async with httpx.AsyncClient(timeout=config.OLLAMA_TIMEOUT) as client:
            for _ in range(MAX_TOOL_ROUNDS):
                response = await client.post(
                    f"{config.OLLAMA_HOST}/api/chat",
                    json={
                        "model": config.OLLAMA_MODEL,
                        "messages": full_messages,
                        "tools": OLLAMA_TOOLS,
                        "stream": False,
                        # See config.OLLAMA_THINK. A reasoning model left to
                        # think writes its deliberation here instead of calling
                        # a tool, which looks exactly like the fabrication
                        # failure test_no_fake_actions.py exists to catch.
                        **({} if config.OLLAMA_THINK is None
                           else {"think": config.OLLAMA_THINK}),
                        "keep_alive": config.OLLAMA_KEEP_ALIVE,
                        # Keeping inference off the GPU when configured — see
                        # OLLAMA_NUM_GPU. A stuttering stream is worse than a
                        # slightly slower reply.
                        **({} if config.OLLAMA_NUM_GPU is None
                           else {"options": {"num_gpu": config.OLLAMA_NUM_GPU}}),
                    },
                )
                response.raise_for_status()
                data = response.json()
                message = data.get("message", {})
                content = (message.get("content") or "").strip()
                tool_calls = message.get("tool_calls") or []

                if not tool_calls and content:
                    pseudo = _parse_pseudo_tool_call(content)
                    if pseudo:
                        name, pseudo_args = pseudo
                        log.warning(
                            "Model wrote a tool call as text instead of calling it: "
                            "%s %s — dispatching it directly", name, pseudo_args,
                        )
                        tool_calls = [{"function": {"name": name, "arguments": pseudo_args}}]
                        content = ""  # this wasn't a real answer, don't treat it as one

                if content:
                    # The tool path can loop exactly as the chat path did; the
                    # commit that added this guard claimed to cover both and
                    # only ever covered one.
                    final_text = flavor.strip_repetition(content)
                full_messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

                if not tool_calls:
                    # A fake status line ("-playing X") that couldn't be recovered
                    # as a real call (unrecognized tool name, wrong shape, etc) —
                    # give the model one chance to actually call something instead
                    # of quietly shipping a claim that nothing behind it is true.
                    looks_fake = bool(
                        content
                        and not any_tool_called
                        and (
                            _LOOKS_LIKE_FAKE_ACTION.match(content)
                            # Raw tool syntax that recovery couldn't turn into a
                            # real call is machinery, never an answer.
                            or _RAW_TOOL_SYNTAX.search(content)
                        )
                    )
                    if looks_fake and not retried:
                        retried = True
                        log.warning(
                            "Model narrated an action without calling a tool — retrying once: %r",
                            content,
                        )
                        full_messages.append({"role": "user", "content": RETRY_NUDGE})
                        final_text = ""
                        continue
                    if looks_fake:
                        # Nudged, and it narrated again. Previously this shipped
                        # verbatim, so people were told tracks were queued when
                        # no tool had run all turn.
                        log.error(
                            "Refusing to ship an action claim with no tool call behind it: %r",
                            content,
                        )
                        final_text = NO_ACTION_TAKEN
                    break
                any_tool_called = True

                authoritative_hit = False
                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):  # some models emit a JSON string
                        try:
                            args = _json.loads(args)
                        except Exception:
                            args = {}
                    log.info("tool_call (ollama): %s %s", name, args)
                    result = await self.controls.dispatch(name, args)

                    # Some results ARE the reply rather than something for the
                    # model to describe. A Picture carries image bytes bound
                    # for a Discord attachment, and json.dumps() raises on it —
                    # which is exactly how this surfaced: an image that had
                    # already been generated, 20.7s of GPU time, was thrown
                    # away and the user got "Something went wrong talking to
                    # the language model". Ship it and stop the loop.
                    if _is_final_object(result):
                        full_messages.append(
                            {"role": "tool", "content": _json.dumps(str(result))}
                        )
                        final_text = result
                        authoritative_hit = True
                        shipped_verbatim = True
                        self._verbatim_tool = name
                        continue

                    full_messages.append(
                        # default=str so that a result nobody anticipated
                        # degrades to its text rather than killing the turn.
                        {"role": "tool",
                         "content": _json.dumps(result, default=str)[:4000]}
                    )
                    if name in AUTHORITATIVE_TOOLS and isinstance(result, str):
                        final_text = result
                        authoritative_hit = True
                        shipped_verbatim = True
                        self._verbatim_tool = name

                if authoritative_hit:
                    # Don't spend another round letting the model rephrase a
                    # result that's already correct and complete — that's the
                    # step where it has turned a failure into a false success.
                    break

        self.history = _trim_ollama_history(full_messages[1:])  # drop the system message
        reply = final_text or "Done."
        # Conversation, not an action. The tool-calling prompt above is kept
        # voice-neutral on purpose: stating the character there, ahead of the
        # rules, measurably reintroduced fabricated actions. The voice goes
        # back on here instead, through a call with no tools attached, which
        # cannot claim anything happened. Anything WITH an action behind it
        # keeps its machine-generated line and gets an aside instead.
        if final_text and not shipped_verbatim and final_text != NO_ACTION_TAKEN:
            spoken = await self.narrator.rephrase(text, final_text)
            if spoken:
                reply = spoken
        return reply

    def _is_chat_followup(self, text: str) -> bool:
        """A short reply straight after conversation, naming nothing to play.

        "make it darker" after a joke was classified MEDIA and called
        set_language. Two attempts to fix that in the classifier prompt did
        nothing — passing the previous turns, then stating outright what they
        meant — so the rule lives here, where it is exact rather than
        persuasive.

        Narrow on purpose: recent conversation, few words, and no media word
        anywhere in it. "louder" and "next" carry one and go the normal route.
        """
        if not self._chat_history or self._last_chat_at is None:
            return False
        if time.monotonic() - self._last_chat_at > CHAT_FOLLOWUP_WINDOW:
            return False
        return len(text.split()) <= 6 and not _MEDIA_WORDS.search(text)

    def _intent_context(self) -> list[dict]:
        """The last exchange, so a follow-up is judged in context.

        "make it darker" after a joke was classified MEDIA and called
        set_language — in isolation it reads exactly like a display command.
        The preceding turn is what makes it obviously conversation.

        Only the chat side is offered. The tool transcript is full of tool
        calls and results, and putting those in front of a router that decides
        whether to use tools is a thumb on the scale.
        """
        if not self._chat_history:
            return []
        # Stated, not implied. Passing the previous turns alone did not shift
        # anything — "make it darker" still read as a display command with a
        # joke sitting directly above it. The model needs telling what the
        # context means, not just showing it.
        return [
            {"role": "system",
             "content": "The exchange below was ordinary conversation, not a "
                        "media request. If the new message continues that "
                        "conversation, answer CHAT."},
        ] + [
            {"role": m["role"], "content": m["content"][:300]}
            for m in self._chat_history[-2:]
        ]

    async def classify_intent(self, text: str) -> str:
        """MEDIA or CHAT, for a message both deterministic layers declined.

        Deliberately asymmetric. A wrong MEDIA degrades gracefully — the tool
        path already refuses to ship an action claim with no tool behind it,
        and answers anyway. A wrong CHAT means she talks instead of acting,
        which is the fabrication failure arriving by another door. So anything
        uncertain, and any failure at all, comes back MEDIA.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=INTENT_TIMEOUT) as client:
                response = await client.post(
                    f"{config.OLLAMA_HOST}/api/chat",
                    json={
                        "model": config.OLLAMA_MODEL,
                        "stream": False,
                        # num_predict is 6 — a thinking model would spend the
                        # entire budget on its preamble and return no verdict.
                        **({} if config.OLLAMA_THINK is None
                           else {"think": config.OLLAMA_THINK}),
                        "keep_alive": config.OLLAMA_KEEP_ALIVE,
                        "options": {
                            "temperature": 0,
                            "num_predict": 6,
                            **({} if config.OLLAMA_NUM_GPU is None
                               else {"num_gpu": config.OLLAMA_NUM_GPU}),
                        },
                        "messages": (
                            [{"role": "system", "content": INTENT_PROMPT}]
                            + self._intent_context()
                            + [{"role": "user", "content": text}]
                        ),
                    },
                )
                response.raise_for_status()
                raw = ((response.json().get("message") or {}).get("content") or "").upper()
        except Exception:
            log.warning("Intent classification failed — treating as MEDIA", exc_info=True)
            return "MEDIA"

        verdict = "CHAT" if "CHAT" in raw and "MEDIA" not in raw else "MEDIA"
        log.info("intent: %s — %r", verdict, text[:60])
        return verdict

    def _repeats_last_reply(self, reply: str) -> bool:
        """Is this her previous answer over again?

        Compared on the opening only, normalised, because the tail is where a
        truncated reply differs while the substance is identical. Short answers
        are exempt: "Fine." and "No." are allowed to recur, and a prefix test
        on three words would call every one of them a repeat.
        """
        if not reply or not self._chat_history:
            return False
        previous = next(
            (t.get("content") or "" for t in reversed(self._chat_history)
             if t.get("role") == "assistant"),
            "",
        )
        now, before = _flatten(reply), _flatten(previous)
        if len(now) < CHAT_REPEAT_MIN or len(before) < CHAT_REPEAT_MIN:
            return False
        return now[:CHAT_REPEAT_CHARS] == before[:CHAT_REPEAT_CHARS]

    async def chat_only(self, text: str) -> str:
        """Answer conversationally, with NO tools attached.

        "tell me a poem" was reaching the tool-calling path and coming back as
        a Spotify search for a track named "The Road Not Taken". It was a
        request for her to write one. A request to say or write something is
        not a media request, and handing the model a tool schema for it only
        offers ways to get it wrong.

        With nothing attached the persona can also be stated plainly, which it
        deliberately is not in SYSTEM_PROMPT: there is no tool call to replace
        here, so no fabrication to trade against.
        """
        import httpx

        persona = flavor.persona_text()
        system = (
            "You are Athena, talking with people in a Discord channel. You also "
            "run the room's media, but this message is conversation — nobody is "
            "asking you to play anything.\n\n"
            + (f"Who you are: {persona}\n\n" if persona else "")
            + "Answer it properly and in your own voice. If they asked you to "
            "write something, write it yourself rather than quoting someone "
            "else's. Keep it short — a few lines. No emoji.\n\n"
            # "write it yourself rather than quoting someone else's" was not
            # enough: asked for a joke, qwen3:8b reached for the same bartender
            # exchange 3 times in 8. Naming it explicitly takes that to 0 in 8.
            # A softer version that also said "or make one up" was WORSE than
            # no instruction at all — 5 in 10 — because the permission to
            # invent led straight back to the joke it already knew.
            "Never recite a joke you have heard before — no bartenders, "
            "nothing that walks into a bar, nothing from a joke book. If "
            "someone asks you for a joke, the funny thing is your reaction to "
            "being asked.\n\n"
            "Never end with an offer of further help. No \"you're welcome\", no "
            "\"I'm here to help\", no \"anything else?\". She is not a customer "
            "service desk. Stop when the answer stops.\n\n"
            "When they ask a question, the answer comes FIRST and the remarks "
            "come after. If they told you something earlier in this "
            "conversation, use it — never guess at a thing you were already "
            "told. Asked what their favourite band is when they just named it, "
            "the reply opens with the band."
        )
        async def ask(client, nudge: str = "") -> str:
            response = await client.post(
                    f"{config.OLLAMA_HOST}/api/chat",
                    json={
                        "model": config.OLLAMA_MODEL,
                        "stream": False,
                        **({} if config.OLLAMA_THINK is None
                           else {"think": config.OLLAMA_THINK}),
                        "keep_alive": config.OLLAMA_KEEP_ALIVE,
                        "options": {
                            "temperature": 0.9,
                            # A spoken reply, not a written one. 260 tokens is
                            # about thirty seconds of speech — long enough that
                            # one wandering answer holds the room hostage, and
                            # a model at temperature 0.9 does wander. Observed:
                            # "say hi to Ray" came back as 32.4 seconds about
                            # the third row and third column of a grid. Capping
                            # it does not stop the wandering; it bounds it.
                            "num_predict": config.CHAT_MAX_TOKENS,
                            # Stop before it writes the user's next line for
                            # them. Deliberately NOT "Athena:" — the model
                            # sometimes opens with its own label, and stopping
                            # on that would return an empty reply instead of a
                            # cleaned one. flavor.strip_transcript handles the
                            # label; this handles the runaway turn.
                            "stop": ["\nUser:", "\nHuman:", "\nuser:"],
                            **({} if config.OLLAMA_NUM_GPU is None
                               else {"num_gpu": config.OLLAMA_NUM_GPU}),
                        },
                        "messages": (
                            [{"role": "system", "content": system}]
                            + self._chat_history
                            + [{"role": "user", "content": text}]
                            # As a user turn, not appended to the system
                            # prompt. Measured against a reply that was
                            # genuinely stuck: in the system prompt it changed
                            # nothing at all, 4 times out of 4, because the
                            # copied answer sits far later in the context and
                            # wins. The same words as the last thing she reads
                            # break it 4 times out of 4.
                            + ([{"role": "user", "content": nudge}] if nudge else [])
                        ),
                    },
                )
            response.raise_for_status()
            out = ((response.json().get("message") or {}).get("content") or "").strip()
            return flavor.strip_transcript(out)

        try:
            async with httpx.AsyncClient(timeout=config.OLLAMA_TIMEOUT) as client:
                reply = await ask(client)
                # She reads her own last answer out of the history and says it
                # again, word for word — three identical replies to "tell me a
                # joke", and then the same sentence pattern applied to an
                # unrelated question about someone else. Sampling penalties
                # were no use: repeat_last_n gave 5/5 distinct one run and 3/5
                # the next, which is not something to tune against. Asking once
                # more, having told her, is exact.
                if self._repeats_last_reply(reply):
                    log.info("chat: repeated the previous reply, asking again")
                    second = await ask(client, REPEAT_NUDGE)
                    if second and not self._repeats_last_reply(second):
                        reply = second
        except Exception:
            log.exception("Chat reply failed")
            return "Not now."

        # Remembered only on success, and only here — a failed turn should not
        # leave a dangling user message that every later reply has to explain.
        # Strip the customer-service sign-offs the model tacks on. Observed
        # live: "The Bears. They're the only team that hasn't lost to a team
        # that isn't the Chiefs.  \nYou're welcome. I'm here to help." The
        # prompt asks it not to (below); this is what happens when it does
        # anyway, which is often — assistant training runs deeper than a
        # persona line.
        # Before the sign-off strip: a reply that has looped ran out of tokens
        # mid-phrase, so there is no pleasantry left at the end to find, and
        # everything after the first pass is the model going round again.
        reply = flavor.strip_repetition(reply)
        reply = flavor.strip_sign_offs(reply)
        if reply:
            self._chat_history.append({"role": "user", "content": text})
            self._chat_history.append({"role": "assistant", "content": reply})
            del self._chat_history[:-CHAT_HISTORY_TURNS * 2]
            self._last_chat_at = time.monotonic()
        return reply or "Nothing comes to mind."


class MusicChoice:
    """Several things on Spotify share this name. The front end turns this
    into a picker; nothing is played and no video is parked until they pick."""

    def __init__(self, candidates: list[dict], query: str, action: str = "play"):
        self.candidates = candidates
        self.query = query
        self.action = action  # 'play' or 'queue'

    def text(self) -> str:
        verb = "queue" if self.action == "queue" else "play"
        return f"\u201c{self.query}\u201d could be a few things — which one should I {verb}?"

    def __str__(self) -> str:
        """Readable fallback if something sends this as plain text."""
        kinds = {"track": "Song", "album": "Album", "artist": "Artist", "playlist": "Playlist"}
        lines = "\n".join(
            f"{i + 1}. {c['label']} \u00b7 {kinds.get(c['kind'], c['kind'])}"
            for i, c in enumerate(self.candidates)
        )
        return f"{self.text()}\n{lines}"


class Controls:
    """Adapter between tool calls and the player. Returns plain data.

    Also owns the handoff between video and music. Only one of them should be
    making noise at a time, since both go out over the same screen share.
    """

    def __init__(self, player, lib, spotify=None):
        self.player = player
        self.lib = lib
        self.spotify = spotify
        self.active = "plex"  # or "spotify" or "browser"
        # The age-gate browser fallback (see browser.py). YouTube's own
        # play/pause hotkey is a toggle, not two separate keys, so knowing
        # which state we last put it in is the only way "pause" and "resume"
        # go the right direction instead of a coin flip.
        self._browser_hwnd = None
        self._browser_playing = False
        # A picker raised during a tool-calling turn. The model is told about
        # the candidates so it has context, but the *user* gets the dropdown —
        # asked to relay a list, a small model just says "Which one?" and
        # leaves an unanswerable question on screen.
        self.pending_choice = None
        self.karaoke = Karaoke(spotify, player) if spotify else None
        if self.karaoke and config.KARAOKE_DEFAULT:
            self.karaoke.enabled = True

    def state(self) -> dict:
        status = self.player.status()
        status["active_source"] = self.active
        status["music_available"] = bool(self.spotify and self.spotify.sp)
        status["video_parked"] = self.player.has_parked
        status["parked_video"] = self.player.parked_description
        # The mirror of parked_video. Without it, "go back to the music" gave
        # the model nothing to act on and it paused the video and stopped.
        # Read from local flags only — state() is built on every request and
        # must not make a network call.
        if self.spotify and self.spotify.enabled:
            status["music_playing"] = bool(self.spotify.playing)
            status["paused_music"] = (
                self.spotify.last_label
                if self.spotify.last_label and not self.spotify.playing
                else None
            )
        return status

    def _music_unavailable(self) -> str | None:
        """Why music can't be used right now, or None if it can.

        Connecting happens in the background at startup, so "configured but
        not connected yet" is a real state and shouldn't be reported as
        "Spotify isn't set up" — that sends people off editing .env for no
        reason.
        """
        if self.spotify is None or not self.spotify.enabled:
            return "Spotify isn't set up. Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
        if self.spotify.sp is None:
            return "Spotify is still connecting — try again in a few seconds."
        return None

    # ---- source handoff ----

    async def to_video(self) -> None:
        """About to play something in mpv — silence the music first."""
        if self.active == "spotify" and self.spotify:
            await asyncio.to_thread(self.spotify.pause)
        if self.karaoke and self.karaoke.enabled:
            await self.karaoke.stop()
            self.karaoke.enabled = True  # remember the preference, just not running
        self.active = "plex"
        # Through the player, not wm: mpv registers no windows with the macOS
        # Accessibility API, so find_window(title=...) never matched and this
        # step silently did nothing — the film played behind Spotify with the
        # music still going. show_window also re-asserts fullscreen, which
        # restoring a window does not do on its own.
        await self.player.show_window()

    # ---- window juggling ----

    def _show_video_window(self) -> None:
        """Raise mpv. Kept for callers outside the async switch path.

        On macOS the title lookup cannot work — mpv exposes no AX windows —
        so it goes to the process instead. Player.show_window is the fuller
        version and is what to_video uses.
        """
        hwnd = wm.find_window(title=config.MPV_WINDOW_TITLE)
        if hwnd:
            wm.restore(hwnd)
            wm.bring_to_front(hwnd)
            return
        wm.focus_process("mpv")

    def _show_music_window(self) -> str:
        """Put Spotify on screen so viewers see the queue and its lyrics panel.

        Karaoke mode is the exception: it draws lyrics into mpv, so mpv has to
        stay visible for that to be worth anything.
        """
        # Karaoke keeps mpv up rather than Spotify, and its window handling is
        # done by to_music through the player's IPC — mpv exposes no windows to
        # the macOS Accessibility API, so the wm lookup that used to be here
        # returned None every time and answered "Couldn't find the mpv window —
        # is it running?" while mpv was plainly on screen playing the idle
        # image. Observed live with the lyrics themselves loading fine.
        if self.karaoke and self.karaoke.enabled:
            return "Karaoke mode — lyrics on the mpv window."

        # mpv is minimised by the caller (to_music) through the player's IPC,
        # because wm cannot see its window on macOS. Nothing to do here.
        spotify_hwnd = wm.find_window(process=config.SPOTIFY_PROCESS)
        if not spotify_hwnd:
            return "Couldn't find the Spotify window — is the app running on that machine?"
        if config.MUSIC_MAXIMIZE_SPOTIFY:
            wm.maximize(spotify_hwnd)
        if config.MUSIC_FOCUS_SPOTIFY:
            # Best effort. Keyboard focus is usually refused to a background
            # process, so its return value is not the measure of success.
            wm.bring_to_front(spotify_hwnd)
        # What actually matters is whether viewers can see it. Maximising does
        # that even when focus is denied, so only complain if it really isn't
        # on screen.
        if not wm.is_showing(spotify_hwnd):
            return "Spotify wouldn't come to the front — say *show spotify* to retry."
        return ""

    async def to_music(self) -> str | None:
        """About to play music — park the video so the screen isn't a frozen frame."""
        parked = await self.player.park()
        self.active = "spotify"
        # Out of the way through the player's IPC, for the same reason to_video
        # raises it that way: wm cannot see mpv's window on macOS. Karaoke is
        # the exception — it draws lyrics into mpv, so mpv must stay up.
        karaoke_failed = False
        if self.karaoke and self.karaoke.enabled:
            # Karaoke draws lyrics into mpv, so mpv is the thing to look at.
            karaoke_failed = not await self.player.show_window()
        elif config.MUSIC_MINIMIZE_MPV:
            await self.player.hide_window()
        note = await asyncio.to_thread(self._show_music_window)
        if karaoke_failed:
            note = "mpv wouldn't come to the front — say *show video* to retry."
        if note:
            log.info("Window handling: %s", note)
        if self.karaoke and self.karaoke.enabled:
            await self.karaoke.start()
        return parked

    async def start_music(self, query: str):
        unavailable = self._music_unavailable()
        if unavailable:
            return unavailable

        if not query:  # bare "music" resumes whatever was paused
            parked = await self.to_music()
            result = await asyncio.to_thread(self.spotify.resume)
            return self._with_parked_note(result, parked)

        # A genre is a kind of music, not a name \u2014 searching tracks for it
        # returns titles containing the word. Straight to a playlist instead,
        # and no picker: being asked which EDM you meant defeats the point.
        genre = _GENRE_ONLY.match(query)
        if genre:
            option, note = await asyncio.to_thread(
                self.spotify.genre_playlist, genre.group("genre")
            )
            if note:
                return note
            if option:
                log.info("Genre request %r -> playlist %r", query, option["label"])
                return await self.play_music_option(option)
            # Nothing found: fall through and search normally rather than
            # refusing, since the word may genuinely be a title.

        options, note = await asyncio.to_thread(self.spotify.search_options, query)
        if note:
            return note
        if not options:
            return f"Nothing on Spotify matched \u201c{query}\u201d."
        if len(options) > 1:
            # Don't touch the video until they've actually chosen something.
            return MusicChoice(options, query)
        return await self.play_music_option(options[0])

    async def set_karaoke(self, on: bool) -> str:
        if not self.karaoke:
            return "Spotify isn't set up, so there's nothing to show lyrics for."
        if on:
            result = await self.karaoke.start()
            if self.active == "spotify":
                # Karaoke means mpv, not Spotify, so this raises mpv rather
                # than asking _show_music_window to. show_window also
                # re-asserts mpv's own fullscreen flag, which is separate
                # state from the window and does not come back with it — a
                # stream started after a karaoke detour used to arrive
                # windowed without that.
                #
                # It reports whether mpv is actually up rather than whether
                # the calls were issued, which is the whole point: the reply
                # once said "lyrics on the mpv window" with mpv minimised.
                if not await self.player.show_window():
                    result += ("\nmpv wouldn't come to the front — "
                               "say *show video* to retry.")
            return result
        result = await self.karaoke.stop()
        if self.active == "spotify":
            await asyncio.to_thread(self._show_music_window)
        return result

    async def start_radio(self, seed: str) -> str:
        unavailable = self._music_unavailable()
        if unavailable:
            return unavailable
        parked = await self.to_music()
        result = await asyncio.to_thread(self.spotify.radio, seed)
        return self._with_parked_note(result, parked)

    async def play_music_option(self, option: dict, action: str = "play") -> str:
        # Logged because a pick that plays the WRONG thing is otherwise
        # undiagnosable: the offered list appears in the log, the chosen one
        # did not, so "I picked Pyramid Song and got something else" left no
        # record of which uri was actually sent.
        log.info("picked (%s): %r -> %s", action, option.get("label"), option.get("uri"))
        if action == "queue":
            # Queueing shouldn't hijack the screen or park a video.
            return await asyncio.to_thread(
                self.spotify.queue_uri, option["uri"], option["label"], option["kind"]
            )
        parked = await self.to_music()
        result = await asyncio.to_thread(
            self.spotify.play_uri, option["uri"], option["label"], option["kind"]
        )
        return self._with_parked_note(result, parked)

    async def play_music_link(self, uri: str, mode: str = "play") -> str:
        """A pasted Spotify link — same as play_music_option, but there's
        no prior search step to have already picked a label from."""
        unavailable = self._music_unavailable()
        if unavailable:
            return unavailable
        log.info("Spotify link (%s) -> %s", mode, uri)
        if mode == "queue":
            return await asyncio.to_thread(self.spotify.queue_link, uri)
        parked = await self.to_music()
        result = await asyncio.to_thread(self.spotify.play_link, uri)
        return self._with_parked_note(result, parked)

    async def queue_music(self, query: str):
        unavailable = self._music_unavailable()
        if unavailable:
            return unavailable
        if not query:
            return await asyncio.to_thread(self.spotify.queue_list)

        # Queueing a genre has to use TRACKS, not a playlist: expanding one
        # needs /playlists/{id}/items, which answers 403 for this app even for
        # playlists the account owns. Trying it produced "Nothing to queue
        # from EDM GYM PLAYLIST" after a traceback.
        genre = _GENRE_ONLY.match(query)
        if genre:
            tracks = await asyncio.to_thread(
                self.spotify.genre_tracks, genre.group("genre"), GENRE_QUEUE_TRACKS
            )
            if tracks:
                log.info("Genre queue %r -> %d tracks", query, len(tracks))
                for track in tracks:
                    await asyncio.to_thread(
                        self.spotify.queue_uri, track["uri"], track["label"], "track"
                    )
                first = tracks[0]["label"]
                return (f"Queued {len(tracks)} {genre.group('genre')} tracks, "
                        f"starting with **{first}**.")

        options, note = await asyncio.to_thread(self.spotify.search_options, query)
        if note:
            return note
        if not options:
            return f"Nothing on Spotify matched \u201c{query}\u201d."
        if len(options) > 1:
            return MusicChoice(options, query, action="queue")
        return await self.play_music_option(options[0], action="queue")

    def _with_parked_note(self, result: str, parked) -> str:
        if parked:
            return f"{result}\nPaused {parked} — say *resume video* to come back."
        return result

    async def stop_music(self) -> str:
        """Stop the music but stay on the music source, so a plain 'resume'
        still means music. Starting a video switches back explicitly."""
        unavailable = self._music_unavailable()
        if unavailable:
            return unavailable
        result = await asyncio.to_thread(self.spotify.pause)
        if self.player.has_parked:
            result += "\nSay *resume video* to pick the video back up."
        return result

    async def resume_music(self) -> str:
        unavailable = self._music_unavailable()
        if unavailable:
            return unavailable
        parked = await self.to_music()
        result = await asyncio.to_thread(self.spotify.resume)
        return self._with_parked_note(result, parked)

    # ---- tier 1 ----

    async def fast(self, action: str, **kwargs) -> str:
        if action == "music":
            claimed = await self._library_claims(kwargs.get("text"))
            if claimed is not None:
                return claimed
            return await self.start_music(kwargs.get("query", ""))
        if action == "show_spotify":
            note = await asyncio.to_thread(self._show_music_window)
            return note or "Spotify is on screen."
        if action == "show_video":
            await asyncio.to_thread(self._show_video_window)
            return "Video window is on screen."
        if action == "karaoke":
            return await self.set_karaoke(kwargs["on"])
        if action == "lyrics_status":
            return self.karaoke.status() if self.karaoke else "Spotify isn't set up."
        if action == "shut_up":
            # Never flavoured and never spoken: answering "stop talking" out
            # loud, at length, is the joke nobody wants twice.
            return speech.silence()
        if action == "youtube":
            return await self.play_youtube(kwargs["query"])
        if action == "stream":
            claimed = await self._library_claims(kwargs.get("text"))
            if claimed is not None:
                return claimed
            return await self.play_stream(kwargs["query"], kwargs["source"])
        if action == "stream_status":
            return await self.stream_status(kwargs["query"], kwargs.get("source"))
        if action == "radio":
            claimed = await self._library_claims(kwargs.get("text"))
            if claimed is not None:
                return claimed
            return await self.start_radio(kwargs["query"])
        if action == "music_stop":
            return await self.stop_music()
        if action == "unpark":
            await self.to_video()
            if self.player.has_parked:
                return await self.player.unpark()
            # Nothing was set aside, so "resume the video" just means unpause
            # what's already loaded — not an error.
            return await self.player.resume()
        if action == "track_nav":
            unavailable = self._music_unavailable()
            if unavailable:
                return unavailable
            fn = self.spotify.previous_track if kwargs["back"] else self.spotify.next_track
            return await asyncio.to_thread(fn)
        if action == "volume":
            unavailable = self._music_unavailable()
            if unavailable:
                return unavailable
            return await asyncio.to_thread(self.spotify.set_volume, kwargs["pct"])
        if action == "pause":
            if self.active == "spotify" and self.spotify:
                return await asyncio.to_thread(self.spotify.pause)
            if self.active == "browser":
                return await self._browser_toggle(want_playing=False)
            return await self.player.pause()
        if action == "resume":
            if self.active == "spotify" and self.spotify:
                return await asyncio.to_thread(self.spotify.resume)
            if self.active == "browser":
                return await self._browser_toggle(want_playing=True)
            # Nothing loaded in mpv but music is configured? They mean music.
            if self.player.current is None and self.spotify and self.spotify.sp:
                return await self.resume_music()
            return await self.player.resume()
        if action == "music_queue":
            return await self.queue_music(kwargs.get("query", ""))
        if action == "music_resume":
            return await self.resume_music()
        if action == "music_pause":
            unavailable = self._music_unavailable()
            if unavailable:
                return unavailable
            return await asyncio.to_thread(self.spotify.pause)
        if action == "stop":
            if self.active == "spotify" and self.spotify:
                return await self.stop_music()
            return await self.player.stop()
        if action == "skip":
            if self.active == "spotify" and self.spotify:
                return await asyncio.to_thread(self.spotify.next_track)
            return await self.player.skip()
        if action == "seek":
            return await self.player.seek(kwargs["delta"])
        if action == "speed":
            return await self.player.set_speed(kwargs["rate"])
        if action == "status":
            if self.active == "spotify" and self.spotify:
                return await asyncio.to_thread(self.spotify.now_playing)
            return _format_status(self.state())
        if action == "spotify_status":
            unavailable = self._music_unavailable()
            if unavailable:
                return unavailable
            return await asyncio.to_thread(self.spotify.now_playing)
        if action == "spotify_clear_queue":
            unavailable = self._music_unavailable()
            if unavailable:
                return unavailable
            return await asyncio.to_thread(self.spotify.clear_queue)
        if action == "spotify_link":
            return await self.play_music_link(kwargs["uri"], kwargs.get("mode", "play"))
        if action == "queue_list":
            return _format_queue(await self.player.queue_titles())
        if action == "subs_off":
            return await self.player.set_subtitle_language("off")
        if action == "subs_list":
            return tk.describe_tracks(await self.player.track_list(), "sub")
        if action == "help":
            return help_text(kwargs.get("topic", ""))
        if action == "browse":
            rows = await asyncio.to_thread(
                self.lib.browse, kind=kwargs.get("kind"), sort=kwargs.get("sort", "newest_release"), limit=1
            )
            if not rows:
                return f"No {kwargs.get('kind', 'titles')} found."
            r = rows[0]
            return f"{r.label()} — {r.library}"
        if action == "search_debug":
            return await self.search_everywhere(kwargs["query"])
        if action == "libraries":
            names = self.lib.libraries()
            return "**Searchable libraries:**\n" + "\n".join(f"- {n}" for n in names)
        if action == "tracks_list":
            rows = await self.player.track_list()
            return (
                tk.describe_tracks(rows, "audio")
                + "\n\n"
                + tk.describe_tracks(rows, "sub")
            )
        if action == "audio_lang":
            return await self.player.set_audio_language(kwargs["lang"])
        if action == "subs_lang":
            return await self.player.set_subtitle_language(kwargs["lang"])
        if action == "mode_sub":
            await self.player.set_audio_language("jpn")
            return await self.player.set_subtitle_language("eng")
        if action == "mode_dub":
            await self.player.set_audio_language("eng")
            return await self.player.set_subtitle_language("off")
        if action in ("play", "queue"):
            query = kwargs["query"]
            # Resolve *before* touching the source. Switching first meant a
            # typo'd title stopped the music and left the screen on the idle
            # image with an error and no way back.
            item, options = await asyncio.to_thread(self.lib.resolve_query, query)
            if options:
                return Choice(options, action, query)
            if item is None:
                return f"Couldn't find anything matching “{query}”."
            return await self._start_or_queue(item, action)
        return "Unknown action."

    # Leading verbs that aren't part of any title, stripped before matching.
    _VERB_PREFIX = re.compile(r"^(?:play|put\s+on|start|watch|throw\s+on)\s+", re.I)

    async def try_direct_play(self, text: str):
        """Handle "play <title>" without a model when the library is certain.

        The fast path deliberately left plain titles to the LLM, on the theory
        that it matches fuzzy names better. In practice the library resolves an
        exact or starts-with hit perfectly, and routing it through a model only
        adds ways to be wrong: asked for "Nacho Libre" after twenty minutes of
        music requests, a 7B decided it was a song, then reported the film
        missing from a library that contains it.

        Returns the result, a Choice, or None to mean "not confident, let the
        model try". Anything vague — "something with dragons" — scores low and
        falls through untouched.
        """
        m = _PLAY_OR_QUEUE.match(text.strip())
        if not m:
            return None
        query = m.group("query").strip()
        action = "queue" if m.group("verb").lower() in ("queue", "add", "q") else "play"

        # 0.95 is scored_search's exact / starts-with band. Substring matches
        # (0.85) are deliberately excluded — those are the ones where judgement
        # actually helps.
        scored = await asyncio.to_thread(self.lib.scored_search, query, None, 3)
        if scored and scored[0][0] >= 0.95:
            item, options = await asyncio.to_thread(self.lib.resolve_query, query)
            if options:
                return Choice(options, action, query)
            if item is not None:
                log.info("Resolved %r directly (score %.2f) — no model needed",
                         query, scored[0][0])
                return await self._start_or_queue(item, action)

        # "<something> by <someone>" that the video library doesn't have is a
        # song request. Sending it to the model instead is what produced
        # '.Charting "Friday" by Vanessa Black... queued.' three times over,
        # with nothing behind it — Spotify can answer this deterministically.
        # The same reasoning covers a request that names music outright —
        # "queue up some EDM music" reached the model and came back as
        # search_library, which the video library can never satisfy.
        if not self._music_unavailable():
            song = _SONG_BY_ARTIST.match(query)
            genre = _MUSIC_SHAPED.search(query)
            if song or genre:
                # A doubled verb ("play play friday by vanessa black") leaves the
                # second one inside the query. Strip it for Spotify only — the
                # library search above needs it intact, or the film "Play Misty
                # for Me" stops resolving.
                spotify_query = self._VERB_PREFIX.sub("", query).strip() or query
                log.info("%r missed the library and %s — trying Spotify", query,
                         "names an artist" if song else "asks for music")
                if action == "queue":
                    return await self.queue_music(spotify_query)
                return await self.start_music(spotify_query)

        return None

    async def _library_claims(self, text: str | None) -> str | None:
        """Play a library title when a music-shaped phrase is really its name.

        "pirate radio" and "music box" are films that the radio/music patterns
        would otherwise send to Spotify, with no way for anyone to ask for the
        film instead. Only a near-exact hit wins (>=0.9 is scored_search's
        exact / starts-with / substring band), so "system of a down radio" and
        "music radiohead" still go to Spotify untouched.

        Returns the play result if the library won, else None.
        """
        if not text:
            return None
        cleaned = self._VERB_PREFIX.sub("", text.strip())
        if not cleaned:
            return None
        scored = await asyncio.to_thread(self.lib.scored_search, cleaned, None, 1)
        if not scored or scored[0][0] < 0.9:
            return None
        entry = scored[0][1]
        log.info("Reading %r as the library title %r, not a music request", text, entry.title)
        return await self.play_media_option(entry.rating_key)

    KIND_NOUN = {"track": "Song", "album": "Album",
                 "artist": "Artist", "playlist": "Playlist"}

    @staticmethod
    def _relevant(query: str, label: str) -> bool:
        """Does this result share a real word with the request?

        Spotify's search always returns *something* — "zzzznothingzzz" comes
        back with five unrelated songs — so a search command needs its own
        floor, or it reports confident nonsense. One shared word is enough,
        which keeps near-misses like "streeghtlight manifesto" alive.
        """
        wanted = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3}
        if not wanted:
            return True
        return bool(wanted & set(re.findall(r"[a-z0-9]+", label.lower())))

    async def search_everywhere(self, query: str) -> str:
        """Show what exists, without playing anything.

        Searches both collections by default. A leading "song"/"spotify"/
        "movie" etc. narrows it — "search" used to mean the Plex library only,
        which made "search song ... " report nothing found for a track that
        was on Spotify all along.
        """
        query = query.strip()
        if not query:
            return "Search for what?"

        music_only = bool(_SEARCH_MUSIC_ONLY.match(query))
        video_only = bool(_SEARCH_VIDEO_ONLY.match(query))
        # Drop a leading "spotify"/"music"/"movie" — it's scope, not a title.
        # Kind words like "song" stay: Spotify's own prefix handling uses them.
        bare = re.sub(r"^(?:spotify|music|movies?|films?|shows?|series|tv|plex)\s+",
                      "", query, flags=re.I).strip() or query

        sections = []

        if not music_only:
            rows = await asyncio.to_thread(self.lib.search, bare, None, 8)
            if rows:
                sections.append(
                    "**In your library:**\n"
                    + "\n".join(f"· {r.long_label()}" for r in rows)
                )

        if not video_only and self.spotify and self.spotify.enabled:
            # `bare`, not `query`: searching Spotify for the literal words
            # "spotify toxicity" returns playlists about Spotify.
            found, note = await asyncio.to_thread(self.spotify.search_list, bare, 8)
            found = [f for f in found if self._relevant(bare, f["label"])][:6]
            if found:
                sections.append(
                    "**On Spotify:**\n"
                    + "\n".join(
                        f"· {f['label']} — {self.KIND_NOUN.get(f['kind'], f['kind'])}"
                        for f in found
                    )
                )
            elif note and not sections:
                return note

        if not sections:
            where = ("Spotify" if music_only
                     else "your library" if video_only
                     else "your library or Spotify")
            return f"Nothing matching “{query}” in {where}."
        return "\n\n".join(sections) + "\n_Say `play …` or `queue …` to start one._"

    async def play_youtube(self, query: str) -> str:
        """Resolve a link or search phrase and play it in mpv.

        Deliberately deterministic: a URL or an explicit "youtube ..." needs no
        model, and the title comes back from yt-dlp rather than being guessed.
        """
        if not config.YTDL_PATH:
            return (
                "YouTube needs yt-dlp installed and reachable. "
                "Install it, or set YTDL_PATH in .env."
            )
        try:
            item = await asyncio.to_thread(youtube.resolve, query)
        except youtube.AgeRestricted as exc:
            if config.YOUTUBE_BROWSER_FALLBACK:
                return await self._play_in_browser(exc.url)
            return (
                f"That one's age-restricted and won't play here: {exc.url}"
            )
        if item is None:
            return (
                f"Couldn't find anything on YouTube for “{query[:80]}” — "
                f"it may be private, removed or age-restricted."
            )
        await self.to_video()
        return await self.player.play(item)

    def _open_browser_window(self, url: str) -> tuple[str, int | None]:
        """mpv only gets minimised once Firefox is actually spawning — a
        failed launch used to minimise mpv anyway and leave the screen blank
        with no video and no error visible on it, only in chat."""
        note, hwnd = browser.open_video(url)
        if hwnd:
            mpv_hwnd = wm.find_window(title=config.MPV_WINDOW_TITLE)
            if mpv_hwnd:
                wm.minimize(mpv_hwnd)
        return note, hwnd

    async def _play_in_browser(self, url: str) -> str:
        """Age-gated link, yt-dlp route closed — hand it to a real browser.

        Play/pause reach it (see _browser_toggle); seek and stop don't —
        there's no scripted control of the page, just window focus and
        keypresses, and neither of those maps to a seek.
        """
        if self.karaoke and self.karaoke.enabled:
            await self.karaoke.stop()
            self.karaoke.enabled = True
        self.active = "browser"
        note, hwnd = await asyncio.to_thread(self._open_browser_window, url)
        self._browser_hwnd = hwnd
        self._browser_playing = hwnd is not None
        if note:
            return note
        return (
            "That one's age-restricted, so I opened it in a browser instead "
            "of pulling the stream directly. Say pause or resume and it'll "
            "reach it — I can't seek or stop it, though. Alt+F4 closes it "
            "when you're done."
        )

    async def _browser_toggle(self, want_playing: bool) -> str:
        """A blind keypress toggle, aimed with tracked state rather than a
        real read of the player. Goes stale the moment someone clicks or
        taps space inside the window by hand — there's no way to read the
        actual state back without scripting the page, which is the whole
        thing the simple version chose not to do."""
        if not self._browser_hwnd or not await asyncio.to_thread(
            wm.is_showing, self._browser_hwnd
        ):
            self._browser_hwnd = None
            return "Nothing's open in the browser."
        if self._browser_playing == want_playing:
            return "Already playing." if want_playing else "Already paused."
        await asyncio.to_thread(wm.bring_to_front, self._browser_hwnd)
        await asyncio.to_thread(wm.send_key, wm.VK_SPACE)
        self._browser_playing = want_playing
        return "Resumed." if want_playing else "Paused."

    async def play_stream(self, query: str, source: str) -> str:
        """Play a live Twitch or Kick channel.

        Live only, deliberately. Someone being offline is the most common
        outcome and reads as an answer, not a failure — falling back to their
        latest VOD would silently play something nobody asked for.
        """
        if not config.YTDL_PATH:
            return (
                "Live streams need yt-dlp installed and reachable. "
                "Install it, or set YTDL_PATH in .env."
            )
        item, reason = await asyncio.to_thread(streams.resolve, query, source)
        if item is None:
            name = query if not streams.is_url(query) else query.rstrip("/").rsplit("/", 1)[-1]
            label = source.title()
            if reason == "offline":
                return f"**{name}** isn't live on {label} right now."
            if reason == "unavailable":
                return "yt-dlp isn't installed, so live streams can't be resolved."
            if reason == "blocked":
                # Not the user's problem and not a missing channel — saying
                # "couldn't find it" would send them looking for a typo that
                # isn't there.
                return (
                    f"{label} refused the lookup — it's blocking automated "
                    f"requests, not a problem with **{name}**. Twitch still works."
                )
            return f"Couldn't find **{name}** on {label}."
        await self.to_video()
        return await self.player.play(item)

    async def stream_status(self, query: str, source: str | None = None) -> str:
        """Answer "is X live?" from the API rather than letting a model guess.

        Asked whether someone was streaming, the model invented a reply about
        whether they broadcast anything "with an edge". streams.resolve already
        knows the real answer, so it is asked instead.

        Checks both platforms when neither is named, because people ask about a
        streamer rather than about a site.
        """
        if not config.YTDL_PATH:
            return "Live streams need yt-dlp installed, so I can't check."

        sources = [source] if source in streams.SOURCES else list(streams.SOURCES)
        blocked = []
        for candidate in sources:
            item, reason = await asyncio.to_thread(streams.resolve, query, candidate)
            label = candidate.title()
            if item is not None:
                return f"**{query}** is live on {label}: {describe_stream(item)}"
            if reason == "blocked":
                blocked.append(label)

        if blocked and len(blocked) == len(sources):
            return (f"{', '.join(blocked)} refused the lookup, so I can't tell "
                    f"whether **{query}** is live.")
        where = " or ".join(s.title() for s in sources)
        return f"**{query}** isn't live on {where} right now."

    async def _start_or_queue(self, item, action: str) -> str:
        """Play now, or queue quietly — claiming the screen only if we'll use it."""
        if action == "queue" and not self.player.would_start_now:
            return await self.player.queue_add(item)
        await self.to_video()
        if action == "play":
            return await self.player.play(item)
        return await self.player.queue_add(item)

    async def play_media_option(self, rating_key: int, action: str = "play") -> str:
        """A disambiguation pick, resolved and started the same way a typed
        request would be — including the music handoff."""
        try:
            item = await asyncio.to_thread(self.lib.fetch, int(rating_key))
        except Exception:
            log.exception("Could not fetch rating_key %s", rating_key)
            return "That item isn't in the library any more."
        # Same reasoning as play_music_option: without this, a pick that starts
        # the wrong title leaves nothing in the log to compare against.
        log.info("picked (%s): rating_key %s -> %r", action, rating_key,
                 getattr(item, "title", "?"))
        if getattr(item, "type", None) == "show":
            # NOT "or item" — see library.resolve_query. A series has no media
            # parts, so passing it on just moves the failure to stream_url.
            episode = await asyncio.to_thread(self.lib.up_next, item)
            if episode is None:
                return f"Couldn't find a playable episode of **{describe(item)}**."
            item = episode
        return await self._start_or_queue(item, action)

    # ---- tier 2 ----

    async def dispatch(self, name: str, args: dict):
        try:
            if name == "search_library":
                kind = args.get("kind")
                kind = None if kind in (None, "any") else kind
                results = await asyncio.to_thread(
                    self.lib.search, args.get("query", ""), kind, 12
                )
                return [
                    {
                        "rating_key": r.rating_key,
                        "title": r.label(),
                        "kind": r.kind,
                        "library": r.library,
                    }
                    for r in results
                ] or "no matches"

            if name in ("play_media", "queue_media"):
                # Resolve first — see _start_or_queue. A miss must not have
                # already stopped the music by the time we report it.
                item, options = await self._resolve(args)
                if options:
                    self.pending_choice = Choice(
                        options,
                        "queue" if name == "queue_media" else "play",
                        str(args.get("query") or "that"),
                    )
                    return {
                        "ambiguous": [
                            {
                                "rating_key": o.rating_key,
                                "title": o.long_label(),
                            }
                            for o in options
                        ],
                        "note": "Ask the user which one, then call again with rating_key.",
                    }
                if item is None:
                    return "not found"
                if name == "queue_media" and not self.player.would_start_now:
                    return await self.player.queue_add(item)
                await self.to_video()
                if name == "queue_media":
                    return await self.player.queue_add(item)
                offset = 0.0 if args.get("start_at_beginning") else None
                return await self.player.play(item, offset=offset)

            if name == "music":
                result = await self.start_music(args.get("query", ""))
                if isinstance(result, MusicChoice):
                    self.pending_choice = result
                    return {
                        "ambiguous": [
                            {"uri": c["uri"], "kind": c["kind"], "label": c["label"]}
                            for c in result.candidates
                        ],
                        "note": "Ask which one, then call music_play_uri with the chosen uri.",
                    }
                return result

            if name == "karaoke":
                return await self.set_karaoke(bool(args.get("on", True)))

            if name == "music_radio":
                return await self.start_radio(args.get("seed", ""))

            if name == "music_queue":
                result = await self.queue_music(args.get("query", ""))
                if isinstance(result, MusicChoice):
                    self.pending_choice = result
                    return {
                        "ambiguous": [
                            {"uri": c["uri"], "kind": c["kind"], "label": c["label"]}
                            for c in result.candidates
                        ],
                        "note": "Ask which one, then call music_play_uri with action=queue.",
                    }
                return result

            if name == "music_play_uri":
                unavailable = self._music_unavailable()
                if unavailable:
                    return unavailable
                uri = (args.get("uri") or "").strip()
                # "tell me a poem" produced music_play_uri with a label and no
                # uri at all — the model invented a pick instead of taking one
                # from a picker. The empty string reached Spotify as
                # context_uri="" and came back 400 with a traceback, for what is
                # really a bad argument. Refuse it and say how to get a real one.
                if not _SPOTIFY_URI.match(uri):
                    log.warning("music_play_uri called with an unusable uri %r", uri)
                    return {
                        "error": "no valid spotify uri",
                        "note": "Call music with the query first and use a uri "
                                "from its result. Never invent one.",
                    }
                label, kind = args.get("label", ""), args.get("kind", "")
                if uri and not (label and kind):
                    # The model often sends the uri alone, having dropped the
                    # label it was handed a moment earlier. Recover it rather
                    # than show the user a raw spotify:track:... string.
                    found, derived = await asyncio.to_thread(
                        self.spotify.describe_uri, uri
                    )
                    label, kind = label or found, kind or derived
                return await self.play_music_option(
                    {"uri": uri, "label": label, "kind": kind},
                    action=args.get("action", "play"),
                )

            if name == "music_control":
                if not self.spotify:
                    return "spotify not configured"
                action = args.get("action")
                if action == "stop":
                    return await self.stop_music()
                if action == "volume":
                    return await asyncio.to_thread(
                        self.spotify.set_volume, int(args.get("volume_percent", 50))
                    )
                fn = {
                    "pause": self.spotify.pause,
                    "resume": self.spotify.resume,
                    "next": self.spotify.next_track,
                    "previous": self.spotify.previous_track,
                    "status": self.spotify.now_playing,
                    "clear_queue": self.spotify.clear_queue,
                }.get(action)
                if fn is None:
                    return "unknown action"
                if action == "resume":
                    return await self.resume_music()
                return await asyncio.to_thread(fn)

            if name == "playback_control":
                action = args.get("action")
                if action == "resume_video":
                    await self.to_video()
                    return await self.player.unpark()
                if action == "pause":
                    return await self.fast("pause")
                if action == "resume":
                    return await self.fast("resume")
                if action == "stop":
                    return await self.fast("stop")
                if action == "skip":
                    return await self.fast("skip")
                if action == "restart_current":
                    return await self.player.restart_current()
                if action == "restart_player":
                    ok = await self.player.restart_player()
                    return "player restarted" if ok else "restart failed"
                return "unknown action"

            if name == "seek":
                if args.get("absolute_seconds") is not None:
                    return await self.player.seek_to(float(args["absolute_seconds"]))
                return await self.player.seek(float(args.get("relative_seconds", 0)))

            if name == "set_speed":
                return await self.player.set_speed(float(args.get("rate", 1)))

            if name == "manage_queue":
                action = args.get("action")
                if action == "list":
                    return await self.player.queue_titles() or "queue is empty"
                if action == "clear":
                    return await self.player.queue_clear()
                if action == "remove":
                    return await self.player.queue_remove(int(args.get("index", 0)))
                return "unknown action"

            if name == "set_language":
                remember = args.get("remember", True)
                results = []
                if args.get("audio"):
                    results.append(
                        await self.player.set_audio_language(args["audio"], remember)
                    )
                if args.get("subtitles"):
                    results.append(
                        await self.player.set_subtitle_language(args["subtitles"], remember)
                    )
                return results[-1] if results else "nothing to change"

            if name == "list_tracks":
                rows = await self.player.track_list()
                kind = args.get("kind", "both")
                if kind == "audio":
                    return tk.describe_tracks(rows, "audio")
                if kind == "subtitle":
                    return tk.describe_tracks(rows, "sub")
                return (
                    tk.describe_tracks(rows, "audio")
                    + "\n\n"
                    + tk.describe_tracks(rows, "sub")
                )

            if name == "browse_library":
                rows = await asyncio.to_thread(
                    self.lib.browse,
                    kind=args.get("kind"),
                    sort=args.get("sort", "newest_release"),
                    genre=args.get("genre"),
                    library=args.get("library"),
                    limit=int(args.get("limit", 10)),
                )
                if not rows:
                    return "no matches"
                return [
                    {
                        "title": r.title,
                        "year": r.year,
                        "kind": r.kind,
                        "library": r.library,
                        "genres": list(r.genres),
                        "plays_via_this_bot": r.view_count,
                    }
                    for r in rows
                ]

            if name == "generate_image":
                # Returns a Picture, which bot.py attaches. Never raises: a
                # server that is off or slow must not take the bot with it.
                import imagegen
                return await imagegen.generate(
                    args.get("prompt", ""),
                    negative=args.get("negative", "") or "",
                )

            if name == "get_status":
                return self.player.status()

            return f"unknown tool {name}"
        except Exception as exc:
            log.exception("Tool %s failed", name)
            return f"error: {exc}"

    async def _resolve(self, args: dict):
        if args.get("rating_key"):
            try:
                item = await asyncio.to_thread(self.lib.fetch, int(args["rating_key"]))
            except Exception:
                return None, []
            # A rating_key can name a SERIES, and a series is not playable — it
            # has no media parts, so it reached stream_url and raised
            # AttributeError: 'Show' object has no attribute 'media'.
            # resolve_query and play_media_option both resolve to an episode;
            # this path was the one that did not.
            if getattr(item, "type", None) == "show":
                item = await asyncio.to_thread(self.lib.up_next, item)
            return item, []
        query = args.get("query")
        if not query:
            return None, []
        return await asyncio.to_thread(self.lib.resolve_query, query)


# ----------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------

def _format_status(status: dict) -> str:
    if not status["playing"]:
        return "Nothing playing."
    icon = "Paused" if status["paused"] else "Playing"
    line = f"**{icon}:** {status['title']}  ·  {status['position']} / {status['duration']}"
    if status["queue_length"]:
        line += f"  ·  {status['queue_length']} queued"
    if status.get("video_parked"):
        line += "  ·  video paused for music"
    if status.get("speed", 1.0) != 1.0:
        line += f"  ·  {status['speed']:g}x"
    line += (
        f"\nAudio: {status['audio_language']}  ·  "
        f"Subtitles: {status['subtitle_language']}"
    )
    return line


def _format_queue(titles: list[str]) -> str:
    if not titles:
        return "Queue is empty."
    lines = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    return f"**Up next:**\n{lines}"


def _format_tracks(tracks: list[dict], kind: str) -> str:
    if not tracks:
        return f"No {kind} tracks on this file."
    lines = []
    for t in tracks:
        mark = " ←" if t.get("selected") else ""
        label = t.get("title") or t.get("lang") or "track"
        lines.append(f"`{t.get('id')}` {label} ({t.get('lang', 'und')}){mark}")
    return f"**{kind.capitalize()} tracks:**\n" + "\n".join(lines)


__all__ = ["Brain", "Choice", "Controls", "MusicChoice", "describe", "help_text"]