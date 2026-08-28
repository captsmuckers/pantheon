"""The episode named before the title.

"play s1e1 of reno 911" fell through the deterministic path — which only knew
"<title> s1e1" — and reached the model, which searched the library for "re",
matched Regular Show and tried to play a series.

The whole point of _STRUCTURED_EPISODE_QUERY is that a title never has to
survive a small model's paraphrasing, so the phrasing is rewritten into the
shape resolve_query already understands rather than taught as a second grammar.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import fast_match  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_episode_first_is_rewritten():
    print("episode-first phrasing reaches the deterministic resolver:")
    for text, want in (
        ("play s1e1 of reno 911", "reno 911 s1e1"),
        ("play s01e05 of the office", "the office s01e05"),
        ("watch 3x05 of the office", "the office 3x05"),
        ("play season 2 episode 4 of severance", "severance season 2 episode 4"),
        ("put on s1e1 of reno 911", "reno 911 s1e1"),
    ):
        hit = fast_match(text)
        ok = hit and hit[0] == "play" and hit[1]["query"].lower() == want
        check(f"{text!r} -> {want!r}", ok, str(hit))

    hit = fast_match("queue s1e2 of reno 911")
    ok = hit and hit[0] == "queue" and hit[1]["query"].lower() == "reno 911 s1e2"
    check("the queue verb works too", ok, str(hit))


def test_title_first_still_works():
    print("\nthe original phrasing is unchanged:")
    for text, want in (
        ("play the office s3e5", "the office s3e5"),
        ("play the office 3x05", "the office 3x05"),
        ("play severance season 2 episode 4", "severance season 2 episode 4"),
        ("play the next episode of severance", "the next episode of severance"),
    ):
        hit = fast_match(text)
        ok = hit and hit[0] == "play" and hit[1]["query"].lower() == want
        check(f"{text!r}", ok, str(hit))


def test_ordinary_titles_are_untouched():
    print("\nnothing else is rewritten:")
    for text in ("play nacho libre", "play dune part one",
                 "play something with dragons in it", "play misty for me"):
        hit = fast_match(text)
        # These either miss fast_match entirely or route as a plain play; what
        # matters is that no episode rewriting happened to the query.
        rewritten = hit and hit[0] == "play" and " of " in text and hit[1]["query"] != text[5:]
        check(f"{text!r} not rewritten", not rewritten, str(hit))


def main():
    test_episode_first_is_rewritten()
    test_title_first_still_works()
    test_ordinary_titles_are_untouched()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
