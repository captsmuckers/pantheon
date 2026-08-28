"""Spoken commands, captured from Discord's audio rather than its API.

The obvious design — have the bot join the voice channel and receive audio —
is dead. Discord enforced its DAVE end-to-end encryption protocol for all
non-stage voice calls in March 2026, and a client without DAVE is rejected at
handshake with close code 4017. discord.py 2.7.1 plus `davey` fixes the
*connection*, but `discord-ext-voice-recv` has no DAVE frame decryption, so the
bot joins successfully and hears nothing: measured 759 UDP packets in 12
seconds producing zero decoded audio and zero crypto errors.

So we listen locally instead. The Discord client on this machine is signed in
as the account that screenshares the desktop, and it sits in the voice channel,
which means its audio output already contains every other participant's voice.
Discord renders that to a VB-Audio virtual cable; the far end of that cable is
an ordinary Windows *recording* device. We open it like a microphone.

    Discord  --plays-->  CABLE Input          [render endpoint]
                              |
                         (the cable)
                              |
       Athena  <--records--  CABLE Output        [capture endpoint / virtual mic]

Consequences worth knowing:

  * No per-speaker separation. We cannot tell who spoke, and do not need to —
    the wake word is what authorises a command, not the speaker's identity.
  * mpv and Spotify render to a different cable entirely, so media can never
    bleed into the capture. That is a stronger guarantee than filtering by
    user id would have given us.
  * Reading a capture endpoint is passive. Nothing we do here is audible to
    anyone in the channel or on the stream.

Four things must be true or the cable carries literal digital zeros: the
streaming account muted but NOT deafened, the cable endpoint unmuted in
Windows, Discord's own output volume above zero, and the account rejoined to
the voice channel after any output-device change (Discord does not migrate an
in-progress voice connection).

Imports are guarded because production runs an interpreter without numpy,
sounddevice or faster-whisper. Importing this module there must not raise.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path
from typing import Any, NamedTuple

import config

log = logging.getLogger("athena.voice")

try:  # pragma: no cover - exercised by which interpreter runs this
    import numpy as np
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except ImportError:  # prod's global interpreter has neither
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]
    AUDIO_AVAILABLE = False


# ----------------------------------------------------------------------
# audio constants
# ----------------------------------------------------------------------

# Discord's voice format, fixed by the protocol and therefore by the cable.
CAPTURE_RATE = 48000
CAPTURE_CHANNELS = 2

# What Whisper wants. 48000 / 16000 is exactly 3, so decimation is a slice.
WHISPER_RATE = 16000
DECIMATION = CAPTURE_RATE // WHISPER_RATE

# 50 ms per callback: fine-grained enough that the silence hold and the
# pre-roll are accurate, coarse enough that we are not waking a thread 1000
# times a second.
BLOCK = CAPTURE_RATE // 20


# ----------------------------------------------------------------------
# wake word
# ----------------------------------------------------------------------

# The bot was called Nyx before this, and the wake word is the reason it isn't.
# "Nyx" is one syllable ending in a sibilant, and no amount of tuning made it
# work: across two model sizes it was transcribed as "next", "nix", "mix",
# "knicks", and — with the prefix fused on — "headaches" and "A next". Every
# one of those needed a homophone entry, and each entry widened the surface for
# false wakes.
#
# The name was chosen by measurement instead. Success rates over a few hundred
# live utterances, medium model:
#
#     Athena  26/28    Ophelia   10/10    Computer 9/10
#     Jarvis   6/9     Bellatrix  5/9     Vesper   3/5
#     Artemis  4/10    Ursula     ~4/10   Lilith   0/4
#
# The pattern in the failures is that the *opening consonant* gets clipped —
# Vesper became "Esper", Bellatrix "Bella", Morticia "Or tissue". Discord's
# own voice gate takes a moment to open and eats the attack, and our pre-roll
# cannot recover audio that was never transmitted. Names starting on an open
# vowel survive that; names starting on a plosive do not. "Artemis" scored
# badly despite starting with a vowel because the R immediately behind it
# behaves like a consonant attack.
#
# So: keep this list short. It is spelling variants of a name the model already
# hears correctly, not damage control for one it doesn't.
DEFAULT_WAKE_WORDS = "athena athina atena athene"

# Optional, not required. A prefix was mandatory when the name was "Nyx",
# because a one-syllable name needed the extra structure to avoid false wakes.
# "Athena" carries enough acoustic information alone, and requiring "hey" in
# front of it reintroduces exactly the clipped-attack problem the name was
# chosen to avoid — "hey" is short and unstressed and gets mangled first.
DEFAULT_WAKE_PREFIXES = "hey hi ok okay hay"

# Empty on purpose. Every entry this used to hold — "headaches", "a next",
# "heynix" — was a patch for a name the model could not hear. They are not
# free to keep: each one matches real speech somewhere.
DEFAULT_WAKE_COMPOUNDS = ""

# Whisper invents these from breath, room tone and silence. They are not
# things anyone said. This is a universal failure mode, not a quirk of our
# audio, so the list is worth having from the first run.
NOISE_HALLUCINATIONS = frozenset({
    "", ".", "you", "you.", "thank you", "thank you.", "thanks",
    "thanks for watching", "thanks for watching!", "bye", "bye.",
    "bye bye", "okay", "okay.", "so", "so.", "uh", "um", "hmm",
    "[ silence ]", "(silence)", "[music]", "(music)", "*", "♪",
})

_PUNCT = r"[\s,.!?'\"\-]"


def build_wake_pattern(words: str, prefixes: str, require_prefix: bool,
                       compounds: str = "") -> re.Pattern:
    """Compile the wake matcher.

    Two things this has to get right, both learned from live audio.

    **Where it may match.** Not simply at the start of the utterance. People
    say "that's a good one. Hey athena, what's the score?" without leaving a full
    second of silence first, so the wake phrase lands mid-transcript and a
    start-anchored pattern misses a perfectly clear command. Matching after a
    sentence boundary accepts that while still rejecting "I was telling Athena to
    skip stuff yesterday", where the name is inside a sentence rather than
    starting one. Anywhere-matching would accept both, which is why we don't.

    **Compounds.** Whole phrases, for the case where recognition fuses a prefix
    into the name and leaves nothing for a prefix-plus-name pattern to find.
    Empty by default now; it existed to prop up a name the model misheard.
    """
    name = "|".join(re.escape(w) for w in words.split() if w)
    pre = "|".join(re.escape(p) for p in prefixes.split() if p)
    if require_prefix and pre:
        head = rf"(?:{pre}){_PUNCT}+(?:{name})"
    elif pre:
        head = rf"(?:(?:{pre}){_PUNCT}+)?(?:{name})"
    else:
        head = rf"(?:{name})"

    # Comma-separated, and each entry may be several words — the gap between
    # them matches the same punctuation the rest of the pattern allows, so
    # "a next", "A, next" and "A next." all match the one entry.
    phrases = []
    for entry in compounds.split(","):
        parts = entry.split()
        if parts:
            phrases.append(rf"{_PUNCT}+".join(re.escape(p) for p in parts))
    if phrases:
        head = rf"(?:{head}|(?:{'|'.join(phrases)}))"

    # Unanchored on purpose — strip_wake decides whether a given position is
    # acceptable, because that decision has to look at what follows.
    return re.compile(rf"(?:{head})\b{_PUNCT}*", re.IGNORECASE)


# Words that can only open an instruction. Used to rescue a wake word sitting
# mid-sentence: people say "Let's see, Athena, play Wonderwall" in one breath,
# and requiring a clause boundary threw that away.
_COMMAND_START = re.compile(
    r"^(?:play|queue|pause|resume|unpause|stop|skip|next|previous|back|forward|"
    r"rewind|seek|volume|mute|unmute|louder|quieter|search|find|look|show|"
    r"switch|karaoke|music|spotify|youtube|twitch|kick|watch|put|start|stream|turn|"
    r"restart|clear|shuffle|repeat|status|tracks|audio|subs|subtitles|sub|dub|"
    r"what|what's|whats|who|when|where|how|why|libraries|help|"
    # Conversation is a command too — she answers questions and writes things.
    # "Athena, tell me a joke" was dropped mid-conversation because "tell" was
    # missing here, which read as her ignoring a perfectly clear request.
    #
    # Only words that can open an instruction. Linking verbs are deliberately
    # absent: "is", "was", "does" and friends turn every remark ABOUT her into
    # a command, and "I think Athena is annoying" fired on exactly that.
    r"tell|say|give|explain|describe|settle|rate|can|could|would|will)\b",
    re.IGNORECASE,
)


# Mirrors the "shut_up" FAST pattern in brain.py exactly — kept as a second
# copy rather than an import because voice.py has no other reason to depend
# on brain.py, which pulls in the whole tool stack. If the phrasing changes
# there, change it here too.
#
# Needed because suppression (see suppress_when, below) can no longer just
# drop audio outright while she is speaking: that made "Athena, shut up"
# impossible to say WHILE she is talking, which is the one moment anyone
# actually says it. This is the one phrase allowed through despite the
# suppression flag.
_STOP_TALKING = re.compile(
    r"^(?:shut\s*up|be\s+quiet|quiet|silence|stop\s+talking|"
    r"stop\s+speaking|enough|shush|hush|zip\s+it)!?\.?$", re.IGNORECASE,
)


def _position_is_acceptable(before: str, after: str) -> bool:
    """May a wake word at this position count as addressing the bot?

    Three ways to qualify, in descending order of confidence:

      * it opens the utterance — the ordinary case;
      * punctuation sits directly behind it, so it opens a new clause —
        "That's a good one. Athena, skip" and "Let's see, Athena, play X";
      * it is mid-sentence, but what follows is unmistakably an instruction.

    That third clause is what keeps this from collapsing into "match anywhere",
    which would fire on "I was telling Athena to skip stuff yesterday" — there
    the remainder opens with "to", not with a verb.
    """
    if not before:
        return True
    if before[-1] in ".!?,;:—-":
        return True
    return bool(_COMMAND_START.match(after))


def strip_wake(text: str, pattern: re.Pattern) -> str | None:
    """Return the command with the wake phrase removed, or None if unwoken.

    Stripping is not cosmetic. Athena's text commands carry no name prefix —
    fast_match() anchors its patterns at the start of the message — so handing
    the brain "athena skip the song" would miss every fast-path regex and dump
    the command into the model as a tier-2 fallthrough.

    Everything before the wake phrase is discarded along with it: in "Let's
    see, Athena, play Wonderwall" the lead-in was talk between people, not part
    of the instruction.

    Every occurrence is considered, not just the first — an utterance may
    mention the name once in passing and then actually address her.
    """
    text = (text or "").strip()
    for match in pattern.finditer(text):
        before = text[: match.start()].rstrip()
        after = text[match.end():].strip()
        if _position_is_acceptable(before, after):
            return after
    return None


def is_hallucination(text: str) -> bool:
    """True for the fixed vocabulary Whisper invents from silence and breath.

    Checked before wake matching, because these are never anything anyone said
    and there is no point asking whether they woke the bot.
    """
    cleaned = (text or "").strip().lower()
    return not cleaned or cleaned in NOISE_HALLUCINATIONS


def too_long(command: str, max_words: int) -> bool:
    """True for a command long enough to be a film rather than an instruction.

    Applied to the command with the wake phrase already stripped, and only
    after it woke the bot — not to the raw transcript. Checking earlier throws
    away legitimate long requests like "play the one where the guy goes to
    space and meets his dad", which is a real thing someone says to a media
    bot. The cap exists for the case where a TV happens to utter the wake
    phrase and then keeps talking.
    """
    return len((command or "").split()) > max_words


# ----------------------------------------------------------------------
# segmentation
# ----------------------------------------------------------------------

def to_whisper_audio(block):
    """48 kHz stereo float32 -> 16 kHz mono float32.

    Downmix then decimate. Skipping this produces confident nonsense rather
    than an error, because Whisper will happily consume a wrongly-rated buffer.
    """
    mono = block.mean(axis=1) if block.ndim > 1 else block
    return mono[::DECIMATION].astype("float32")


class Utterance(NamedTuple):
    """One captured stretch of speech, with everything decided about it.

    Carries wall-clock bounds because attribution happens much later, on the
    event loop, after Whisper has had its turn — by which point "now" is no
    longer anywhere near when the words were actually said.
    """

    audio: Any
    suppressed: bool
    started_at: float
    ended_at: float


class Segmenter:
    """Cut a continuous stream into utterances using an energy gate.

    A general-purpose VAD would be overkill here. The cable carries *exact*
    digital silence between utterances — Discord transmits nothing when nobody
    is speaking — so the speech/not-speech decision is unusually clean and an
    RMS threshold with hysteresis is entirely sufficient. That also removes a
    fragile native dependency from the build.

    Keeps a pre-roll ring buffer so the wake word is not clipped: by the time
    energy crosses the threshold, the first syllable is already in the past.
    """

    def __init__(self, *, threshold: float, silence_hold_s: float,
                 preroll_s: float, min_s: float, max_s: float,
                 rate: int = CAPTURE_RATE):
        self.threshold = threshold
        self.rate = rate
        self.silence_needed = int(silence_hold_s * rate)
        self.min_samples = int(min_s * rate)
        self.max_samples = int(max_s * rate)
        self.preroll = deque(maxlen=max(1, int(preroll_s * rate / BLOCK)))
        self._active: list = []
        self._silence = 0
        self._held = 0

    @property
    def speaking(self) -> bool:
        return bool(self._active)

    def reset(self) -> None:
        """Abandon whatever is part-captured and clear the pre-roll.

        Used when capture is suppressed mid-utterance — carrying the fragment
        across the gap would splice unrelated audio either side of it.
        """
        self._active = []
        self._silence = 0
        self._held = 0
        self.preroll.clear()

    def feed(self, block) -> list:
        """Push one block; return any utterances that just completed."""
        rms = float(np.sqrt((block ** 2).mean())) if block.size else 0.0
        loud = rms > self.threshold
        done = []

        if not self._active:
            if loud:
                # Open with the pre-roll already in front of the first block.
                self._active = list(self.preroll)
                self._active.append(block.copy())
                self._held = sum(b.shape[0] for b in self._active)
                self._silence = 0
            else:
                self.preroll.append(block.copy())
            return done

        self._active.append(block.copy())
        self._held += block.shape[0]
        self._silence = 0 if loud else self._silence + block.shape[0]

        if self._silence >= self.silence_needed or self._held >= self.max_samples:
            utterance = np.concatenate(self._active)
            # Trailing silence carries no information and costs Whisper time.
            if self._silence:
                utterance = utterance[: max(0, len(utterance) - self._silence)]
            self._active = []
            self._held = 0
            self._silence = 0
            self.preroll.clear()
            if len(utterance) >= self.min_samples:
                done.append(utterance)
            else:
                log.debug("Dropped a %.2fs blip", len(utterance) / self.rate)
        return done


# ----------------------------------------------------------------------
# transcription
# ----------------------------------------------------------------------

def _register_cuda_dlls() -> None:
    """Put the nvidia wheels' DLLs where CTranslate2 will find them.

    CTranslate2 resolves cublas64_12.dll by bare name through PATH, and
    os.add_dll_directory() is NOT consulted for that — which fails with
    "Library cublas64_12.dll is not found or cannot be loaded" even though the
    file is plainly installed. Must run before faster_whisper is imported.

    Windows only, and returning early rather than relying on the glob below
    finding nothing. It would find nothing on a Mac — there is no Lib/ and no
    nvidia/ — but a function whose macOS behaviour is an accident of path
    casing is one refactor away from quietly prepending garbage to PATH.
    """
    if os.name != "nt":
        return
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    dirs = [str(d) for d in root.glob("*/bin")]
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")


class Transcriber:
    """faster-whisper, loaded once, one inference at a time.

    The lock is not optional: the model is not safe to call concurrently, and
    two people talking over each other produces exactly that. Serialising also
    keeps the GPU free for the language model, which shares this card.
    """

    def __init__(self, model: str, device: str, compute_type: str,
                 cpu_threads: int = 0):
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._model is not None:
            return
        _register_cuda_dlls()
        from faster_whisper import WhisperModel  # deferred: heavy, and optional

        log.info("Loading whisper %s (%s, %s, %s threads)", self.model_name,
                 self.device, self.compute_type, self.cpu_threads or "auto")
        # cpu_threads is ignored by CTranslate2 unless the device is the CPU,
        # so this is safe to pass unconditionally and matters only on the Mac,
        # where the CPU is the only device available to it.
        self._model = WhisperModel(self.model_name, device=self.device,
                                   compute_type=self.compute_type,
                                   cpu_threads=self.cpu_threads)

    def transcribe(self, audio) -> str:
        """Blocking. Call from a worker thread, never the event loop.

        beam_size is worth spending on here. Greedy decoding (beam_size=1) is
        the default reflex for latency, but utterances are a few seconds long
        and the model runs far faster than realtime, so the time saved is
        invisible while the accuracy cost is not — and every mis-heard wake
        word is a command that silently does nothing.
        """
        self.load()
        with self._lock:
            # No initial_prompt on purpose — see DEFAULT_WAKE_PREFIXES.
            segments, _ = self._model.transcribe(
                audio, language="en", beam_size=config.VOICE_BEAM_SIZE,
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()


# ----------------------------------------------------------------------
# the listener
# ----------------------------------------------------------------------

def find_device(name_fragment: str) -> int:
    """Resolve a capture device by name, preferring config.AUDIO_HOST_API.

    Host API matters on the Windows hardware: MME reads the VB-Cable as silent
    while WASAPI, DirectSound and WDM-KS all see the same signal. macOS has
    only Core Audio, so the preference is satisfied trivially there rather than
    falling through and warning about it. Resolving by name rather than a fixed
    index also survives devices being added or removed.
    """
    apis = sd.query_hostapis()
    fallback = None
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        if not dev["name"].lower().startswith(name_fragment.lower()):
            continue
        if config.AUDIO_HOST_API in apis[dev["hostapi"]]["name"]:
            return index
        if fallback is None:
            fallback = index
    if fallback is not None:
        log.warning("No %s entry for %r — falling back to a different host API",
                    config.AUDIO_HOST_API, name_fragment)
        return fallback
    raise RuntimeError(_no_device_message(name_fragment, "capture"))


def _no_device_message(name_fragment: str, kind: str) -> str:
    """Say which devices DO exist, and on macOS how to get the missing one.

    voice.start() swallows this into "carrying on without it", so the log line
    is the only thing anyone gets. A bare "no capture device whose name starts
    with 'BlackHole 2ch'" leaves them checking a device list by hand to find
    out what is actually there — and on this platform the answer is nearly
    always that BlackHole simply is not installed, because macOS has no
    built-in loopback and nothing prompts you about it.
    """
    channels = "max_input_channels" if kind == "capture" else "max_output_channels"
    try:
        have = sorted({d["name"] for d in sd.query_devices() if d[channels] >= 1})
    except Exception:
        have = []
    msg = (f"no {kind} device whose name starts with {name_fragment!r}. "
           f"Available: {', '.join(have) or 'none'}.")
    if sys.platform == "darwin" and name_fragment.lower().startswith("blackhole"):
        which = "blackhole-16ch" if "16" in name_fragment else "blackhole-2ch"
        msg += (f" macOS has no built-in loopback device — install one with "
                f"`brew install --cask {which}` (it needs your password, and a "
                f"restart of whatever is using audio).")
    return msg


def open_tuning_log(path: str):
    """A dedicated file recording what was heard and what was decided.

    Separate from the bot's own log on purpose. The main log records only the
    shape of an utterance — everything said in the channel passes through the
    transcriber, and most of it is people talking to each other. This file is
    the one place the words are written down, so turning it off is a single
    setting rather than an audit of every log line.

    Its own handler with propagate=False, or every line would also surface in
    the main log and defeat the split.
    """
    if not path:
        return None
    target = Path(path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parent / target
    target.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("athena.voice.tuning")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    log.info("Voice tuning log: %s", target)
    return logger


class VoiceListener:
    """Owns the capture stream and the transcribe/dispatch pipeline.

    Threading, which is where this kind of code usually goes wrong:

      * sounddevice invokes the callback on a PortAudio thread. It only
        segments and hands off — no awaits, no I/O, no model calls.
      * Completed utterances cross to the event loop via call_soon_threadsafe
        onto a bounded queue. Bounded and drop-oldest, so a wedged consumer
        cannot grow memory without limit.
      * One consumer task transcribes in a worker thread under a timeout, then
        dispatches. A stale command is worse than a dropped one.
    """

    QUEUE_MAX = 8

    def __init__(self, *, handler, notify=None, loop=None, suppress_when=None,
                 attribute=None):
        """`handler(text) -> str | None` runs a command; `notify(str)` reports.

        `suppress_when()` is polled per audio block and drops it while true —
        used to stop Athena transcribing her own speech. Discord never loops a
        mic back to its own speakers, so she cannot hear herself directly, but
        her voice returns through anyone else's open mic in the channel.

        `attribute(started_at, ended_at) -> str | None` names whoever was
        talking over that window, for the tuning log. The cable carries a
        mixed stream with no identity in it, so this comes from Discord
        rather than from the audio — see speakers.py.
        """
        if not AUDIO_AVAILABLE:
            raise RuntimeError(
                "voice needs numpy and sounddevice; this interpreter has neither"
            )
        self.handler = handler
        self.notify = notify
        self.suppress_when = suppress_when
        self.attribute = attribute
        self.loop = loop or asyncio.get_event_loop()

        self.pattern = build_wake_pattern(
            config.VOICE_WAKE_WORDS, config.VOICE_WAKE_PREFIXES,
            config.VOICE_REQUIRE_PREFIX, config.VOICE_WAKE_COMPOUNDS,
        )
        self.segmenter = Segmenter(
            threshold=config.VOICE_THRESHOLD,
            silence_hold_s=config.VOICE_SILENCE_MS / 1000,
            preroll_s=config.VOICE_PREROLL_MS / 1000,
            min_s=config.VOICE_MIN_MS / 1000,
            max_s=config.VOICE_MAX_S,
        )
        self.transcriber = Transcriber(
            config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE,
            config.WHISPER_CPU_THREADS,
        )
        self.audio_dir = None
        if config.VOICE_SAVE_AUDIO:
            self.audio_dir = Path(config.VOICE_SAVE_AUDIO)
            if not self.audio_dir.is_absolute():
                self.audio_dir = Path(__file__).resolve().parent / self.audio_dir
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            log.info("Saving utterance audio to %s (keeping %d)",
                     self.audio_dir, config.VOICE_SAVE_AUDIO_KEEP)

        self.tuning = open_tuning_log(config.VOICE_TUNING_LOG)
        if self.tuning:
            self.tuning.info(
                "=== session start | model=%s | wake=%r | prefixes=%r | "
                "compounds=%r | dry_run=%s ===",
                config.WHISPER_MODEL, config.VOICE_WAKE_WORDS,
                config.VOICE_WAKE_PREFIXES, config.VOICE_WAKE_COMPOUNDS,
                config.VOICE_DRY_RUN,
            )
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self._stream = None
        self._task: asyncio.Task | None = None
        # True if any block of the utterance currently being assembled
        # arrived while suppress_when() was true. Carried from the capture
        # callback to the consumer alongside the audio itself.
        self._utterance_suppressed = False
        self.stats = {"utterances": 0, "woken": 0, "ignored": 0,
                      "noise": 0, "dropped": 0, "suppressed": 0}

    # -- capture side (PortAudio thread) --------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("capture status: %s", status)
        try:
            # Still fed to the segmenter rather than dropped: dropping
            # outright, as this used to, means "Athena, shut up" said WHILE
            # she is talking never even reaches the segmenter — silenced
            # exactly when someone needs it. Suppression instead is tracked
            # per-utterance and applied in _handle_utterance, where only the
            # stop phrase is let through a suppressed utterance.
            suppressed = bool(self.suppress_when is not None and self.suppress_when())
            if suppressed:
                self.stats["suppressed"] += 1
            if not self.segmenter.speaking:
                # Nothing carries over from a finished utterance.
                self._utterance_suppressed = suppressed
            elif suppressed:
                self._utterance_suppressed = True

            for audio in self.segmenter.feed(indata):
                was_suppressed = self._utterance_suppressed
                self._utterance_suppressed = False
                self.loop.call_soon_threadsafe(
                    self._enqueue, self._stamp(audio, was_suppressed))
        except Exception:
            # Never let an exception escape into PortAudio; it kills the stream.
            log.exception("Segmenter failed on a block")

    def _attribute(self, item: Utterance) -> str | None:
        """Who Discord says was talking, or None. Never raises.

        Attribution is a log annotation and nothing depends on it, so a
        failure here must not cost an utterance.
        """
        if self.attribute is None:
            return None
        try:
            return self.attribute(item.started_at, item.ended_at)
        except Exception:
            log.debug("Could not attribute an utterance", exc_info=True)
            return None

    def _stamp(self, audio, suppressed: bool) -> Utterance:
        """Work out when this audio actually happened.

        Called the moment the segmenter closes an utterance, which is not
        the moment the speech ended: a gap of silence had to elapse first,
        and the segmenter trims that silence back off before handing the
        audio over. So the end is a silence-hold in the past — unless the
        utterance was cut off at the maximum length instead, in which case
        it runs right up to now.

        Approximate by construction, which is why matching against it is
        given room either side. See config.VOICE_SPEAKER_TOLERANCE_MS.
        """
        now = time.time()
        hit_limit = len(audio) >= self.segmenter.max_samples
        ended = now if hit_limit else now - (config.VOICE_SILENCE_MS / 1000)
        return Utterance(audio, suppressed, ended - len(audio) / CAPTURE_RATE, ended)

    def _enqueue(self, item: Utterance) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except Exception:
                pass
            log.warning("Voice queue full — dropped the oldest utterance")

    # -- consume side (event loop) --------------------------------------

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._handle_utterance(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A dead consumer means the bot silently stops listening
                # forever, with nothing in the log to say why.
                log.exception("Voice pipeline failed on an utterance")
            finally:
                self._queue.task_done()

    async def _handle_utterance(self, item: Utterance) -> None:
        self.stats["utterances"] += 1
        suppressed = item.suppressed
        seconds = len(item.audio) / CAPTURE_RATE
        audio = to_whisper_audio(item.audio)
        # Asked before the reply is generated, not after: she speaks her
        # answers into the same channel, and by the time a reply exists she
        # may well be the one Discord reports as talking.
        who = self._attribute(item)
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self.transcriber.transcribe, audio),
                timeout=config.VOICE_STT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.error("Whisper timed out on %.1fs of audio", seconds)
            self._tune(seconds, "STT-TIMEOUT", "", None, None)
            return

        # Decide the verdict first, act second, then record once. Keeping the
        # decision in one place is what makes the tuning log a faithful account
        # of why each utterance went the way it did.
        #
        # Order matters. Hallucinations first, because they are never speech.
        # Then the wake gate. The length cap comes last, applied to the command
        # itself — a long request that was properly woken is legitimate.
        command = None
        reply = None

        if is_hallucination(text):
            self.stats["noise"] += 1
            verdict = "NOISE"
        else:
            command = strip_wake(text, self.pattern)
            if command is None:
                self.stats["ignored"] += 1
                verdict = "NO-WAKE"
            elif not command:
                verdict = "WAKE-EMPTY"
            elif too_long(command, config.VOICE_MAX_WORDS):
                self.stats["noise"] += 1
                verdict = "TOO-LONG"
            else:
                self.stats["woken"] += 1
                verdict = "WAKE-DRYRUN" if config.VOICE_DRY_RUN else "WAKE"

        # A wake heard while she was speaking is, almost always, her own
        # voice returning through someone else's open mic rather than a real
        # command — the self-transcription loop suppression exists to break.
        # The stop phrase is the one exception: it is the one thing anyone
        # actually needs to say WHILE she is talking, so it is let through
        # even though everything else heard during suppression is not.
        if suppressed and verdict in ("WAKE", "WAKE-DRYRUN") and not _STOP_TALKING.match(command):
            self.stats["woken"] -= 1
            self.stats["ignored"] += 1
            verdict = "ECHO" if verdict == "WAKE" else "ECHO-DRYRUN"

        # The main log gets the shape, never the words. Everything said in the
        # channel passes through here and most of it is people talking to each
        # other; the tuning log below is where the text goes, deliberately and
        # separately.
        log.info("voice %.1fs: %s (%d words)", seconds, verdict, len(text.split()))

        if verdict == "WAKE":
            reply = await self.handler(command)

        self._tune(seconds, verdict, text, command, reply, who)
        self._save_audio(verdict, text, audio)

        if verdict == "WAKE" and reply and self.notify:
            await self.notify(f"\N{STUDIO MICROPHONE} `{command}`\n{reply}")

    def _save_audio(self, verdict: str, text: str, audio) -> None:
        """Write exactly what Whisper was given, for listening back.

        Saves the 16 kHz mono buffer rather than the 48 kHz capture on purpose:
        the question this answers is "what did the model actually hear", and
        anything lost in the downmix is part of that answer.

        Filenames carry the timestamp and the transcript so a file lines up
        with its row in the tuning log at a glance. Off by default, and pruned,
        because everything said in the channel passes through here.
        """
        if self.audio_dir is None:
            return
        try:
            stamp = datetime.datetime.now().strftime("%H%M%S")
            label = "".join(c if c.isalnum() else "_" for c in (text or "empty"))[:44]
            path = self.audio_dir / f"{stamp}_{verdict}_{label or 'empty'}.wav"
            pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(WHISPER_RATE)
                w.writeframes(pcm.tobytes())

            keep = config.VOICE_SAVE_AUDIO_KEEP
            files = sorted(self.audio_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
            for old in files[:-keep] if len(files) > keep else []:
                old.unlink(missing_ok=True)
        except Exception:
            log.debug("Could not save utterance audio", exc_info=True)

    def _tune(self, seconds, verdict, text, command, reply, who=None) -> None:
        """One line per utterance in the tuning log, if one is open."""
        if self.tuning is None:
            return
        parts = [f"{seconds:4.1f}s", f"{verdict:<11}"]
        # Before the transcript, so a column of names stays scannable when
        # reading back a long session. "?" rather than a blank for nobody,
        # because an empty column reads as a bug in the logging.
        #
        # Wide enough for two names: at 20 this truncated the second speaker
        # to a bare comma, which read as a logging fault rather than as two
        # people talking at once.
        if self.attribute is not None:
            parts.append(f"{(who or '?')[:30]:<30}")
        parts.append(f"in={text!r}")
        if command is not None:
            parts.append(f"cmd={command!r}")
        if reply is not None:
            parts.append(f"reply={str(reply)[:120]!r}")
        self.tuning.info(" | ".join(parts))

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        device = find_device(config.VOICE_DEVICE)
        info = sd.query_devices(device)
        log.info("Listening on [%d] %s", device, info["name"])

        # Load before opening the stream so the first command is not slowed by
        # a cold model, and so a missing CUDA library fails loudly at startup
        # rather than on the first thing anyone says.
        await asyncio.to_thread(self.transcriber.load)

        self._stream = sd.InputStream(
            samplerate=CAPTURE_RATE, channels=CAPTURE_CHANNELS, dtype="float32",
            blocksize=BLOCK, device=device, callback=self._callback,
        )
        self._stream.start()
        self._task = self.loop.create_task(self._consume())
        log.info("Voice ready — wake phrase required, dry_run=%s",
                 config.VOICE_DRY_RUN)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.debug("Could not close the capture stream", exc_info=True)
            self._stream = None
        log.info("Voice stopped — %s", self.stats)


_listener: VoiceListener | None = None


async def start(*, handler, notify=None, suppress_when=None,
                attribute=None) -> VoiceListener | None:
    """Start listening, or return None if voice is off or unavailable.

    Never raises into startup: voice is an accessory, and a bot that refuses to
    boot because a sound card moved is worse than one that cannot hear.
    """
    global _listener
    if not config.VOICE_ENABLED:
        return None
    if not AUDIO_AVAILABLE:
        log.warning("VOICE_ENABLED is set but numpy/sounddevice are missing here")
        return None
    try:
        listener = VoiceListener(handler=handler, notify=notify,
                                 suppress_when=suppress_when,
                                 attribute=attribute)
        await listener.start()
    except Exception:
        log.exception("Voice failed to start — carrying on without it")
        return None
    _listener = listener
    return listener


async def stop() -> None:
    global _listener
    if _listener is not None:
        await _listener.stop()
        _listener = None
