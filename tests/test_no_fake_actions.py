"""A claimed action with no tool call behind it must never reach the user.

Every string in FABRICATED was produced by a live 7B model and shipped to real
people as if it had happened. Nothing was played or queued.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain as brain_mod  # noqa: E402
from brain import _LOOKS_LIKE_FAKE_ACTION, NO_ACTION_TAKEN  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


FABRICATED = [
    '.Charting "Friday" by Vanessa Black... queued.',
    '.Playing "The Beautiful People" by Charles Manson.',
    '.Playing "Friday" by Vanessa Black.',
    'Queuing "Friday"... already playing, so queuing instead.',
    "Queueing that up now.",
    "Checking library... no info without playing it.",
    "*Cueing up the track*",
    "- spinning that up now",
    "Now playing: something you asked for",
    "Added it to the queue.",
    "  ...searching for that one",
]

LEGITIMATE = [
    "Nothing is playing right now.",
    "The queue is empty.",
    "I only control Plex and Spotify, sorry.",
    "Which one did you mean?",
    "Dune came out in 1984.",
    "That's a film, not a song.",
    "Couldn't find anything matching that.",
    "Up next: three tracks.",
    "There are 11,263 films in the library.",
]


def test_detector():
    print("fabrications produced by a real model tonight:")
    missed = [f for f in FABRICATED if not _LOOKS_LIKE_FAKE_ACTION.match(f)]
    for f in FABRICATED:
        check(f"{f[:52]!r}", bool(_LOOKS_LIKE_FAKE_ACTION.match(f)))
    check("none missed", not missed, str(missed))

    print("\nlegitimate answers must pass through:")
    for g in LEGITIMATE:
        check(f"{g[:52]!r}", not _LOOKS_LIKE_FAKE_ACTION.match(g))


class FakeControls:
    """Records whether anything was actually dispatched."""

    def __init__(self):
        self.dispatched = []

    def state(self):
        return {"playing": False}

    async def dispatch(self, name, args):
        self.dispatched.append(name)
        return "ok"


def make_brain(replies):
    """A Brain whose model returns a scripted sequence of assistant messages."""
    b = brain_mod.Brain.__new__(brain_mod.Brain)
    b.controls = FakeControls()
    b.history = []
    b._lock = asyncio.Lock()
    b._verbatim_tool = None
    b.backend = "ollama"
    b.narrator = type("N", (), {"wants": lambda *a, **k: False})()
    b._replies = list(replies)
    return b


def run_ollama(b, monkeypatched_post):
    import types
    # Replace the HTTP round trip with the scripted replies.
    async def fake_ask(self, text):
        return await brain_mod.Brain._ask_ollama(self, text)
    return fake_ask


def test_unbacked_claim_is_replaced():
    """End-to-end through _ask_ollama with a model that only ever narrates."""
    print("\na model that narrates twice gets its claim withheld:")

    import httpx

    calls = {"n": 0}

    class FakeResponse:
        status_code = 200

        def __init__(self, content):
            self._content = content

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": self._content, "tool_calls": []}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls["n"] += 1
            # Narrates every time, even after the nudge.
            return FakeResponse('.Charting "Friday" by Vanessa Black... queued.')

    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        b = make_brain([])
        reply = asyncio.run(b._ask_ollama("play friday by vanessa black"))
    finally:
        httpx.AsyncClient = original

    check("it was nudged and asked twice", calls["n"] == 2, f"{calls['n']} calls")
    check("no tool ran", b.controls.dispatched == [], str(b.controls.dispatched))
    check("the fabrication was withheld", reply == NO_ACTION_TAKEN, reply)
    check("reply says nothing happened", "nothing was played" in reply.lower(), reply)


# Bracketed pseudo-calls, all seen from a live 7B. Unrecovered, each of these
# shipped raw JSON to the channel and performed no action at all.
BRACKETED = [
    ('[music] {"query": "edm"}', "music", {"query": "edm"}),
    ('[music] {"query": "haywyre insight"}', "music", {"query": "haywyre insight"}),
    ('[music] {"query": "blood moon reign"}', "music", {"query": "blood moon reign"}),
    ('[music_control] {"action": "pause"}', "music_control", {"action": "pause"}),
    ('Let me check... [music_control] {"action": "pause"} Queuing next:',
     "music_control", {"action": "pause"}),
    ('[get_status]', "get_status", {}),
]

NOT_CALLS = [
    '[not_a_tool] {"x": 1}',
    'see [note] {something}',
    'Nothing is playing right now.',
    'The queue is empty.',
]


def test_bracketed_pseudo_calls():
    print("\nbracketed tool calls written as text are recovered:")
    for text, name, args in BRACKETED:
        got = brain_mod._parse_pseudo_tool_call(text)
        check(f"{text[:44]!r}", got == (name, args), str(got))

    print("\n...but unknown names and prose are not dispatched:")
    for text in NOT_CALLS:
        got = brain_mod._parse_pseudo_tool_call(text)
        check(f"{text[:44]!r}", got is None, str(got))


def test_raw_syntax_is_never_shown():
    print("\nunrecovered tool syntax counts as machinery, not an answer:")
    for text in ['[not_a_tool] {"x": 1}', '{"query": "edm"}',
                 'here you go: {"action": "pause"}']:
        check(f"{text[:44]!r} flagged",
              bool(brain_mod._RAW_TOOL_SYNTAX.search(text)))
    for text in ['Nothing is playing right now.', 'The queue is empty.',
                 'Dune came out in 1984.']:
        check(f"{text[:44]!r} not flagged",
              not brain_mod._RAW_TOOL_SYNTAX.search(text))


def test_stray_punctuation_stays_on_the_fast_path():
    print("\nstray quotes must not cost a model round trip:")
    for text, action in (("pause'", "pause"), ('"skip"', "skip"),
                         ("`stop`", "stop"), ("resume ", "resume")):
        hit = brain_mod.fast_match(text)
        check(f"{text!r} -> {action}", hit is not None and hit[0] == action, str(hit))


def main():
    test_detector()
    test_unbacked_claim_is_replaced()
    test_bracketed_pseudo_calls()
    test_raw_syntax_is_never_shown()
    test_stray_punctuation_stays_on_the_fast_path()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)




def test_state_change_claims_are_caught():
    """Announcing a switch was flipped, without flipping it.

    "turn on karaoke mode" missed the fast path, reached the model, and came
    back as "Karaoke mode activated. The volume is dialed precisely..." — with
    no tool call behind it. Every verb in the guard named something that moves
    media; nothing named something being switched on or off, and the noun-first
    phrasing put the object in front of the verb where an anchored pattern
    could never see it.
    """
    from brain import _LOOKS_LIKE_FAKE_ACTION as guard

    for claim in [
        "Karaoke mode activated. The volume is dialed precisely",
        "Karaoke is now on.",
        "Lyrics enabled.",
        "Turned on karaoke mode.",
        "Subtitles are now off",
        "Enabled karaoke for you",
        "Toggled karaoke mode",
    ]:
        check(f"caught: {claim[:38]!r}", bool(guard.match(claim)))

    # The guard only runs when no tool was called, but it must still not swallow
    # an honest answer — those are the replies worth keeping.
    for honest in [
        "I have no idea what that is.",
        "That film is not in the library.",
        "Nothing is playing right now.",
        "There are three things called Dune.",
        "The library has 13266 titles.",
    ]:
        check(f"allowed: {honest[:38]!r}", not guard.match(honest))


test_state_change_claims_are_caught()

main()
