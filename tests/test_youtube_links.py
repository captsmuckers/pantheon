"""A YouTube link with a verb in front of it is still a link.

"play https://www.youtube.com/watch?v=..." reached the model, which called
play_media with the URL as a search query and reported it missing from the Plex
library. youtube.is_url matches the WHOLE message, so a bare link worked and a
link with any word before it did not — the same anchoring gap streams.find_url
was added to close for Twitch and Kick.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import youtube  # noqa: E402
from brain import fast_match  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_links_with_words_around_them():
    print("a link reaches YouTube however it is wrapped:")
    for text, want in (
        ("play https://www.youtube.com/watch?v=qOqYIVKfV-U",
         "https://www.youtube.com/watch?v=qOqYIVKfV-U"),
        ("watch this https://youtu.be/abc123def45 please", "https://youtu.be/abc123def45"),
        ("queue https://www.youtube.com/watch?v=abc123def45",
         "https://www.youtube.com/watch?v=abc123def45"),
        ("https://youtu.be/abc123def45, thanks", "https://youtu.be/abc123def45"),
        ("put on https://www.youtube.com/shorts/abc123def45",
         "https://www.youtube.com/shorts/abc123def45"),
    ):
        hit = fast_match(text)
        ok = hit and hit[0] == "youtube" and hit[1]["query"] == want
        check(f"{text[:44]!r}", ok, str(hit))


def test_bare_link_unchanged():
    print("\na bare link behaves exactly as before:")
    hit = fast_match("https://youtu.be/abc123def45")
    check("routes to youtube",
          hit and hit[0] == "youtube" and hit[1]["query"] == "https://youtu.be/abc123def45",
          str(hit))


def test_other_platforms_not_stolen():
    print("\nother platforms are not swallowed:")
    for text, expected in (("https://kick.com/someone", "stream"),
                           ("https://www.twitch.tv/someone", "stream"),
                           ("stream paymoneywubby on kick", "stream")):
        hit = fast_match(text)
        check(f"{text[:40]!r} -> {expected}", hit and hit[0] == expected, str(hit))


def test_non_links_untouched():
    print("\nnothing else is treated as a link:")
    for text in ("play nacho libre", "youtube big buck bunny",
                 "what's playing right now", "talk about youtube videos"):
        found = youtube.find_url(text)
        check(f"{text[:40]!r} has no link", found is None, str(found))


def main():
    test_links_with_words_around_them()
    test_bare_link_unchanged()
    test_other_platforms_not_stolen()
    test_non_links_untouched()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
