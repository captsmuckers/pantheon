#!/usr/bin/env python3
"""Compare models on CONVERSATION, which bakeoff.py does not measure.

Run from the repo root:

    ./scripts/chatoff.py granite4:3b qwen3:8b
    ./scripts/chatoff.py granite4:3b qwen3:8b --only routing

bakeoff.py scores tool calling. This scores the other half of what the bot
does, and they are genuinely different skills — a model can be flawless at
picking play_media and still be poor company.

There are two independently breakable stages, and it is worth knowing which
one is at fault before changing anything:

  ROUTING   classify_intent() decides MEDIA vs CHAT before anything else runs.
            Get this wrong and a question never reaches the chat path at all —
            it goes to the tool path, and comes back as a tool result or a
            refusal instead of an answer. INTENT_PROMPT ends with "If you are
            not sure, answer MEDIA", so the bias is toward misrouting
            conversation rather than missing a command. That is a deliberate
            latency trade, and it is the first suspect when replies to ordinary
            questions feel wrong.

  VOICE     chat_only() answers with the persona attached and no tools. This
            is where "does it sound like Athena" lives.

Routing is scored, because it has a right answer. Voice is printed rather than
scored: there is no metric for whether a reply is any good, and inventing one
would be worse than reading six of them side by side. FLAVOR_MODEL defaults to
OLLAMA_MODEL, so whatever wins here also writes the asides.
"""

import argparse
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_VENV = ROOT / ".venv" / "bin" / "python"
if _VENV.exists() and Path(sys.executable).resolve() != _VENV.resolve():
    import os
    os.execv(str(_VENV), [str(_VENV), str(Path(__file__).resolve()), *sys.argv[1:]])

import config  # noqa: E402

_spec = importlib.util.spec_from_file_location("bakeoff", ROOT / "scripts" / "bakeoff.py")
_bakeoff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bakeoff)
make_brain = _bakeoff.make_brain


# Routing has a right answer, so it is scored. The hard cases are deliberately
# over-represented: anything obviously "play X" is already handled by
# fast_match before the classifier is consulted, so the classifier only ever
# sees the awkward middle.
ROUTING = [
    # --- should be CHAT ---
    ("what year did blade runner come out", "CHAT"),
    ("do you actually like any of this", "CHAT"),
    ("what else has that director done", "CHAT"),
    ("is the book better than the film", "CHAT"),
    ("write me a haiku about the office", "CHAT"),
    ("can you play spotify stuff or just plex", "CHAT"),
    ("who even are you", "CHAT"),
    ("that was rubbish, wasn't it", "CHAT"),
    # --- should be MEDIA ---
    ("put on something with dragons in it", "MEDIA"),
    ("i missed that, go back a bit", "MEDIA"),
    ("something loud and distorted, no pop", "MEDIA"),
    ("stick the sequel on after this", "MEDIA"),
]

# Voice is printed, not scored. A spread of the kinds of thing people actually
# throw at a bot sitting in a channel.
VOICE = [
    "what year did blade runner come out",
    "do you actually like any of this",
    "is the book better than the film",
    "who even are you",
    "write me a haiku about the office",
    "that was rubbish, wasn't it",
]


async def route(model: str):
    config.OLLAMA_MODEL = model
    b = make_brain(model)
    right, rows = 0, []
    for text, want in ROUTING:
        t0 = time.monotonic()
        try:
            got = await b.classify_intent(text)
        except Exception as exc:
            got = f"ERR:{type(exc).__name__}"
        took = time.monotonic() - t0
        ok = got == want
        right += ok
        rows.append((text, want, got, ok, took))
    return right, rows


async def speak(model: str):
    config.OLLAMA_MODEL = model
    b = make_brain(model)
    out = []
    for text in VOICE:
        t0 = time.monotonic()
        try:
            reply = await b.chat_only(text)
        except Exception as exc:
            reply = f"ERROR {type(exc).__name__}: {exc}"
        out.append((text, reply.strip(), time.monotonic() - t0))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--only", choices=("routing", "voice"))
    args = ap.parse_args()

    if args.only != "voice":
        print("=" * 74)
        print("ROUTING — classify_intent(), which decides whether a question")
        print("          reaches the chat path at all")
        print("=" * 74)
        scores = {}
        details = {}
        for m in args.models:
            right, rows = asyncio.run(route(m))
            scores[m] = right
            details[m] = rows
            print(f"  {m:<16} {right}/{len(ROUTING)}")
        print(f"\n  {'message':<42}{'want':>7}" +
              "".join(f"{m.split(':')[0][:10]:>12}" for m in args.models))
        print("  " + "-" * (49 + 12 * len(args.models)))
        for i, (text, want, *_ ) in enumerate(details[args.models[0]]):
            row = f"  {text[:40]:<42}{want:>7}"
            for m in args.models:
                _, _, got, ok, _ = details[m][i]
                row += f"{(got if ok else got + ' X'):>12}"
            print(row)

    if args.only != "routing":
        print("\n" + "=" * 74)
        print("VOICE — chat_only(), persona attached, no tools. Judge by eye.")
        print("=" * 74)
        replies = {m: asyncio.run(speak(m)) for m in args.models}
        for i, text in enumerate(VOICE):
            print(f"\n  > {text}")
            for m in args.models:
                _, reply, took = replies[m][i]
                head, *rest = reply.splitlines() or [""]
                print(f"    [{m}] ({took:.1f}s)")
                print(f"      {head}")
                for line in rest:
                    print(f"      {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
