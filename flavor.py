"""One short aside, appended to a result that already happened.

Deliberately additive. The factual line comes from the player and is never
rewritten, so a model that misreads the situation can only be dull — it cannot
report an outcome that didn't occur. That failure mode (turning "couldn't find
it" into something that reads like success) is what the tool-calling path
spends most of its effort preventing, and letting a narrator rewrite results
would hand the risk straight back.

Two things learned from measuring a 7B model on this:

1. Personas that list example lines get parroted. "Example reactions: ..." in
   BOT_PERSONA produced "Ugh, fine." on six responses out of six, so the
   examples are stripped before the persona reaches the narrator.
2. Negative constraints don't work. Telling it "do not open with X" made
   variety *worse* — 3 distinct openings out of 8 versus 4 out of 6 without.
3. The constraints were the problem, not the model. A tight word cap plus a
   rotated "angle" gave 7 distinct openings in 8; simply loosening the brief
   and raising the temperature gave 8 in 8, with far more character. The
   angles were compensating for a cage of my own building, so they're gone.

What is NOT loosened is the split: the model reacts, it never reports. Letting
it write the whole reply was measured too, and a 7B dropped the actual fact in
two of five cases — "music wonderwall" came back as "Spotify's got the
nostalgia cranked up" without ever naming the song — and invented a "Zarpa the
Conqueror" to suggest. The factual line stays machine-generated.
"""

import asyncio
import logging
import random
import re

import config

log = logging.getLogger("athena.flavor")

# Actions worth a remark. Everything else — volume, seeking, subtitle and track
# fiddling, status queries — stays terse: a bot that comments on every single
# thing isn't deadpan, it's exhausting.
FLAVOURED_ACTIONS = {
    "play", "queue", "skip", "stop", "unpark",
    "music", "music_resume", "music_stop", "radio", "youtube", "stream",
}

# ...and the tool-calling equivalents, for results shipped verbatim from tier 2.
FLAVOURED_TOOLS = {
    "play_media", "queue_media", "music", "music_radio", "music_play_uri",
}

# How many recent exchanges the narrator is shown. Enough to notice a repeat
# request or a run of one artist, which is where the genuinely good lines come
# from — and cheap, since these are already in hand.
CONTEXT_TURNS = 3

_STRIP_EXAMPLES = re.compile(r"example reactions?:.*", re.I | re.S)


def _int_env(name: str, default: int) -> int:
    """A small integer from the environment. Local so flavor keeps its own
    settings rather than reaching into config for a single number."""
    import os
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# The shortest complete sentence worth keeping in place of an ellipsis.
MIN_SENTENCE = 25

# How long an aside may be before it gets trimmed.
#
# History worth keeping, because this has moved twice in opposite directions.
# It was 140, which "cut good lines off mid-thought", so it went to 220 to
# leave room for two sentences. Both of those were sized for the old footnote
# style, where the aside sat alone on its own italic line and length cost
# nothing but screen space.
#
# Inline changes that. A remark that runs on after the factual line is part of
# the same breath, and two sentences there overstay — "Deftones again. The kind
# of noise that makes you wonder if the speaker is broken" lands softer than
# its first half alone. 120 is roughly one sentence.
#
# The mid-thought truncation that made 140 unusable is fixed below rather than
# tolerated: trimming now prefers a sentence boundary over an ellipsis far more
# readily, so a shorter ceiling clips cleanly instead of trailing off.
MAX_LENGTH = _int_env("FLAVOR_MAX_LENGTH", 120)


def _persona(flourish: bool = False) -> str:
    """The persona minus any example lines — see the module docstring.

    BOT_PERSONA_FLOURISH is folded in only when `flourish` is set. Asking a 7B
    to reference something "occasionally, not every line" gets it into 5 lines
    out of 8; deciding here how often to even mention it is the only reliable
    way to make it occasional.
    """
    text = _STRIP_EXAMPLES.sub("", config.BOT_PERSONA or "").strip()
    extra = _STRIP_EXAMPLES.sub("", config.BOT_PERSONA_FLOURISH or "").strip()
    if flourish and extra:
        return f"{text} {extra}".strip()
    return text


def persona_text(flourish: bool = False) -> str:
    """The cleaned persona, for callers outside this module — see _persona."""
    return _persona(flourish)


# Small quantized models occasionally emit a stray token from another script
# mid-sentence ("huh?静"), which reads as a bug rather than a joke. Assumes an
# English persona; widen this if BOT_PERSONA is written in another language.
_KEEP_NON_ASCII = "—–…‘’“”"


# Small models sometimes answer as a transcript rather than as themselves:
# they prefix their own name as a speaker label, or carry on past their turn
# and write the user's next line too. Ollama's chat endpoint uses proper
# message roles, so this is the model free-running rather than a prompt-format
# problem — stop sequences catch most of it and this catches the rest.
#
# Naming the bot in the system prompt makes it likelier, because the name is
# sitting right there to be used as a label. This appeared on the rename.
_SPEAKER_LABEL = re.compile(r"^\s*(?:athena|assistant|bot|ai)\s*:\s*", re.I)
# Line-anchored, and only the words that actually start a new turn. Matching a
# bare "you:" anywhere would eat the tail of "I told you: don't".
_NEXT_TURN = re.compile(r"(?:^|\n)\s*(?:user|human)\s*:", re.I)


def strip_transcript(text: str) -> str:
    """Drop a self-applied speaker label and anything after a new turn starts."""
    text = (text or "").strip()
    for _ in range(3):  # "Athena: Athena: ..." does happen
        stripped = _SPEAKER_LABEL.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    cut = _NEXT_TURN.search(text)
    if cut:
        text = text[: cut.start()].strip()
    return text


# Assistant sign-offs. Every one of these was produced by qwen3:8b in the live
# channel, tacked onto the end of an otherwise in-character reply:
#
#   "The Bears. They're the only team that hasn't lost to a team that isn't
#    the Chiefs.  \nYou're welcome. I'm here to help."
#
# It is chat-assistant training showing through the persona, and no amount of
# "you are aloof and deadpan" in the prompt reliably suppresses it — the prompt
# is asked nicely, this is not. Stripped after the fact rather than argued with.
#
# Deliberately anchored to the END and to whole sentences. A reply that happens
# to contain "I'm here" mid-thought is left alone; only a trailing pleasantry
# is removed.
_SIGN_OFFS = re.compile(
    r"(?:^|\s)(?:"
    # Both the contracted and spelled-out forms: a model writes "You're
    # welcome" and "You are welcome" interchangeably, and matching only the
    # apostrophe version left half of them behind.
    r"(?:you(?:'|’)re|you\s+are)\s+welcome|"
    r"(?:i(?:'|’)m|i\s+am)\s+(?:happy|glad)\s+to\s+help|"
    r"(?:i(?:'|’)m|i\s+am)\s+here\s+to\s+(?:help|assist)(?:\s+you)?|"
    r"let\s+me\s+know\s+if\s+(?:you\s+need|there(?:'|’)s)[^.!?]*|"
    r"happy\s+to\s+help|"
    r"(?:is\s+there\s+)?anything\s+else[^.!?]*|"
    r"how\s+can\s+i\s+(?:help|assist)[^.!?]*"
    r")\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def strip_sign_offs(text: str) -> str:
    """Remove trailing customer-service pleasantries. Never empties the reply.

    Applied repeatedly because they arrive in pairs — "You're welcome. I'm here
    to help." is two of them — and bailing out if the result would be empty,
    since a reply that is ONLY a pleasantry is at least still an answer of
    sorts, and returning "" would ship silence.
    """
    out = (text or "").strip()
    for _ in range(4):
        trimmed = _SIGN_OFFS.sub("", out).strip()
        if trimmed == out or not trimmed:
            break
        out = trimmed
    return out or (text or "").strip()


def _clean(text: str, limit: int = MAX_LENGTH) -> str:
    """A tidy remark, or nothing."""
    line = strip_transcript(text).strip('"').strip("*_").strip()
    # Paragraph breaks become spaces rather than truncation points: a two
    # sentence reaction split over two lines is still one reaction.
    line = " ".join(part.strip() for part in line.splitlines() if part.strip())
    # Asterisks only ever arrive as stage directions ("...go heavy. *sigh"),
    # and the whole remark is already italicised when it's attached.
    line = line.replace("*", "").strip()
    line = "".join(c for c in line if c.isascii() or c in _KEEP_NON_ASCII).strip()
    if len(line) > limit:
        # Cut at the last sentence end that fits, so it never trails off mid-word.
        clipped = line[:limit]
        stop = max(clipped.rfind("."), clipped.rfind("?"), clipped.rfind("!"))
        # A complete short remark beats a truncated long one, so almost any
        # sentence boundary is preferred to an ellipsis. This threshold used to
        # be limit // 2, which was fine at 220 and actively harmful at 120: an
        # aside whose first sentence was short would fail the test and get
        # ellipsised mid-word instead of simply ending after that sentence,
        # which is exactly the "cut off mid-thought" complaint that pushed the
        # limit up in the first place.
        if stop >= MIN_SENTENCE:
            line = clipped[: stop + 1]
        else:
            # Leave room for the ellipsis inside the limit, not past it.
            line = line[: limit - 3].rstrip() + "..."
    return line


class Narrator:
    """Writes the aside. Never decides or reports what happened."""

    def __init__(self, backend: str):
        self.backend = backend
        self._last_opening = ""
        self._recent: list[tuple[str, str]] = []
        self.enabled = bool(config.FLAVOR_ENABLED and _persona() and backend)
        if self.enabled:
            log.info("Flavour text enabled via %s", backend)

    def wants(self, action: str | None = None, tool: str | None = None) -> bool:
        if not self.enabled:
            return False
        if action is not None:
            return action in FLAVOURED_ACTIONS
        return tool in FLAVOURED_TOOLS

    def _system(self, flourish: bool = False) -> str:
        return (
            "You are the voice of a media bot in a Discord channel. It has "
            "ALREADY performed an action and already told everyone so in the "
            "line above yours. You add your reaction underneath.\n\n"
            f"Voice: {_persona(flourish)}\n\n"
            "Say what you actually think about this one — the artist, the "
            "title, the mood of it, being asked at all, whatever strikes you. "
            "One or two sentences. Starting lowercase is fine.\n\n"
            "Only three hard rules:\n"
            "- Don't restate what happened. The line above already did that, "
            "and repeating it just sounds like a robot.\n"
            "- Never name a different title, artist or alternative, and never "
            "suggest what they should have asked for instead. You cannot see "
            "the library, so anything you name would be invented.\n"
            "- No exclamation points, no emoji, no quotation marks."
        )

    async def aside(self, request: str, result: str) -> str | None:
        """A remark to append, or None. Never raises."""
        if not self.enabled or not result:
            return None

        # Roll for the flourish here rather than asking the model to use it
        # "occasionally" — see _persona().
        flourish = random.random() < config.FLAVOR_FLOURISH_CHANCE

        # Recent context is what lets it notice "that again?" or a run of the
        # same artist, which is where the lines with any wit come from.
        history = ""
        if self._recent:
            history = "Earlier in this session:\n" + "\n".join(
                f"- they asked {r!r}, and: {o}" for r, o in self._recent
            ) + "\n\n"
        prompt = (
            f"{history}"
            f"Right now they asked: {request}\n"
            f"What happened: {result}\n\n"
            f"Your reaction:"
        )
        try:
            line = await asyncio.wait_for(
                self._generate(prompt, flourish), timeout=config.FLAVOR_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.debug("Flavour timed out after %ss", config.FLAVOR_TIMEOUT)
            return None
        except Exception:
            log.debug("Flavour generation failed", exc_info=True)
            return None

        line = _clean(line)
        if not line:
            return None

        # Repetition is worse than silence, and silence suits the voice.
        opening = line.split()[0].lower().strip(",.;:")
        if opening and opening == self._last_opening:
            log.debug("Dropped a flavour line repeating the last opening: %r", line)
            return None
        self._last_opening = opening
        self._recent.append((request, result))
        del self._recent[:-CONTEXT_TURNS]
        return line

    def _rephrase_system(self, flourish: bool = False) -> str:
        return (
            "You are the voice of a media bot in a Discord channel. Someone "
            "asked it a question and it has drafted a flat, colourless answer. "
            "Say the same thing the way you would say it.\n\n"
            f"Voice: {_persona(flourish)}\n\n"
            "Only four hard rules:\n"
            "- Keep every claim exactly as it is: same capabilities, same yes "
            "or no, same names. You are changing how it sounds, never what it "
            "says.\n"
            "- Add nothing. No new offers, no titles, no suggestions, no "
            "questions the draft didn't ask.\n"
            "- Same length or shorter. One or two sentences.\n"
            "- No exclamation points, no emoji, no quotation marks."
        )

    async def rephrase(self, request: str, draft: str) -> str | None:
        """The draft answer in voice, or None to keep the draft as it is.

        This exists so the tool-calling prompt can stay voice-neutral. Persona
        pressure in that prompt measurably traded against tool discipline —
        stating the character ahead of the rules reintroduced fabricated
        actions, which is the one failure the whole design exists to prevent.
        Here there are no tools attached at all, so the worst case is a dull
        line rather than a claim that something happened when it didn't.

        Only conversation reaches this. Anything with an action behind it keeps
        its machine-generated factual line and gets aside() instead.
        """
        if not self.enabled or not draft or not draft.strip():
            return None

        flourish = random.random() < config.FLAVOR_FLOURISH_CHANCE
        prompt = (f"They asked: {request}\n"
                  f"The flat draft answer: {draft}\n\n"
                  f"Same answer, your voice:")
        try:
            # Cooler and shorter than an aside. At the aside's 1.05 this padded
            # rather than restated — it kept the draft verbatim and bolted
            # commentary on the end, inventing content to fill the space.
            line = await asyncio.wait_for(
                self._generate(prompt, flourish,
                               system=self._rephrase_system(flourish),
                               temperature=0.8, num_predict=90),
                timeout=config.FLAVOR_TIMEOUT,
            )
        # Falling back is logged at INFO, not DEBUG: a flat reply in the
        # channel is otherwise indistinguishable from the voice simply not
        # being applied, and diagnosing "How are you doing Athena?" shipping
        # colourless took a separate experiment to work out which of these it
        # had been.
        except asyncio.TimeoutError:
            log.info("Rephrase timed out after %ss — shipping the flat draft",
                     config.FLAVOR_TIMEOUT)
            return None
        except Exception:
            log.info("Rephrase failed — shipping the flat draft", exc_info=True)
            return None

        draft = draft.strip()
        # Room to restate with character, not room to write an essay. Trimming
        # a padded reply mid-sentence looked worse than keeping the flat draft,
        # so over-long rewrites are rejected below rather than clipped here.
        # The floor matters as much as the ratio: a 40-character draft scaled
        # by 1.6 leaves no room for any character at all, and every voiced
        # rewrite of a short answer got rejected. In practice the model keeps
        # the draft and appends a dry remark, which is the additive shape this
        # module argues for anyway — the factual half is never rewritten.
        # In practice the model keeps the draft and appends a remark rather
        # than restating, which lands close to the old 220 ceiling and got
        # clipped — a 56-character draft produced 202 and 203 character
        # replies, both legitimate, one of five rejected outright. Sized for
        # the shape it actually produces.
        ceiling = max(int(len(draft) * 2.5) + 120, 320)
        line = _clean(line, limit=ceiling)
        if not line:
            return None
        # A rewrite far shorter than the draft has dropped something rather
        # than tightened it. Style is not worth losing half the answer.
        if len(line) < len(draft) * 0.4:
            log.info("Rephrase dropped too much, keeping the draft: %r", line)
            return None
        # ...and one much longer has padded rather than restated, which is how
        # invented content ("a few exceptions that might surprise you") got in.
        if len(line) > ceiling - 10:
            log.info("Rephrase padded rather than restated, keeping the draft: %r", line)
            return None
        return line

    async def _generate(self, prompt: str, flourish: bool = False,
                        system: str | None = None,
                        temperature: float = 1.05, num_predict: int = 110) -> str:
        if self.backend != "ollama":
            return ""
        import httpx

        async with httpx.AsyncClient(timeout=config.FLAVOR_TIMEOUT) as client:
            response = await client.post(
                f"{config.OLLAMA_HOST}/api/chat",
                json={
                    "model": config.FLAVOR_MODEL,
                    "stream": False,
                    # See config.OLLAMA_THINK. Flavour is one throwaway line;
                    # a thinking preamble would be longer than the output.
                    **({} if config.OLLAMA_THINK is None
                       else {"think": config.OLLAMA_THINK}),
                    "keep_alive": config.OLLAMA_KEEP_ALIVE,
                    "options": {
                        **({} if config.FLAVOR_NUM_GPU is None
                           else {"num_gpu": config.FLAVOR_NUM_GPU}),
                        "temperature": temperature,
                        "top_p": 0.95,
                        "num_predict": num_predict,
                    },
                    "messages": [
                        {"role": "system", "content": system or self._system(flourish)},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            return (response.json().get("message") or {}).get("content", "")


def _sentence_case(text: str) -> str:
    """Capitalise the start of every sentence, and nothing else.

    The model writes asides lowercase far more often than not. On its own
    italic line that read as a stylistic choice; run on after a full stop it
    just reads as broken — and a two-sentence aside was broken twice, since
    capitalising only the first character left the second sentence lowercase
    mid-line.

    Deliberately only touches characters that follow sentence-ending
    punctuation. Title-casing or .capitalize() would flatten the rest of the
    string, and asides routinely carry titles and band names that must keep
    the case the model gave them.
    """
    out = []
    capitalise = True
    for ch in text:
        if capitalise and ch.isalpha():
            out.append(ch.upper())
            capitalise = False
        else:
            out.append(ch)
            if ch in ".!?":
                capitalise = True
    return "".join(out)


def attach(result, line: str | None, style: str | None = None):
    """Join an aside to a plain-text result, leaving it alone otherwise.

    Pickers and other rich results pass straight through — the aside would
    interrupt a question the user still has to answer.

    Two styles, because the original reads as annotation rather than speech:

      inline  "Stopped and cleared the queue. What a waste of time."
      line    "Stopped and cleared the queue.\n_what a waste of time._"

    inline is the default. The second line always looked bolted on, because
    it was — a separate italic sentence under every reply reads as a footnote
    from a different voice, not as the same person still talking.

    What is deliberately NOT done here: asking the model to write the whole
    reply. The factual half stays exactly as the code generated it, and this
    only ever concatenates. That is the line between an aside and a
    fabrication — a model given the chance to restate what happened is a
    model that can claim something happened that did not, which is the one
    failure this whole design exists to prevent. Joining strings cannot.

    A multi-line result keeps the old style regardless: a queue listing with a
    remark run onto the end of its last row is worse than a footnote.
    """
    if not line or not isinstance(result, str) or not result.strip():
        return result
    if (style or config.FLAVOR_STYLE) != "inline" or "\n" in result.strip():
        return f"{result}\n_{line}_"

    body = result.rstrip()
    if body[-1] not in ".!?:":
        body += "."
    aside = _sentence_case(line.strip())
    if aside and aside[-1] not in ".!?":
        aside += "."
    return f"{body} {aside}"
