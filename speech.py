"""Athena's voice, played back into the Discord channel.

The mirror of voice.py. That module captures what Discord *plays*; this one
feeds what Discord *transmits*:

    others speak  -> Discord speaker -> CABLE Input   -> CABLE Output   -> voice.py
    Athena speaks -> speech.py       -> CABLE-B Input -> CABLE-B Output -> Discord mic

The two cables run in opposite directions, and that asymmetry buys something
valuable: Discord never loops your own microphone back to your speakers, so
Athena physically cannot hear herself. No echo cancellation, no feedback loop.

She can still hear herself *indirectly* — through someone else's open mic in
the channel — so capture is suppressed while she speaks. That is what
is_speaking() is for; voice.py consults it and drops blocks.

Synthesis itself lives in a separate process (tts/tts_server.py) behind a tiny
HTTP contract. It runs Python 3.10 because every GPU TTS path fails on the
bot's 3.14 for want of wheels, and keeping it out of process also keeps a
second CUDA stack away from faster-whisper.

Imports are guarded: production has neither numpy nor sounddevice, and
importing this module there must not raise.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import threading
import wave

import config

log = logging.getLogger("athena.speech")

try:  # pragma: no cover - depends which interpreter runs this
    import numpy as np
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]
    AUDIO_AVAILABLE = False


# ----------------------------------------------------------------------
# making a reply speakable
# ----------------------------------------------------------------------

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MD_CHARS = re.compile(r"[*_#>|]+")
_WHITESPACE = re.compile(r"\s+")


# Curly punctuation the model emits, mapped to what a TTS engine can read.
# Dashes become commas rather than hyphens: a dash is a pause when spoken, and
# Kokoro reads a bare hyphen as either nothing or the word.
_SPACED_PUNCT = re.compile(r"\s+([,.;:!?])")

_TYPOGRAPHIC = {
    ord("\u2018"): "'", ord("\u2019"): "'",           # \u2018 \u2019
    ord("\u201c"): '"', ord("\u201d"): '"',           # \u201c \u201d
    ord("\u2013"): ",", ord("\u2014"): ",",           # en dash, em dash
    ord("\u2026"): "...",                             # ellipsis
    ord("\u00a0"): " ", ord("\u202f"): " ",           # non-breaking spaces
    ord("\u2032"): "'", ord("\u02bc"): "'",           # prime, modifier apostrophe
}


def sanitize_for_speech(text: str, max_chars: int | None = None) -> str:
    """Flatten a reply into something worth reading aloud.

    Athena's replies are full of markdown — bold titles, italic asides — and a
    TTS engine reads asterisks out as the word, spells URLs character by
    character, and stumbles over emoji. This is the same shape as the routine
    in the reference implementation this design borrows from, which was arrived
    at by hearing each of those failures.

    Truncation prefers a sentence boundary so she never stops mid-thought.
    """
    max_chars = config.TTS_MAX_CHARS if max_chars is None else max_chars
    text = _CODE_BLOCK.sub(" ", text or "")
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)          # keep the label, drop the target
    text = _URL.sub("", text)
    text = _MD_CHARS.sub(" ", text)
    # Typographic punctuation FIRST, or the ascii strip below eats it. The
    # model writes curly apostrophes, so "You\u2019re" became "Youre" — not a
    # word, and Kokoro read it as something between "you are" and nonsense.
    # Reported as her struggling with contractions, which she was, because she
    # was never given one. The straight-quote version came through fine, which
    # is why this survived: half the replies were correct.
    text = text.translate(_TYPOGRAPHIC)
    text = text.encode("ascii", "ignore").decode()   # emoji and friends
    text = _WHITESPACE.sub(" ", text).strip()
    # A dash that was spaced — like this — leaves " ," behind once it becomes a
    # comma. Harmless in print, but it is a token boundary to a TTS engine.
    text = _SPACED_PUNCT.sub(r"\1", text)

    if len(text) > max_chars:
        cut = text[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > 0 else cut
    return text.strip()


# ----------------------------------------------------------------------
# playback
# ----------------------------------------------------------------------

def find_output_device(name_fragment: str) -> int:
    """Resolve a playback device by name, preferring config.AUDIO_HOST_API.

    Same reasoning as the capture side: on the Windows hardware MME reports
    these cables as silent while WASAPI works. On macOS there is only Core
    Audio and the preference costs nothing.
    """
    apis = sd.query_hostapis()
    fallback = None
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] < 1:
            continue
        if not dev["name"].lower().startswith(name_fragment.lower()):
            continue
        if config.AUDIO_HOST_API in apis[dev["hostapi"]]["name"]:
            return index
        if fallback is None:
            fallback = index
    if fallback is not None:
        log.warning("No %s entry for %r — using another host API",
                    config.AUDIO_HOST_API, name_fragment)
        return fallback
    # Same reasoning as voice._no_device_message: this surfaces only as a log
    # line, so it has to carry the fix with it.
    import voice
    raise RuntimeError(voice._no_device_message(name_fragment, "playback"))


def _close_quietly(stream) -> None:
    """Release a stream that failed to start, ignoring further errors.

    An audio endpoint stays claimed until the stream holding it is closed,
    so this runs on every failure path — a stream we could not start is
    still a stream that can lock the device out for everything after it.
    """
    if stream is None:
        return
    try:
        stream.close()
    except Exception:
        log.debug("Could not close a failed output stream", exc_info=True)


# Half the taps of a windowed-sinc kernel, per polyphase branch. 16 puts the
# stopband far enough down that the images below are inaudible, and keeps the
# filter short enough to convolve a minute of speech in a few milliseconds.
_RESAMPLE_TAPS = 16


def _antialias_kernel(factor: int) -> "np.ndarray":
    """Windowed-sinc low-pass at the lower of the two Nyquist limits."""
    half = _RESAMPLE_TAPS * factor
    n = np.arange(-half, half + 1, dtype="float64")
    cutoff = 0.5 / factor
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(len(n))
    return h


# Below this a clip is a noise rather than a word. "Fine." is the shortest line
# that works, at 410ms; "Mm." failed at 120ms. 300 leaves room either side.
ACK_MIN_MS = 300


def _voiced_ms(audio, rate: int) -> int:
    """How much of a clip is actually speech, ignoring the silence around it."""
    win = max(int(rate * 0.010), 1)
    usable = audio[: len(audio) // win * win]
    if usable.size == 0:
        return 0
    env = np.sqrt((usable.reshape(-1, win) ** 2).mean(axis=1))
    return int((env > 0.01).sum() * 10)


def normalize(audio):
    """Bring a line up to a normal speaking level, without ever clipping.

    Kokoro delivers about -23 dBFS RMS against a -18 dBFS broadcast norm, and
    the quiet end of that is where a voice gate starts eating syllables — which
    is the shape the choppiness complaint took. Levelling also keeps one line
    from being noticeably quieter than the next.

    The gain is the smaller of what the RMS target asks for and what the peak
    ceiling allows, so the ceiling always wins and the result cannot clip.
    """
    if not config.TTS_NORMALIZE:
        return audio
    target = 10.0 ** (config.TTS_LEVEL_DBFS / 20.0)
    ceiling = 10.0 ** (-1.0 / 20.0)          # -1 dBFS, a little true headroom
    # Measured over speech only: the silence between words would drag the
    # average down and make every line come out too loud.
    voiced = audio[np.abs(audio) > 0.005]
    if voiced.size == 0:
        return audio
    rms = float(np.sqrt((voiced ** 2).mean()))
    peak = float(np.abs(audio).max())
    if rms <= 0.0 or peak <= 0.0:
        return audio
    gain = min(target / rms, ceiling / peak)
    return (audio * gain).astype("float32")


def resample(mono, source_rate: int, target_rate: int):
    """Rate-convert with a proper anti-imaging filter.

    Not optional: WASAPI shared mode refuses a mismatched rate outright with
    "Invalid sample rate" rather than converting, and Kokoro emits 24kHz while
    these cables are configured at 48kHz.

    This used to be np.interp, described here as "plenty for speech". Measured,
    it was not: linear interpolation attenuates the spectral images that
    zero-stuffing creates but does not remove them, and 24k -> 48k left 27dB of
    energy above 12kHz that Kokoro never produced. Kokoro's own output stops at
    12kHz, so every bit of that was invented by the resampler, and it reads as
    a hard, gritty edge on her voice.

    Zero-stuff, low-pass with a windowed sinc, then decimate — the textbook
    polyphase arrangement, written out rather than pulled from scipy, which is
    not a dependency of the bot.
    """
    if source_rate == target_rate:
        return mono

    from math import gcd

    g = gcd(int(source_rate), int(target_rate))
    up, down = int(target_rate) // g, int(source_rate) // g

    x = np.asarray(mono, dtype="float64")
    if up > 1:
        stuffed = np.zeros(len(x) * up, dtype="float64")
        stuffed[::up] = x
    else:
        stuffed = x

    kernel = _antialias_kernel(max(up, down))
    # Unity passband gain: zero-stuffing divides the signal's energy among the
    # inserted zeros, so the filter has to put the factor back.
    kernel = kernel * (up / kernel.sum())
    filtered = np.convolve(stuffed, kernel, mode="same")
    return filtered[::down].astype("float32")


def decode_wav(payload: bytes):
    """WAV bytes -> (float32 mono in [-1, 1], sample rate)."""
    with wave.open(io.BytesIO(payload)) as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if channels > 1:
        frames = frames.reshape(-1, channels).mean(axis=1)
    return frames.astype("float32") / 32768.0, rate


# ----------------------------------------------------------------------
# the speaker
# ----------------------------------------------------------------------

class Speaker:
    """Queued, one-at-a-time speech into the microphone cable.

    A queue rather than fire-and-forget because a second reply arriving mid
    sentence would otherwise cut the first off partway through — the audio
    device simply starts playing the new buffer.
    """

    QUEUE_MAX = 4

    # One attempt, deliberately, and not a knob. Retrying an exclusive open
    # makes things strictly worse here: measured on this machine, a single
    # refused attempt still leaves shared mode working, while eight attempts
    # leave the endpoint unusable by anything for the life of the process —
    # so the fallback fails too and she goes completely silent instead of
    # merely being heard twice on the stream.
    EXCLUSIVE_TRIES = 1

    # How often the writer thread looks up to see whether it has been cut off.
    # Small enough that "shut up" feels immediate, large enough that the write
    # loop is still one buffer copy per block rather than per sample.
    INTERRUPT_POLL_S = 0.05

    def __init__(self, *, loop=None):
        if not AUDIO_AVAILABLE:
            raise RuntimeError("speech needs numpy and sounddevice; neither is here")
        self.loop = loop or asyncio.get_event_loop()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self._task: asyncio.Task | None = None
        self._device: int | None = None
        self._rate: int | None = None
        self._stream = None
        # threading.Event, not asyncio: voice.py reads this from PortAudio's
        # callback thread, which has no event loop.
        self._speaking = threading.Event()
        # Raised by silence() to cut the current line short. The worker thread
        # owns every call on the stream; see _play for why that matters.
        self._interrupt = threading.Event()
        self.stats = {"spoken": 0, "failed": 0, "dropped": 0}
        # Pre-rendered acknowledgements: [(line, stereo float array)], already
        # resampled to the device rate so playing one is a buffer write.
        self._acks: list = []
        self._last_ack = ""

    def is_speaking(self) -> bool:
        """True while audio is on the wire. Read from any thread."""
        return self._speaking.is_set()

    async def start(self) -> None:
        self._device = await asyncio.to_thread(
            find_output_device, config.TTS_OUTPUT_DEVICE
        )
        info = sd.query_devices(self._device)
        self._rate = int(info["default_samplerate"])

        # One long-lived stream, opened here and written to for every line.
        #
        # sd.play() builds a new stream per call, and doing that repeatedly
        # alongside the capture stream failed every time with 'WdmSyncIoctl ...
        # Unanticipated host error' — PortAudio reaching the WDM-KS backend for
        # a device that enumerates as WASAPI, with the index verified correct at
        # the moment of the call. Opening once, while nothing else is starting
        # up, sidesteps whatever that race is, and is what the capture side has
        # always done.
        self._stream = await asyncio.to_thread(self._open_stream)
        log.info("Speaking into [%d] %s @ %dHz", self._device, info["name"], self._rate)
        self._task = self.loop.create_task(self._run())

    def _open_stream(self):
        """Open the long-lived output stream, exclusive first if asked.

        Exclusive mode was an attempt to keep her voice off the Discord
        stream — see config.TTS_EXCLUSIVE. It does not work on this machine
        and is off by default; the code is kept because the reasoning is
        sound and a driver or library update could change the outcome, and
        because rediscovering the dead end costs more than carrying it.

        Whatever the mode, a stream that fails to start is closed before
        anything else is tried. That is the part worth preserving: it is not
        housekeeping. See the comments below.
        """
        if config.TTS_EXCLUSIVE:
            # Retried rather than attempted once, because the usual reason
            # this fails is a restart racing the previous run: Windows takes
            # a moment to release an endpoint after the process holding it
            # dies, and an exclusive open against a not-yet-released device
            # fails as 'Invalid device' rather than anything that names the
            # real cause. Measured on this machine: it fails immediately
            # after a stop and succeeds a few seconds later. Shared mode has
            # no such problem, which is why this only guards the exclusive
            # path.
            last = None
            for attempt in range(1, self.EXCLUSIVE_TRIES + 1):
                # Closed explicitly on failure, and this is not a tidiness
                # detail. A stream can construct successfully and then fail
                # in start(); left unclosed, that half-open stream goes on
                # holding the endpoint for the life of the process, so every
                # later attempt AND the shared fallback below fail against a
                # device the bot is occupying itself. That is exactly how a
                # single refused exclusive open turned into no speech at all.
                stream = None
                try:
                    stream = sd.OutputStream(
                        samplerate=self._rate, channels=2, dtype="float32",
                        device=self._device,
                        extra_settings=sd.WasapiSettings(exclusive=True),
                    )
                    stream.start()
                    log.info("Output stream is WASAPI exclusive%s — the "
                             "device is held until the bot stops",
                             "" if attempt == 1 else f" (took {attempt} tries)")
                    return stream
                except Exception as exc:
                    last = exc
                    _close_quietly(stream)
            log.warning("Exclusive mode refused (%s) — falling back to shared. "
                        "Her voice will still reach the channel, but the "
                        "stream may hear it twice.", last)

        stream = None
        try:
            stream = sd.OutputStream(
                samplerate=self._rate, channels=2, dtype="float32",
                device=self._device,
            )
            stream.start()
            return stream
        except Exception:
            _close_quietly(stream)
            raise

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.debug("Could not close the playback stream", exc_info=True)
            self._stream = None
        self._speaking.clear()
        log.info("Speech stopped — %s", self.stats)

    def silence(self) -> int:
        """Cut her off now and drop anything queued behind it.

        Without this a long reply plays to the end with no way to interrupt it,
        talking over the room. Callable from any thread: the queue drain is a
        non-blocking loop and the stream itself is left to the worker.

        Returns how many queued replies were discarded, so the caller can say
        something truthful about what just happened.
        """
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        # Aborting from here used to race the worker's blocking write and
        # raise PortAudioError -9986 out of the middle of a sentence, killing
        # the line instead of ending it. PortAudio will not have abort() and
        # write() overlap, so the flag goes up and the writer cuts itself off.
        self._interrupt.set()
        if not self._speaking.is_set():
            try:
                # Nobody is writing, so this thread may safely drop whatever
                # the device still holds. stop() would politely play it out,
                # which is the opposite of the ask.
                if self._stream is not None:
                    self._stream.abort()
                    self._stream.start()
            except Exception:
                log.debug("Nothing to stop", exc_info=True)
        self._speaking.clear()
        return dropped

    async def say(self, text: str) -> None:
        """Queue a reply to be spoken. Never raises into the caller."""
        speakable = sanitize_for_speech(text)
        if not speakable:
            return
        try:
            self._queue.put_nowait(speakable)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            log.warning("Speech queue full — dropped %r", speakable[:60])

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._speak_one(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A dead worker means she goes silent forever with nothing in
                # the log to say why.
                self.stats["failed"] += 1
                log.exception("Speaking failed")
            finally:
                self._queue.task_done()

    async def _speak_one(self, text: str) -> None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=config.TTS_TIMEOUT) as client:
                response = await client.post(
                    f"{config.TTS_URL}/synthesize",
                    # instruct rides along on every call for the same reason
                    # voice does: it lets the voice change without reloading a
                    # model. Qwen VoiceDesign takes the description per request,
                    # and every other engine ignores the field.
                    json={"text": text, "voice": config.TTS_VOICE,
                          "instruct": config.TTS_VOICE_DESIGN},
                )
                response.raise_for_status()
                payload = response.content
        except Exception as exc:
            # Not fatal and not worth a traceback: the text reply already went
            # to the channel, so the answer is not lost, only unspoken.
            self.stats["failed"] += 1
            log.warning("TTS service unavailable (%s) — reply not spoken",
                        exc.__class__.__name__)
            return

        mono, rate = await asyncio.to_thread(decode_wav, payload)
        audio = await asyncio.to_thread(resample, mono, rate, self._rate)
        audio = await asyncio.to_thread(normalize, audio)
        stereo = np.column_stack([audio, audio])

        self._interrupt.clear()
        self._speaking.set()
        try:
            done = await asyncio.to_thread(self._play, stereo)
            self.stats["spoken"] += 1
            log.info("%s %.1fs: %r", "spoke" if done else "cut short",
                     len(audio) / self._rate, text[:70])
        finally:
            # Held briefly past the end of playback: the tail is still moving
            # through Discord's encoder and someone else's open mic can echo it
            # back to us for a moment after the device goes quiet.
            await asyncio.sleep(config.TTS_ECHO_GUARD_MS / 1000)
            self._speaking.clear()

    # ---- acknowledgements -------------------------------------------
    #
    # Rendered once, cached on disk, held in memory as the exact float array
    # the output stream wants. An ack must be instant to be worth anything:
    # the whole point is that it lands while the real answer is still being
    # worked out, so it does no HTTP, no decode and no resample at play time.

    async def prepare_acks(self) -> int:
        """Render any missing ack clips and load them all. Returns the count.

        Failure here is never fatal. A missing ack costs a cue, not a reply,
        and the TTS service being slow to come up at boot is ordinary — the
        clips it could not fetch are simply absent and the next start tries
        again.
        """
        import hashlib
        import os

        import httpx

        if not (config.TTS_ACK_ENABLED and config.TTS_ACK_LINES):
            return 0
        os.makedirs(config.TTS_ACK_DIR, exist_ok=True)
        loaded = 0
        for line in config.TTS_ACK_LINES:
            # Keyed by voice as well as text, so changing the voice renders new
            # clips instead of playing the old voice's. TTS_VOICE_DESIGN is in
            # the key for exactly the same reason: under Qwen VoiceDesign it IS
            # the voice, and leaving it out meant redescribing her kept playing
            # the previous description's acks with no way to tell why.
            key = hashlib.sha1(
                f"{config.TTS_VOICE}|{config.TTS_VOICE_DESIGN}|{line}".encode()
            ).hexdigest()[:16]
            path = os.path.join(config.TTS_ACK_DIR, f"{key}.wav")
            payload = None
            if os.path.exists(path):
                payload = await asyncio.to_thread(lambda p=path: open(p, "rb").read())
            else:
                try:
                    async with httpx.AsyncClient(timeout=config.TTS_TIMEOUT) as client:
                        r = await client.post(f"{config.TTS_URL}/synthesize",
                                              json={"text": line,
                                                    "voice": config.TTS_VOICE,
                                                    "instruct": config.TTS_VOICE_DESIGN})
                        r.raise_for_status()
                        payload = r.content
                    await asyncio.to_thread(
                        lambda p=path, d=payload: open(p, "wb").write(d))
                    log.info("Rendered ack %r", line)
                except Exception as exc:
                    log.warning("Could not render ack %r (%s) — skipping",
                                line, exc.__class__.__name__)
                    continue
            try:
                mono, rate = await asyncio.to_thread(decode_wav, payload)
                audio = await asyncio.to_thread(resample, mono, rate, self._rate)
                audio = await asyncio.to_thread(normalize, audio)
                # Some lines are too short to survive the trip. "Mm." rendered
                # as 120ms of nasal hum at -18 dBFS — a tenth of a second with
                # no consonant to define it — and came out of Discord as "m" or
                # "eh". Long enough to look fine in a list of lines, far too
                # short to be heard as a word.
                voiced = _voiced_ms(audio, self._rate)
                if voiced < ACK_MIN_MS:
                    log.warning("Ack %r is only %dms of speech — too short to "
                                "be heard, skipping it", line, voiced)
                    continue
                self._acks.append((line, np.column_stack([audio, audio])))
                loaded += 1
            except Exception:
                log.warning("Ack %r would not decode — skipping", line)
        if loaded:
            log.info("%d acknowledgement clips ready", loaded)
        return loaded

    async def ack(self) -> None:
        """Play a short "heard you", now. Never raises, never queues.

        Deliberately NOT put on the reply queue: it would then land after
        whatever is already speaking, which is precisely backwards — its value
        is entirely in being immediate.

        Skipped while she is already talking. Two voices at once is worse than
        no cue, and if she is mid-sentence the listener plainly knows she heard.
        """
        if not self._acks or self._speaking.is_set():
            return
        # Never the same one twice running: a fixed clip on every command turns
        # into a beep people stop hearing.
        choices = [a for a in self._acks if a[0] != self._last_ack] or self._acks
        line, stereo = random.choice(choices)
        self._last_ack = line
        self._interrupt.clear()
        self._speaking.set()
        try:
            await asyncio.to_thread(self._play, stereo)
        except Exception:
            log.debug("Ack playback failed", exc_info=True)
        finally:
            await asyncio.sleep(config.TTS_ECHO_GUARD_MS / 1000)
            self._speaking.clear()

    def _play(self, stereo) -> bool:
        """Blocking write into the long-lived stream. Runs in a worker thread.

        Written a block at a time rather than in one call so that silence() can
        cut in without touching the stream from another thread: abort() landing
        while this write was in flight raised PortAudioError -9986 and took the
        line with it. The writer therefore owns every stream call, and silence()
        only raises a flag.

        Returns True if the whole line played, False if it was cut short.
        """
        data = np.ascontiguousarray(stereo, dtype="float32")
        block = max(int(self._rate * self.INTERRUPT_POLL_S), 1)
        for start in range(0, len(data), block):
            if self._interrupt.is_set():
                # Drop what the device has already buffered, then leave the
                # stream running so the next line need not reopen it.
                self._stream.abort()
                self._stream.start()
                return False
            self._stream.write(data[start:start + block])
        return True

    def _resolve_device(self) -> int:
        """Confirm the cached index still names the device we resolved.

        PortAudio indices are positional and shift when devices come and go —
        and on this machine they do, because virtual cables and Discord change
        endpoints while running. A stale index does not fail cleanly: it opens
        whatever now sits at that position, which was a WDM-KS entry, and
        surfaces as 'Unanticipated host error ... WdmSyncIoctl' from a call
        that looks correct.
        """
        expected = config.TTS_OUTPUT_DEVICE.lower()
        try:
            current = sd.query_devices(self._device)
            api = sd.query_hostapis()[current["hostapi"]]["name"]
            if (current["name"].lower().startswith(expected)
                    and config.AUDIO_HOST_API in api):
                return self._device
            log.warning("Playback device moved from under index %d (now %r on %s)"
                        " — re-resolving", self._device, current["name"], api)
        except Exception:
            log.warning("Playback device index %d no longer valid — re-resolving",
                        self._device)
        self._device = find_output_device(config.TTS_OUTPUT_DEVICE)
        self._rate = int(sd.query_devices(self._device)["default_samplerate"])
        return self._device



_speaker: Speaker | None = None


async def start() -> Speaker | None:
    """Start speech output, or return None if it is off or unavailable.

    Never raises into startup — a bot that will not boot because a sound device
    moved is worse than one that cannot talk.
    """
    global _speaker
    if not config.TTS_ENABLED:
        return None
    if not AUDIO_AVAILABLE:
        log.warning("TTS_ENABLED is set but numpy/sounddevice are missing here")
        return None
    try:
        speaker = Speaker()
        await speaker.start()
        # After start(), so the clips are resampled to the device's real rate.
        await speaker.prepare_acks()
    except Exception:
        log.exception("Speech failed to start — carrying on without it")
        return None
    _speaker = speaker
    return speaker


async def stop() -> None:
    global _speaker
    if _speaker is not None:
        await _speaker.stop()
        _speaker = None


def is_speaking() -> bool:
    """Module-level view, so voice.py need not hold a reference."""
    return _speaker is not None and _speaker.is_speaking()


def silence() -> str:
    """Stop her talking. Safe to call when she isn't."""
    if _speaker is None:
        return "I wasn't saying anything."
    was_speaking = _speaker.is_speaking()
    dropped = _speaker.silence()
    if not was_speaking and not dropped:
        return "I wasn't saying anything."
    if dropped:
        return f"Fine. Dropped {dropped} more, too."
    return "Fine."


async def say(text: str) -> None:
    if _speaker is not None:
        await _speaker.say(text)


async def ack() -> None:
    """Play the "heard you" cue. Safe to call when speech is off entirely."""
    if _speaker is not None:
        await _speaker.ack()
