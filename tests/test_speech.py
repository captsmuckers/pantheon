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
    check("endpoints preserved", (round(float(out[0]), 3),
                                  round(float(out[-1]), 3)), (-1.0, 1.0))
    same = speech.resample(mono, 48000, 48000)
    check("same rate is a passthrough", len(same), 2400)


for fn in (_check_sanitize, _check_suppression, _check_resample):
    fn()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("speech checks passed")
