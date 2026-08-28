"""Voice command routing: wake matching, noise rejection, segmentation.

Everything here is a pure function or a plain object fed synthetic buffers —
no audio hardware, no GPU, no Discord. The wake and noise tests deliberately
avoid numpy so they still run on production's interpreter, which has neither
numpy nor sounddevice installed; the segmenter tests skip there instead of
failing.

The wake cases are transcripts that actually came out of Whisper during live
testing, not invented strings. That matters: nearly every wrong assumption in
this module was corrected by a real transcript rather than by reasoning.
"""

import sys
from pathlib import Path

# Windows consoles default to cp1252, and the hallucination list contains the
# musical note Whisper emits for background music.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voice  # noqa: E402

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")
        failures.append(label)


PATTERN = voice.build_wake_pattern(
    voice.DEFAULT_WAKE_WORDS, voice.DEFAULT_WAKE_PREFIXES,
    require_prefix=False, compounds=voice.DEFAULT_WAKE_COMPOUNDS,
)


# ----------------------------------------------------------------------
# wake matching
# ----------------------------------------------------------------------

def _check_wake_accepts():
    print("\nthe wake word is accepted and stripped")
    # All real transcripts from live sessions.
    for said, want in [
        ("Athena, skip the song.", "skip the song."),
        ("Athena, play Ratatouille.", "play Ratatouille."),
        ("Athena, cue up toxicity.", "cue up toxicity."),
        ("athena play ratatouille", "play ratatouille"),
        ("ATHENA SKIP", "SKIP"),
        # Spelling variants the model produces for the same word.
        ("athina what's playing", "what's playing"),
        ("atena pause", "pause"),
    ]:
        check(f"{said!r}", voice.strip_wake(said, PATTERN), want)

    print("\na prefix is accepted but not required")
    # Required under the old one-syllable name; optional now, because demanding
    # a short unstressed word in front reintroduces the clipping problem.
    check("'hey athena skip'", voice.strip_wake("hey athena skip", PATTERN), "skip")
    check("'okay athena skip'", voice.strip_wake("okay athena skip", PATTERN), "skip")
    check("bare name works", voice.strip_wake("athena skip", PATTERN), "skip")


def _check_wake_rejects():
    print("\nordinary conversation never wakes it")
    # The reason a wake word exists at all: people say these to each other
    # constantly while the bot is listening. All observed in live transcripts.
    for said in [
        "skip this part",
        "let's play that one",
        "pause it for a sec",
        "next song please",
        "skip to the next one",
        "put on some music",
        "and let's get this on.",
        # Near-misses the model produced while testing the name itself. None
        # may wake it, or the wake word is worthless.
        "Latina.",
        "Patina.",
        "at Eno.",
        "that Dina played toxicity.",
    ]:
        check(f"{said!r}", voice.strip_wake(said, PATTERN), None)


def _check_anchoring():
    print("\nthe wake word may follow a sentence boundary, not only start one")
    # Real transcripts: clear commands that a start-anchored pattern discarded
    # because the speaker did not pause a full second first.
    check("after a full stop",
          voice.strip_wake("That's a good one. Athena, what's the score?", PATTERN),
          "what's the score?")
    check("after a question mark",
          voice.strip_wake("Is it on? Athena, skip the song", PATTERN),
          "skip the song")

    print("\nafter a comma, and mid-sentence when a command follows")
    # Real transcript: this was discarded because the wake word sat after a
    # comma rather than a full stop, so a genuine request was silently lost.
    check("'Let's see, Athena, play Wonderwall.'",
          voice.strip_wake("Let's see, Athena, play Wonderwall.", PATTERN),
          "play Wonderwall.")
    # No punctuation at all in front of it — rescued because the remainder
    # opens with a verb that can only start an instruction.
    check("'okay so anyway Athena play wonderwall'",
          voice.strip_wake("okay so anyway Athena play wonderwall", PATTERN),
          "play wonderwall")
    check("'hold on Athena pause'",
          voice.strip_wake("hold on Athena pause", PATTERN), "pause")

    print("\nconversational openers are rescued mid-sentence too")
    # "Athena, tell me a joke" was dropped when it landed mid-conversation,
    # because "tell" was not a recognised opener and only clause boundaries
    # rescued a mid-sentence name.
    check("'so anyway Athena tell me a joke'",
          voice.strip_wake("so anyway Athena tell me a joke", PATTERN),
          "tell me a joke")
    check("'right so Athena say something mean'",
          voice.strip_wake("right so Athena say something mean", PATTERN),
          "say something mean")
    # ...but linking verbs are NOT openers. Adding "is"/"was"/"does" turned
    # every remark about her into a command: "I think Athena is annoying" fired.
    check("'I think Athena is annoying'",
          voice.strip_wake("I think Athena is annoying", PATTERN), None)
    check("'we all know Athena was right'",
          voice.strip_wake("we all know Athena was right", PATTERN), None)

    print("\nthe name opening an utterance always wakes, whatever follows")
    # Accepted, not overlooked. Someone who starts a sentence with her name is
    # addressing her; requiring a known verb here would drop short commands
    # that no list anticipates. The cost is that "Athena was right about that"
    # spoken in isolation reaches the model and gets a conversational answer.
    check("'Athena was right about that'",
          voice.strip_wake("Athena was right about that", PATTERN),
          "was right about that")

    print("\n...but mid-sentence mentions still do not wake it")
    # The case anchoring exists for, which must survive the relaxation above.
    # Each remainder opens with something that is not a command verb.
    for said in [
        "I was telling Athena to skip stuff yesterday",
        "so I asked athena about it and nothing happened",
        "we were just chatting and athena came up",
        "that Athena thing is cool",
        "I think Athena is annoying",
        "the athena update broke it",
    ]:
        check(f"{said!r}"[:52], voice.strip_wake(said, PATTERN), None)


def _check_woken_but_empty():
    print("\nthe name alone yields an empty command, not a miss")
    # Distinct from None: the caller reports "woken but nothing said" rather
    # than silently ignoring it. This is the commonest live case while testing.
    check("'Athena.'", voice.strip_wake("Athena.", PATTERN), "")
    check("'athena'", voice.strip_wake("athena", PATTERN), "")


def _check_compounds_are_optional():
    print("\ncompounds are empty by default and the matcher still works")
    # They existed to prop up a name the model misheard. Removing them must not
    # break anything, and an empty list must not match everything.
    bare = voice.build_wake_pattern(voice.DEFAULT_WAKE_WORDS,
                                    voice.DEFAULT_WAKE_PREFIXES,
                                    require_prefix=False, compounds="")
    check("still matches the name", voice.strip_wake("athena skip", bare), "skip")
    check("empty list matches nothing extra",
          voice.strip_wake("headaches, what's on?", bare), None)

    print("\na compound can still be added back, including multi-word ones")
    tuned = voice.build_wake_pattern(voice.DEFAULT_WAKE_WORDS,
                                     voice.DEFAULT_WAKE_PREFIXES,
                                     require_prefix=False,
                                     compounds="that dina, uh thena")
    check("'that Dina played toxicity.'",
          voice.strip_wake("that Dina played toxicity.", tuned), "played toxicity.")
    check("punctuation inside a compound",
          voice.strip_wake("That, Dina. skip", tuned), "skip")


# ----------------------------------------------------------------------
# noise rejection
# ----------------------------------------------------------------------

def _check_noise():
    print("\nWhisper's silence hallucinations are rejected")
    # Every one of these was emitted from silence during live testing.
    for text in ["", "  ", "you", "Thank you.", "thanks for watching",
                 "Bye.", "♪", "[music]", "Hmm"]:
        check(f"{text!r}", voice.is_hallucination(text), True)

    print("\nreal speech is not a hallucination")
    for text in ["skip the song", "play ratatouille", "what's playing",
                 "when I went to your thing in your house and I was like dude"]:
        check(f"{text!r}"[:46], voice.is_hallucination(text), False)

    print("\nthe length cap applies to woken commands only")
    check("20 words is too long", voice.too_long(" ".join(["w"] * 20), 14), True)
    check("14 words is at the cap", voice.too_long(" ".join(["w"] * 14), 14), False)
    # Regression: the cap used to run BEFORE the wake gate, which threw away
    # legitimate long requests that had properly woken the bot.
    spoken = "athena play the one where the guy goes to space and meets his dad"
    command = voice.strip_wake(spoken, PATTERN)
    check("long woken request survives the wake gate",
          command, "play the one where the guy goes to space and meets his dad")
    check("...and is under the cap once the name is gone",
          voice.too_long(command, 14), False)


# ----------------------------------------------------------------------
# segmentation
# ----------------------------------------------------------------------

def _check_segmenter():
    print("\nsegmentation")
    if not voice.AUDIO_AVAILABLE:
        print("  skip (numpy/sounddevice not installed on this interpreter)")
        return
    import numpy as np

    rate = voice.CAPTURE_RATE
    block = voice.BLOCK

    def seg():
        return voice.Segmenter(threshold=0.01, silence_hold_s=0.2, preroll_s=0.1,
                               min_s=0.1, max_s=2.0, rate=rate)

    loud = np.full((block, 2), 0.5, dtype="float32")
    quiet = np.zeros((block, 2), dtype="float32")

    s = seg()
    out = [u for _ in range(20) for u in s.feed(quiet)]
    check("silence yields nothing", out, [])

    s = seg()
    done = []
    for _ in range(10):
        done += s.feed(loud)
    check("still open while loud", done, [])
    for _ in range(10):
        done += s.feed(quiet)
    check("one utterance after the hold", len(done), 1)

    # Pre-roll means the utterance starts BEFORE the threshold was crossed, so
    # the first syllable is not clipped. Discord's own gate does this to us
    # upstream, which is why the wake word had to start on a vowel — this is
    # the part of the problem we can actually control.
    s = seg()
    for _ in range(5):
        s.feed(quiet)
    done = []
    for _ in range(10):
        done += s.feed(loud)
    for _ in range(10):
        done += s.feed(quiet)
    check("utterance includes pre-roll", len(done[0]) > 10 * block, True)

    s = voice.Segmenter(threshold=0.01, silence_hold_s=0.1, preroll_s=0.0,
                        min_s=1.0, max_s=2.0, rate=rate)
    done = s.feed(loud)
    for _ in range(6):
        done += s.feed(quiet)
    check("sub-minimum blip dropped", done, [])

    s = voice.Segmenter(threshold=0.01, silence_hold_s=5.0, preroll_s=0.0,
                        min_s=0.1, max_s=0.5, rate=rate)
    done = []
    for _ in range(40):
        done += s.feed(loud)
    check("capped at max_s", len(done) >= 1, True)
    if done:
        check("cap respected", len(done[0]) <= int(0.5 * rate) + block, True)


def _check_resample():
    print("\nresampling")
    if not voice.AUDIO_AVAILABLE:
        print("  skip (numpy not installed on this interpreter)")
        return
    import numpy as np

    stereo = np.zeros((4800, 2), dtype="float32")
    out = voice.to_whisper_audio(stereo)
    check("48k stereo -> 16k mono length", out.shape, (1600,))
    check("is 1-D", out.ndim, 1)

    lr = np.zeros((30, 2), dtype="float32")
    lr[:, 0] = 1.0
    check("downmix averages", float(voice.to_whisper_audio(lr)[0]), 0.5)


for fn in (_check_wake_accepts, _check_wake_rejects, _check_anchoring,
           _check_woken_but_empty, _check_compounds_are_optional, _check_noise,
           _check_segmenter, _check_resample):
    fn()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("voice checks passed")
