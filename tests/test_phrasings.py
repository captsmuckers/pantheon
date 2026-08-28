import sys
from pathlib import Path

# Relative, not hardcoded — see the note in test_event_loop.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import fast_match


def _check_chat_routing():
    """Conversation must not reach the tool path.

    "tell me a poem" was a request for Athena to write one. It reached the model
    with the tool schema attached and came back as a Spotify search for a track
    called "The Road Not Taken", then a music_play_uri call with no uri.
    """
    failures = []
    for text in ("tell me a poem", "tell me a joke", "write me a haiku about mondays",
                 "tell me a story", "say something mean", "give me a limerick",
                 "what do you think about pop music", "how do you feel about mondays"):
        hit = fast_match(text)
        if not (hit and hit[0] == "chat"):
            failures.append(f"{text!r} should be chat, got {hit}")

    # Media, and self-description, must be untouched by that pattern.
    for text in ("tell me about yourself", "play nacho libre", "what's playing right now",
                 "music radiohead", "queue up some EDM music", "play a movie about robots",
                 "tell me what's in the queue", "stream someone on kick"):
        hit = fast_match(text)
        if hit and hit[0] == "chat":
            failures.append(f"{text!r} should NOT be chat, got {hit}")

    print("\nconversational requests skip the tool path:")
    for f in failures:
        print("  FAIL " + f)
    if not failures:
        print("  ok   all 16 phrasings routed correctly")
    return not failures

SHOULD_RESUME_MUSIC = [
    "go back to the music",        # the one that failed
    "back to the music",
    "go back to music",
    "switch back to the music",
    "swap back to the music",
    "return to the music",
    "resume the music",
    "resume music",
    "play the music",
    "unpause the music",
    "music on",
    "spotify resume",
    "go back to spotify",
]

SHOULD_UNPARK = [
    "go back to the movie",
    "back to the movie",
    "go back to the video",
    "resume the video",
    "return to the film",
    "swap back to the show",
]

MUST_NOT_MATCH_EITHER = [
    "play the music video from 1999",   # prefix trap
    "play music box",
    "music radiohead",
    "play the office",
    "back to the future",               # a real film title!
    "go back 30 seconds",
    "play back to the future",
]

fails = []
print("resume-music phrasings:")
for p in SHOULD_RESUME_MUSIC:
    hit = fast_match(p)
    ok = hit and hit[0] == "music_resume"
    print(f"  {'ok  ' if ok else 'FAIL'} {p!r:<34} -> {hit}")
    if not ok:
        fails.append(p)

print("\nresume-video phrasings:")
for p in SHOULD_UNPARK:
    hit = fast_match(p)
    ok = hit and hit[0] == "unpark"
    print(f"  {'ok  ' if ok else 'FAIL'} {p!r:<34} -> {hit}")
    if not ok:
        fails.append(p)

print("\nmust NOT be hijacked as a resume:")
for p in MUST_NOT_MATCH_EITHER:
    hit = fast_match(p)
    bad = hit and hit[0] in ("music_resume", "unpark")
    print(f"  {'FAIL' if bad else 'ok  '} {p!r:<34} -> {hit}")
    if bad:
        fails.append(p)

if not _check_chat_routing():
    fails.append("chat routing")

print(f"\n{'FAILURES: ' + str(fails) if fails else 'all phrasings behave'}")
sys.exit(1 if fails else 0)
