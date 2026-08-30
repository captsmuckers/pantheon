"""Flavour text: additive only, selective, and never load-bearing.

No model is called here — the point is the plumbing around it, which is where
a bug would actually hurt.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import flavor  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


class StubNarrator(flavor.Narrator):
    """A Narrator whose model returns whatever we hand it."""

    def __init__(self, reply="", fail=False, delay=0.0):
        super().__init__("ollama")
        self.enabled = True
        self._reply, self._fail, self._delay = reply, fail, delay
        self.calls = 0

    async def _generate(self, prompt, flourish=False):
        self.calls += 1
        self.last_flourish = flourish
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("model exploded")
        return self._reply


def test_attach_is_additive():
    """The invariant, in both styles: the factual half is never rewritten.

    That is the line between an aside and a fabrication. attach() may only
    concatenate — the moment it can alter what the code said happened, a
    remark can contradict it.
    """
    print("the factual line is never rewritten:")
    fact = "Couldn't find anything matching “zorp”."

    inline = flavor.attach(fact, "the void stares back", "inline")
    check("inline: factual half verbatim", inline.startswith(fact), inline)
    check("inline: aside runs on, sentence-cased",
          inline == f"{fact} The void stares back.", inline)

    lined = flavor.attach(fact, "the void stares back", "line")
    check("line: factual half verbatim", lined.startswith(fact), lined)
    check("line: aside italicised underneath",
          lined.endswith("_the void stares back_"), lined)

    check("no aside leaves it untouched", flavor.attach(fact, None) == fact)
    check("empty aside leaves it untouched", flavor.attach(fact, "") == fact)

    # A remark run onto the end of a queue listing's last row is worse than a
    # footnote, so multi-line results keep the old style whatever is asked for.
    listing = "Up next:\n- Dune\n- Heat"
    multi = flavor.attach(listing, "no surprises there", "inline")
    check("multi-line result falls back to the footnote",
          multi == f"{listing}\n_no surprises there_", multi)


def test_sentence_case():
    print("\nasides arrive lowercase and must not read as fragments:")
    cases = [
        ("what a waste. another mindless command",
         "What a waste. Another mindless command"),
        ("deftones again", "Deftones again"),
        ("", ""),
        # Only characters after sentence-enders move: titles and names keep
        # whatever case the model gave them.
        ("more REM, then. still not pop",
         "More REM, then. Still not pop"),
    ]
    for raw, want in cases:
        got = flavor._sentence_case(raw)
        check(f"{raw[:34]!r}", got == want, got)


def test_pickers_pass_through():
    print("\nrich results are never decorated:")

    class Picker:
        def __str__(self):
            return "which one?"

    p = Picker()
    check("picker object returned as-is", flavor.attach(p, "an aside") is p)
    check("empty result untouched", flavor.attach("", "an aside") == "")


def test_selectivity():
    print("\nonly worthwhile actions get a line:")
    n = StubNarrator("something")
    for action in ("play", "skip", "stop", "music", "unpark", "radio"):
        check(f"{action} is flavoured", n.wants(action=action))
    for action in ("volume", "seek", "status", "help", "subs_lang",
                   "tracks_list", "audio_lang", "speed", "libraries",
                   "queue_list", "pause", "resume"):
        check(f"{action} stays silent", not n.wants(action=action))
    check("authoritative tools flavoured", n.wants(tool="play_media"))
    check("get_status tool not flavoured", not n.wants(tool="get_status"))


def test_disabled_paths():
    print("\ndisabled or misconfigured is silent, not broken:")
    n = StubNarrator("something")
    n.enabled = False
    check("wants() false when disabled", not n.wants(action="play"))
    check("aside() returns None when disabled",
          asyncio.run(n.aside("play x", "Playing X")) is None)
    check("no model call was made", n.calls == 0)


def test_failures_are_swallowed():
    print("\na broken or slow model must not break the reply:")
    n = StubNarrator(fail=True)
    check("model error -> None", asyncio.run(n.aside("play x", "Playing X")) is None)

    slow = StubNarrator("too late", delay=config.FLAVOR_TIMEOUT + 0.5)
    import time
    t0 = time.monotonic()
    out = asyncio.run(slow.aside("play x", "Playing X"))
    elapsed = time.monotonic() - t0
    check("timeout -> None", out is None)
    check("gave up near the timeout, didn't hang",
          elapsed < config.FLAVOR_TIMEOUT + 1.0, f"{elapsed:.1f}s")

    empty = StubNarrator("   ")
    check("blank generation -> None", asyncio.run(empty.aside("play x", "Playing X")) is None)


def test_cleanup():
    print("\noutput is tidied:")
    n = StubNarrator('"quoted line"')
    check("surrounding quotes stripped",
          asyncio.run(n.aside("x", "y")) == "quoted line")

    n = StubNarrator("first line\nsecond line")
    check("wrapped lines joined, not truncated",
          asyncio.run(n.aside("x", "y")) == "first line second line")

    n = StubNarrator("go heavy. *sigh*")
    check("stage directions stripped",
          asyncio.run(n.aside("x", "y")) == "go heavy. sigh")

    n = StubNarrator("stray token huh?静")
    check("foreign-script artifact removed",
          asyncio.run(n.aside("x", "y")) == "stray token huh?")

    n = StubNarrator("an em—dash and an ellipsis…")
    check("typographic punctuation survives",
          asyncio.run(n.aside("x", "y")) == "an em—dash and an ellipsis…")

    n = StubNarrator("x" * 400)
    out = asyncio.run(n.aside("x", "y"))
    check("over-long line truncated", len(out) <= flavor.MAX_LENGTH, f"{len(out)} chars")

    long_sentences = ("A first sentence that runs on a while. " * 8)
    n = StubNarrator(long_sentences)
    out = asyncio.run(n.aside("x", "y"))
    check("cut at a sentence boundary",
          len(out) <= flavor.MAX_LENGTH and out.endswith("."), f"{len(out)}: ...{out[-40:]}")


def test_repeat_suppression():
    print("\nrepetition is dropped rather than sent:")
    n = StubNarrator("finally, something watchable")
    first = asyncio.run(n.aside("x", "y"))
    check("first line sent", first is not None, str(first))
    second = asyncio.run(n.aside("x", "y"))
    check("identical opening suppressed", second is None, str(second))

    n._reply = "silence, then"
    third = asyncio.run(n.aside("x", "y"))
    check("a different opening is allowed", third is not None, str(third))


def test_flourish_is_rationed_by_code():
    """"Occasionally reference X" is not a frequency a 7B can honour — with the
    goth imagery inside BOT_PERSONA it appeared in 5 lines out of 8. The
    proportion is decided here instead."""
    print("\nthe optional flourish is mixed in by the code, not the model:")
    persona, extra = config.BOT_PERSONA, config.BOT_PERSONA_FLOURISH
    chance = config.FLAVOR_FLOURISH_CHANCE
    config.BOT_PERSONA = "Dry and tired."
    config.BOT_PERSONA_FLOURISH = "Be gothic about it."
    try:
        check("absent by default", "gothic" not in flavor._persona(False),
              flavor._persona(False))
        check("present when asked", "gothic" in flavor._persona(True),
              flavor._persona(True))

        config.FLAVOR_FLOURISH_CHANCE = 0.0
        n = StubNarrator("a line")
        asyncio.run(n.aside("x", "y"))
        check("chance 0 never rolls it", n.last_flourish is False)

        config.FLAVOR_FLOURISH_CHANCE = 1.0
        n = StubNarrator("another line")
        asyncio.run(n.aside("x", "y"))
        check("chance 1 always rolls it", n.last_flourish is True)

        # No flourish configured -> the flag changes nothing.
        config.BOT_PERSONA_FLOURISH = ""
        check("empty flourish is a no-op",
              flavor._persona(True) == flavor._persona(False))
    finally:
        config.BOT_PERSONA = persona
        config.BOT_PERSONA_FLOURISH = extra
        config.FLAVOR_FLOURISH_CHANCE = chance


def test_persona_examples_stripped():
    print("\npersona example lines are kept out of the prompt:")
    original = config.BOT_PERSONA
    config.BOT_PERSONA = (
        'Dry and tired. Example reactions: "Ugh, fine." / "This again."'
    )
    try:
        text = flavor._persona()
        check("examples removed", "Ugh, fine" not in text, text)
        check("voice description kept", "Dry and tired" in text, text)
    finally:
        config.BOT_PERSONA = original


def test_transcript_labels_stripped():
    """Small models answer as a transcript instead of as themselves.

    Observed live right after the rename: replies arriving as "Athena: ..."
    and, worse, continuing past their own turn to write the user's next line.
    Naming the bot in the system prompt gives the model a label to reach for.
    """
    for raw, want in [
        ("Athena: Obviously.", "Obviously."),
        ("athena: Already done.", "Already done."),
        ("Athena: Athena: Fine.", "Fine."),
        ("Assistant: Done.", "Done."),
        # Runs past its turn and writes both sides.
        ("Obviously.\nUser: and the other one?\nAthena: That too.", "Obviously."),
        ("Fine. Not my taste.\n\nUser: thanks", "Fine. Not my taste."),
        # Untouched: a plain reply, and a colon that is not a speaker label.
        ("Already playing it.", "Already playing it."),
        ("I told you: do not ask twice.", "I told you: do not ask twice."),
    ]:
        check(f"strip {raw[:34]!r}", flavor.strip_transcript(raw) == want,
              flavor.strip_transcript(raw))

    # _clean flattens newlines to spaces, so without stripping first a runaway
    # transcript becomes one run-on line rather than being caught.
    cleaned = flavor._clean("Athena: Fine.\nUser: really?")
    check("_clean drops the transcript too", cleaned == "Fine.", cleaned)


def test_repetition_loops_are_cut():
    """The failure that had to be interrupted by hand.

    "tell me a joke" came back as the same bartender exchange six times over,
    594 characters, done_reason 'length' — 33 seconds of speech. Sampling
    penalties gave nothing to tune against (12 samples over four settings
    produced no loop at all), so the guard runs after the fact instead.

    The first version of this missed it entirely: it split sentences on a
    lookbehind for [.!?], and the looping text reads
        ... your kind here." The man says ...
    where the character before the space is the quote, not the stop. The whole
    reply came back as one sentence and nothing was detected. That is what this
    first case is really testing.
    """
    print("\nrepetition loops are cut back to the first pass")
    looped = (
        'A man walks into a bar. The bartender says, "We don\u2019t serve your '
        'kind here." The man says, "Then I\u2019ll have a beer." The bartender '
        'says, "We don\u2019t serve your kind here." The man says, "Then '
        'I\u2019ll have a beer." The bartender says, "We don\u2019t serve your '
        'kind here." The man says, "Then I\u2019ll have a beer." The bartender says'
    )
    out = flavor.strip_repetition(looped)
    check("the loop is cut", len(out) < len(looped) / 2,
          f"{len(looped)} -> {len(out)} chars")
    check("the joke itself survives", out.startswith("A man walks into a bar."))
    check("it is told exactly once",
          out.count("Then I\u2019ll have a beer") == 1)

    print("\n  replies that merely repeat a short phrase are left alone")
    for text in (
        "I don't have time for jokes. If you want one, go write it yourself.",
        "Ray. He's not here. You're not. I'm not. The lights are off.",
        "No. No. Absolutely not.",
        'She said, "come here." Then she left. Nobody followed.',
        "",
    ):
        check(f"unchanged: {text[:34]!r}",
              flavor.strip_repetition(text) == text.strip())


def main():
    test_transcript_labels_stripped()
    test_attach_is_additive()
    test_sentence_case()
    test_pickers_pass_through()
    test_selectivity()
    test_disabled_paths()
    test_failures_are_swallowed()
    test_cleanup()
    test_repeat_suppression()
    test_flourish_is_rationed_by_code()
    test_persona_examples_stripped()
    test_repetition_loops_are_cut()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
