"""Speech output: making a reply speakable, and not hearing yourself say it.

Pure functions only — no audio device, no GPU, no TTS service. The sanitiser
runs anywhere; the resampler needs numpy and skips on production's interpreter.
"""

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import speech  # noqa: E402
import voice  # noqa: E402

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")
        failures.append(label)


def _check_sanitize():
    print("\nmarkdown is stripped rather than read aloud")
    # Every one of these is a shape Athena's replies actually take.
    for raw, want in [
        ("Playing **The Land Before Time (1988)**",
         "Playing The Land Before Time (1988)"),
        ("_clearing. done._", "clearing. done."),
        ("Stopped and cleared the queue.\n_not my taste._",
         "Stopped and cleared the queue. not my taste."),
        ("`skip the song`", "skip the song"),
        ("Use ```python\nprint(1)\n``` for that", "Use for that"),
    ]:
        check(f"{raw[:38]!r}", speech.sanitize_for_speech(raw), want)

    print("\nurls and emoji are dropped, links keep their label")
    check("bare url",
          speech.sanitize_for_speech("See https://example.com/x for more"),
          "See for more")
    check("markdown link",
          speech.sanitize_for_speech("[Ratatouille](https://plex/x) is queued"),
          "Ratatouille is queued")
    check("emoji removed",
          speech.sanitize_for_speech("\N{STUDIO MICROPHONE} skip the song"),
          "skip the song")

    print("\nlong replies are cut at a sentence boundary")
    long_text = ("First sentence here. Second sentence here. "
                 "Third one runs on and on and on.")
    out = speech.sanitize_for_speech(long_text, max_chars=45)
    check("ends on a full stop", out.endswith("."), True)
    check("did not run over", len(out) <= 45, True)
    check("kept whole sentences", out, "First sentence here. Second sentence here.")

    print("\nnothing to say yields nothing")
    for empty in ["", "   ", "**", "`` ``"]:
        check(f"{empty!r}", speech.sanitize_for_speech(empty), "")


def _check_suppression():
    print("\ncapture is dropped while she is speaking")
    if not voice.AUDIO_AVAILABLE:
        print("  skip (numpy not installed on this interpreter)")
        return
    import numpy as np

    rate = voice.CAPTURE_RATE
    block = voice.BLOCK
    loud = np.full((block, 2), 0.5, dtype="float32")
    quiet = np.zeros((block, 2), dtype="float32")

    # A part-captured utterance must be abandoned, not spliced across the gap:
    # otherwise audio from before she spoke joins audio from after.
    seg = voice.Segmenter(threshold=0.01, silence_hold_s=0.2, preroll_s=0.1,
                          min_s=0.1, max_s=2.0, rate=rate)
    for _ in range(5):
        seg.feed(loud)
    check("mid-utterance before reset", seg.speaking, True)
    seg.reset()
    check("abandoned after reset", seg.speaking, False)
    done = []
    for _ in range(6):
        done += seg.feed(quiet)
    check("nothing emitted from the fragment", done, [])

    # ...and it still works normally afterwards.
    for _ in range(5):
        done += seg.feed(loud)
    for _ in range(6):
        done += seg.feed(quiet)
    check("captures again after the gap", len(done), 1)


def _check_resample():
    print("\nresampling for the cable")
    if not speech.AUDIO_AVAILABLE:
        print("  skip (numpy not installed on this interpreter)")
        return
    import numpy as np

    # WASAPI shared mode refuses a mismatched rate outright rather than
    # converting, and Kokoro emits 24kHz into a 48kHz cable.
    mono = np.linspace(-1, 1, 2400, dtype="float32")
    out = speech.resample(mono, 24000, 48000)
    check("24k -> 48k doubles the length", len(out), 4800)
    same = speech.resample(mono, 48000, 48000)
    check("same rate is a passthrough", len(same), 2400)

    # The property that actually matters, and the one linear interpolation
    # failed. A 24kHz source has nothing above 12kHz, so any energy up there
    # after upsampling was invented by the resampler. Linear interpolation
    # left it 27dB below the signal, which is audible as a gritty edge.
    t = np.arange(24000, dtype="float64") / 24000.0
    tone = np.sin(2 * np.pi * 3000 * t).astype("float32")   # a clean 3kHz tone
    up = np.asarray(speech.resample(tone, 24000, 48000), dtype="float64")
    spec = np.abs(np.fft.rfft(up * np.hanning(len(up))))
    freqs = np.fft.rfftfreq(len(up), 1 / 48000)
    wanted = float((spec[(freqs > 20) & (freqs < 12000)] ** 2).sum())
    images = float((spec[freqs >= 12500] ** 2).sum())
    db = 10 * np.log10(images / wanted)
    check("images are pushed below -40dB", db < -40, True)
    if db >= -40:
        print(f"         imaging measured at {db:.1f} dB")

    print("\nlevelling cannot clip, whatever it is given")
    quiet = (np.sin(np.linspace(0, 400, 8000)) * 0.02).astype("float32")
    loud = (np.sin(np.linspace(0, 400, 8000)) * 0.99).astype("float32")
    for name, sig in (("a quiet line", quiet), ("an already-hot line", loud)):
        out = speech.normalize(sig)
        check(f"{name} stays under full scale",
              bool(np.abs(out).max() <= 1.0), True)
    check("a quiet line is brought up",
          float(np.abs(speech.normalize(quiet)).max()) > float(np.abs(quiet).max()),
          True)
    check("silence is left alone",
          float(np.abs(speech.normalize(np.zeros(1000, dtype="float32"))).max()),
          0.0)


class _FakeStream:
    """Records what was asked of it, in order. Nothing reaches a device."""

    def __init__(self):
        self.written = 0
        self.calls = []

    def write(self, data):
        self.calls.append("write")
        self.written += len(data)

    def abort(self):
        self.calls.append("abort")

    def start(self):
        self.calls.append("start")


def _bare_speaker():
    """A Speaker with no event loop, no device and no TTS service behind it."""
    import asyncio
    import threading

    sp = speech.Speaker.__new__(speech.Speaker)
    sp._stream = _FakeStream()
    sp._rate = 48000
    sp._interrupt = threading.Event()
    sp._speaking = threading.Event()
    sp._queue = asyncio.Queue()
    return sp


def _check_interrupt():
    print("\ncutting a line short without racing the writer")
    if not speech.AUDIO_AVAILABLE:
        print("  skip (numpy not installed on this interpreter)")
        return
    import numpy as np

    one_second = np.zeros((48000, 2), dtype="float32")

    # A line nobody interrupts must play whole and leave the stream alone.
    sp = _bare_speaker()
    check("an uninterrupted line reports done", sp._play(one_second), True)
    check("every frame is written", sp._stream.written, 48000)
    check("a finished line is never aborted", "abort" in sp._stream.calls, False)

    # Flag already up: not a single frame should reach the device.
    sp = _bare_speaker()
    sp._interrupt.set()
    check("an interrupted line reports cut short", sp._play(one_second), False)
    check("no audio is written after the cut", sp._stream.written, 0)
    check("the buffered tail is dropped, then the stream is usable again",
          sp._stream.calls, ["abort", "start"])

    # Flag raised mid-line: it must stop partway, not play to the end.
    sp = _bare_speaker()
    stream = sp._stream
    real_write = stream.write

    def cut_after_three(data):
        real_write(data)
        if stream.calls.count("write") >= 3:
            sp._interrupt.set()

    stream.write = cut_after_three
    check("a line cut partway reports cut short", sp._play(one_second), False)
    check("it stops well short of the whole line",
          stream.written < 48000 and stream.written > 0, True)
    check("and still leaves the stream started", stream.calls[-2:],
          ["abort", "start"])

    # The regression that started this: abort() from the caller's thread while
    # the worker was inside a blocking write raised PortAudioError -9986 and
    # killed the line instead of ending it.
    sp = _bare_speaker()
    sp._speaking.set()
    sp.silence()
    check("silence() never touches the stream while a writer owns it",
          sp._stream.calls, [])
    check("it raises the flag instead", sp._interrupt.is_set(), True)

    # With nobody writing there is no race, so it may abort directly.
    sp = _bare_speaker()
    sp._speaking.clear()
    sp.silence()
    check("with no writer it drops the buffer itself",
          sp._stream.calls, ["abort", "start"])


for fn in (_check_sanitize, _check_suppression, _check_resample,
           _check_interrupt):
    fn()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("speech checks passed")
