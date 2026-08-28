"""Who was talking, taken from Discord's own speaking signal.

MEASURED, AND IT DOES NOT WORK. Off by default. Read this before switching
it back on.

The theory below is sound and the connection succeeds: the bot joins, and
Discord sends opcode 5 SPEAKING frames naming user ids in the clear. What it
does NOT do is keep sending them. Measured on a live channel, every frame
arrived in the same second the connection opened -- the current state of all
five people present, every one of them speaking=1 -- and not one frame after
that, through several minutes of people plainly talking.

The reason appears to be that we never receive any audio. DAVE encrypts the
media, so the bot's connection carries no voice packets, and a connection
receiving nothing gives Discord no occasion to report who is transmitting.
The speaking frame accompanies a stream we are not getting.

That produces a failure that looks like success from a distance: five
intervals opened and never closed, matching every utterance that follows, so
the log confidently names someone for lines they did not say. It was briefly
mistaken for working. Attribution against an initial state dump is worse
than no attribution, which is why this is off rather than merely imperfect.

What is left, and where to pick this up: the host account's Discord client IS
receiving that audio and does know who is speaking. It exposes a local RPC
API (127.0.0.1, ports 6463-6472, confirmed listening here) carrying
SPEAKING_START and SPEAKING_STOP events for a voice channel. That is an
official local API for exactly this, and it needs no second account in the
channel. The obstacle to check first is authorisation: RPC scopes are gated
to applications Discord has whitelisted, so the handshake may simply be
refused.

The code below is kept because the interval bookkeeping and the overlap
matching are correct and tested, and would be reused verbatim by an RPC
source -- only the frames feeding _begin/_end would change.


The capture side cannot answer this. Athena listens to a virtual cable
carrying Discord's *mixed* output — one stream with every voice already
summed into it — so there is nothing in the audio to attribute. That was an
acceptable trade for running commands, where the wake word authorises the
request and the speaker's identity is irrelevant. It is not acceptable for
reading the tuning log afterwards, where "who said that" is most of the
value.

The signal used here is deliberately NOT the audio. Discord's voice
websocket carries an opcode 5 SPEAKING frame naming the user id that just
started or stopped transmitting, and that frame is ordinary signalling: DAVE
end-to-end encryption covers the media, not the control channel. So the
question that killed voice receive — can we decrypt what people said — is
not the question being asked here. We only ask who was talking, and Discord
tells us in the clear.

What that costs: the bot has to hold a voice connection to receive those
frames, so it appears in the voice channel as a participant. It joins muted,
transmits nothing, and ignores every audio packet that arrives.

What it cannot do: attribution is by overlapping timestamps, not by
separating the audio. Two people talking at once produce one utterance and
both names. The audio path and the gateway path also have independent
latency, so the match is deliberately generous at the edges — see
config.VOICE_SPEAKER_TOLERANCE_MS.

Import must not raise where discord.py is absent or voice is off; the bot
runs without this.
"""

from __future__ import annotations

import asyncio
import logging
import time

import config

log = logging.getLogger("athena.speakers")

try:  # pragma: no cover - depends on the interpreter, like voice.py
    import discord
    from discord.voice_state import VoiceConnectionState
    DISCORD_AVAILABLE = True
except Exception:  # pragma: no cover
    discord = None  # type: ignore[assignment]
    VoiceConnectionState = None  # type: ignore[assignment]
    DISCORD_AVAILABLE = False


# Discord's voice opcode for "this user started/stopped transmitting".
_OP_SPEAKING = 5

# An interval left open this long is closed at its last known extent. Not
# every client sends the stop frame — some only ever announce the start — and
# without this a single unclosed interval would go on claiming every later
# utterance forever.
_MAX_OPEN_S = 30.0


def overlaps(interval: tuple[float, float], start: float, end: float,
             tolerance: float) -> bool:
    """Did a speaking interval overlap an utterance, allowing for slop?

    Pulled out and kept dependency-free so it can be tested without a
    Discord connection, which is the part of this module that cannot be
    exercised offline.
    """
    began, ended = interval
    return began - tolerance <= end and ended + tolerance >= start


class SpeakerTracker:
    """Holds a voice connection and remembers who was talking, and when."""

    # Enough to cover any utterance still waiting on transcription, without
    # growing without bound in a channel that talks all evening.
    HISTORY_S = 300.0

    def __init__(self, bot):
        self.bot = bot
        self.voice_client = None
        # Closed intervals, oldest first: (user_id, began, ended).
        self._spoken: list[tuple[int, float, float]] = []
        # user_id -> when they started, for intervals still open.
        self._open: dict[int, float] = {}
        self._names: dict[int, str] = {}

    # -- connection ------------------------------------------------------

    async def start(self) -> bool:
        """Join the voice channel to receive speaking frames. False if not."""
        if not config.VOICE_TRACK_SPEAKERS:
            return False
        if not DISCORD_AVAILABLE:
            log.warning("VOICE_TRACK_SPEAKERS is set but discord.py is missing")
            return False
        if not config.VOICE_CHANNEL_ID:
            log.warning("VOICE_TRACK_SPEAKERS is set but VOICE_CHANNEL_ID is not")
            return False

        channel = self.bot.get_channel(config.VOICE_CHANNEL_ID)
        if not isinstance(channel, discord.VoiceChannel):
            log.warning("VOICE_CHANNEL_ID %s is not a voice channel I can see",
                        config.VOICE_CHANNEL_ID)
            return False

        hook = self._on_ws

        class _Tracking(discord.VoiceClient):
            """A voice client that reports every frame to us.

            The hook has to be supplied when the connection state is built —
            it is read once, when the websocket is created — so setting it
            after connect() would only take effect on a later reconnect.
            """

            def create_connection_state(self):
                return VoiceConnectionState(self, hook=hook)

        try:
            # Muted because she has nothing to transmit here: her voice
            # reaches the channel through the cable and the host account's mic, not
            # through this connection. NOT deafened — a deafened client is
            # not a reliable recipient of speaking frames, which are the one
            # thing this connection exists to receive.
            self.voice_client = await channel.connect(
                cls=_Tracking, self_mute=True, self_deaf=False, timeout=30.0
            )
        except Exception:
            log.exception("Could not join %s — the log will not name speakers",
                          getattr(channel, "name", config.VOICE_CHANNEL_ID))
            return False

        log.info("Tracking speakers in #%s", channel.name)
        return True

    async def stop(self) -> None:
        if self.voice_client is not None:
            try:
                await self.voice_client.disconnect(force=True)
            except Exception:
                log.debug("Could not leave the voice channel", exc_info=True)
            self.voice_client = None

    # -- the signal ------------------------------------------------------

    async def _on_ws(self, ws, msg) -> None:
        """Every voice websocket frame. We care about exactly one.

        Must never raise: this runs inside discord.py's receive loop, and an
        exception here would take the voice connection down with it.
        """
        try:
            if not isinstance(msg, dict) or msg.get("op") != _OP_SPEAKING:
                return
            data = msg.get("d") or {}
            user_id = data.get("user_id")
            if user_id is None:
                return
            user_id = int(user_id)
            # The field is a bitfield (microphone / soundshare / priority),
            # not a boolean — any non-zero value means transmitting.
            log.debug("SPEAKING frame: user=%s speaking=%r", user_id,
                      data.get("speaking"))
            if data.get("speaking"):
                self._begin(user_id)
            else:
                self._end(user_id)
        except Exception:
            log.debug("Malformed speaking frame", exc_info=True)

    def _begin(self, user_id: int) -> None:
        self._open.setdefault(user_id, time.time())

    def _end(self, user_id: int) -> None:
        began = self._open.pop(user_id, None)
        if began is None:
            return
        self._spoken.append((user_id, began, time.time()))
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.HISTORY_S
        if self._spoken and self._spoken[0][1] < cutoff:
            self._spoken = [s for s in self._spoken if s[2] >= cutoff]

    def _name(self, user_id: int) -> str:
        """Display name, remembered so a departure doesn't blank the log.

        The voice channel's own member list is tried first, and it is the
        one that actually works: Bot.get_user reads a cache populated from
        messages and the members intent, neither of which we have for
        someone who has only ever spoken — which is precisely the person
        being named here. Anyone sending a speaking frame is by definition
        in this channel, so that list is both the most reliable source and
        the most obviously correct one.
        """
        cached = self._names.get(user_id)
        if cached:
            return cached

        user = None
        channel = getattr(self.voice_client, "channel", None)
        for member in getattr(channel, "members", None) or ():
            if member.id == user_id:
                user = member
                break
        if user is None:
            guild = getattr(channel, "guild", None)
            if guild is not None:
                user = guild.get_member(user_id)
        if user is None:
            user = self.bot.get_user(user_id)

        name = getattr(user, "display_name", None) or getattr(user, "name", None)
        name = name or str(user_id)
        # Only remembered once it resolved to a real name. Caching the id
        # would make a first frame that arrived before the member list was
        # populated permanent for the rest of the session.
        if user is not None:
            self._names[user_id] = name
        return name

    # -- the answer ------------------------------------------------------

    def who_spoke(self, start: float, end: float) -> str | None:
        """Who was transmitting while this utterance was being captured.

        Returns a name, several names for people talking over each other, or
        None when nothing was heard from Discord — which is a real answer,
        not a failure: it is what a sound reaching the cable from somewhere
        other than the voice channel looks like.
        """
        if self.voice_client is None:
            return None

        now = time.time()
        tolerance = config.VOICE_SPEAKER_TOLERANCE_MS / 1000
        candidates: dict[int, float] = {}

        # Intervals still open are counted at their extent so far. Someone
        # who has not stopped talking is exactly who is being transcribed.
        live = [(uid, began, now) for uid, began in self._open.items()
                if now - began < _MAX_OPEN_S]

        for user_id, began, ended in list(self._spoken) + live:
            if not overlaps((began, ended), start, end, tolerance):
                continue
            shared = min(end, ended) - max(start, began)
            candidates[user_id] = max(candidates.get(user_id, 0.0), shared)

        if not candidates:
            return None
        # Most overlap first: with two speakers, the one who talked through
        # most of the utterance is the likelier author of it. Capped at two
        # because the log line has a fixed-width column for this, and a
        # third name only ever arrived as a truncated fragment of one.
        ordered = sorted(candidates, key=lambda u: candidates[u], reverse=True)
        return ", ".join(self._name(u) for u in ordered[:2])


_tracker: SpeakerTracker | None = None


async def start(bot) -> SpeakerTracker | None:
    """Begin tracking, or return None. Never raises into startup."""
    global _tracker
    try:
        tracker = SpeakerTracker(bot)
        if not await tracker.start():
            return None
    except Exception:
        log.exception("Speaker tracking failed to start — carrying on without it")
        return None
    _tracker = tracker
    return tracker


async def stop() -> None:
    global _tracker
    if _tracker is not None:
        await _tracker.stop()
        _tracker = None


def who_spoke(start_ts: float, end_ts: float) -> str | None:
    """Module-level view, so voice.py needs no reference to the tracker."""
    if _tracker is None:
        return None
    return _tracker.who_spoke(start_ts, end_ts)
