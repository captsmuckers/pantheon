"""Twitch and Kick: routing, live-only resolution, and live playback rules.

The live rules are the point of this file. A live stream has no position, so
seeking must be refused rather than attempted, the freeze watchdog must rejoin
at the live edge instead of a stale offset, and end-of-stream must go idle
rather than advancing the queue into something nobody asked for.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeItem, FakeLib, make_player  # noqa: E402

import config  # noqa: E402
import streams  # noqa: E402
from brain import Controls, fast_match  # noqa: E402
from library import TitleEntry  # noqa: E402

PASS = []


class SearchableLib(FakeLib):
    """FakeLib plus the title-search surface _library_claims needs."""

    def __init__(self, items=None, titles=()):
        super().__init__(items)
        self._entries = [
            TitleEntry(rating_key=k, title=t, kind=kind, year=y, library="Movies")
            for k, t, kind, y in titles
        ]

    def scored_search(self, query, kind=None, limit=25, library=None, year=None):
        q = (query or "").strip().lower()
        out = []
        for e in self._entries:
            t = e.title.lower()
            score = 1.0 if t == q else (0.95 if t.startswith(q) else (0.85 if q in t else 0.0))
            if score:
                out.append((score, e))
        out.sort(key=lambda p: -p[0])
        return out[:limit]

    def resolve_query(self, query):
        hits = self.scored_search(query)
        if not hits:
            return None, []
        return self.items[hits[0][1].rating_key], []

    def up_next(self, show):
        return None


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def live(title="Some Streamer", source="twitch"):
    return streams.StreamItem(f"https://{source}.tv/x", title, None, "", True, source)


def test_url_detection():
    print("recognising live links:")
    for url, want in (
        ("https://www.twitch.tv/someone", "twitch"),
        ("https://twitch.tv/someone", "twitch"),
        ("https://m.twitch.tv/someone", "twitch"),
        ("https://www.twitch.tv/videos/123456789", "twitch"),
        ("https://www.twitch.tv/someone/clip/abc-def", "twitch"),
        ("https://kick.com/someone", "kick"),
        ("https://www.kick.com/someone", "kick"),
        ("https://kick.com/someone/videos/abc-123", "kick"),
    ):
        check(f"{url[:44]!r} -> {want}", streams.source_of(url) == want,
              str(streams.source_of(url)))
    for url in ("https://youtube.com/watch?v=abc123def45", "https://example.com/twitch.tv",
                "twitch.tv", "just some text", "https://kick.com/"):
        check(f"not a live link: {url[:38]!r}", not streams.is_url(url))


def test_channel_urls():
    print("\nbare channel names become URLs:")
    check("twitch", streams.channel_url("twitch", "someone") == "https://www.twitch.tv/someone")
    check("kick", streams.channel_url("kick", "someone") == "https://kick.com/someone")
    check("@ stripped", streams.channel_url("twitch", "@someone") == "https://www.twitch.tv/someone")
    check("nonsense rejected", streams.channel_url("twitch", "a b c") is None)
    check("unknown source rejected", streams.channel_url("hulu", "someone") is None)


def test_routing():
    print("\nrouting without the model:")
    for text, source, query in (
        ("twitch someone", "twitch", "someone"),
        ("kick someone", "kick", "someone"),
        ("play twitch someone", "twitch", "someone"),
        ("watch kick someone", "kick", "someone"),
    ):
        hit = fast_match(text)
        ok = hit and hit[0] == "stream" and hit[1]["source"] == source \
            and hit[1]["query"] == query
        check(f"{text!r} -> {source}:{query}", ok, str(hit))

    # A pasted kick.com link must not have its domain read as the verb "kick".
    hit = fast_match("https://kick.com/someone")
    ok = hit and hit[0] == "stream" and hit[1]["source"] == "kick" \
        and hit[1]["query"] == "https://kick.com/someone"
    check("a pasted link keeps the whole URL", ok, str(hit))

    hit = fast_match("https://www.twitch.tv/someone")
    check("twitch link routes whole", hit and hit[1]["query"].startswith("https://"), str(hit))

    # A channel name is one token, so a multi-word title can't be swallowed.
    for text in ("kick", "twitch", "play twitch plays pokemon on the tv"):
        hit = fast_match(text)
        check(f"{text[:38]!r} not treated as a stream",
              not (hit and hit[0] == "stream"), str(hit))

    # "kick" opens real titles, so the match must carry the original text for
    # the library to claim — the same contract "pirate radio" relies on.
    hit = fast_match("play kick ass")
    check("'play kick ass' carries text for the library guard",
          hit and hit[0] == "stream" and hit[1].get("text") == "play kick ass", str(hit))


def test_phrasings_that_failed_live():
    """Both real attempts in the channel missed and became Spotify searches.

    "stream paymoneywubby on kick" names the source AFTER the channel, and the
    pattern only had the prefix form. "stream this from kick\\n<link>" puts the
    link on a second line, which the anchored URL match cannot see.
    """
    print("\nphrasings that failed live:")
    for text in ("stream paymoneywubby on kick",
                 "stream someone from twitch",
                 "watch someone on twitch",
                 "play someone via kick"):
        hit = fast_match(text)
        ok = hit and hit[0] == "stream"
        check(f"{text!r} -> stream", ok, str(hit))

    hit = fast_match("stream paymoneywubby on kick")
    ok = hit and hit[1]["source"] == "kick" and hit[1]["query"] == "paymoneywubby"
    check("source and channel parsed the right way round", ok, str(hit))

    # The verb alone, with the source first, must still work.
    hit = fast_match("stream kick someone")
    check("'stream kick someone' still works",
          hit and hit[0] == "stream" and hit[1]["query"] == "someone", str(hit))

    print("\na link under a sentence is still a link:")
    for text, want in (
        ("stream this from kick\nhttps://kick.com/paymoneywubby",
         "https://kick.com/paymoneywubby"),
        ("put this on https://www.twitch.tv/someone please",
         "https://www.twitch.tv/someone"),
        ("https://kick.com/someone, thanks", "https://kick.com/someone"),
    ):
        hit = fast_match(text)
        ok = hit and hit[0] == "stream" and hit[1]["query"] == want
        check(f"{text.splitlines()[0][:34]!r} -> {want}", ok, str(hit))

    # A bare link keeps the existing behaviour exactly.
    hit = fast_match("https://kick.com/someone")
    check("a bare link is unchanged",
          hit and hit[1]["query"] == "https://kick.com/someone", str(hit))

    # And a message merely mentioning the word is not hijacked.
    for text in ("i watched someone on twitch yesterday",
                 "kick the tires on that one"):
        hit = fast_match(text)
        check(f"{text[:36]!r} not a stream", not (hit and hit[0] == "stream"), str(hit))


def test_item_shape():
    print("\nthe item looks like something the player can use:")
    item = live("Someone Playing Something")
    check("external", item.external is True)
    check("is_live", item.is_live is True)
    check("no duration", item.duration == 0, str(item.duration))
    check("no rating key", item.ratingKey is None)
    vod = streams.StreamItem("https://twitch.tv/videos/1", "An Old VOD", 3600.0,
                             "Someone", False, "twitch")
    check("a VOD is not live", vod.is_live is False)
    check("VOD duration in ms", vod.duration == 3600000, str(vod.duration))
    check("uploader folded into title", "Someone" in vod.title, vod.title)


async def test_library_title_beats_the_channel_lookup():
    """"kick ass" is a film. The stream patterns can swallow real titles, so
    the library gets first refusal exactly as it does for "pirate radio"."""
    print("\na film named like a channel still wins:")
    kickass = FakeItem(1, "Kick-Ass", 2010)
    lib = SearchableLib([kickass], [(1, "Kick-Ass", "movie", 2010)])
    controls = Controls(make_player(lib), lib, None)
    asked = []
    saved = streams.resolve
    try:
        streams.resolve = lambda q, s=None: (asked.append(q), (None, "offline"))[1]
        out = await controls.fast("stream", query="ass", source="kick",
                                  text="play kick-ass")
        check("played the film", "Kick-Ass" in out, out)
        check("never asked Kick", asked == [], str(asked))
    finally:
        streams.resolve = saved

    # ...and a name the library does not have still reaches the platform.
    lib2 = SearchableLib([], [])
    controls2 = Controls(make_player(lib2), lib2, None)
    asked2 = []
    try:
        streams.resolve = lambda q, s=None: (asked2.append(q), (None, "offline"))[1]
        out = await controls2.fast("stream", query="someone", source="kick",
                                   text="kick someone")
        check("unknown name goes to Kick", asked2 == ["someone"], str(asked2))
        check("and reports offline", "isn't live" in out, out)
    finally:
        streams.resolve = saved


async def test_offline_is_an_answer():
    print("\noffline reads as an answer, not a failure:")
    lib = FakeLib([])
    controls = Controls(make_player(lib), lib, None)
    saved = streams.resolve
    try:
        streams.resolve = lambda q, s=None: (None, "offline")
        out = await controls.play_stream("someone", "twitch")
        check("names the channel", "someone" in out, out)
        check("says not live", "isn't live" in out, out)
        check("names the platform", "Twitch" in out, out)
        check("did not claim to play", "Playing" not in out, out)

        streams.resolve = lambda q, s=None: (None, "notfound")
        out = await controls.play_stream("someone", "kick")
        check("a miss says couldn't find", "Couldn't find" in out, out)

        # Kick answers yt-dlp with a Cloudflare 403. Reporting that as
        # "couldn't find the channel" sends people hunting for a typo in a
        # name that is perfectly correct.
        streams.resolve = lambda q, s=None: (None, "blocked")
        out = await controls.play_stream("paymoneywubby", "kick")
        check("a block is not reported as a miss", "Couldn't find" not in out, out)
        check("says who is refusing", "refused" in out.lower(), out)
        check("clears the channel of blame", "paymoneywubby" in out, out)
    finally:
        streams.resolve = saved


async def test_missing_ytdlp_is_reported():
    print("\nno yt-dlp is explained, not crashed:")
    lib = FakeLib([])
    controls = Controls(make_player(lib), lib, None)
    saved = config.YTDL_PATH
    config.YTDL_PATH = ""
    try:
        out = await controls.play_stream("someone", "twitch")
        check("mentions yt-dlp", "yt-dlp" in out, out)
    finally:
        config.YTDL_PATH = saved


async def test_live_cannot_be_seeked():
    print("\nseeking a live stream is refused, not attempted:")
    p = make_player(FakeLib([]))
    p.mpv._tracks.append({"id": 1, "type": "video", "lang": "eng"})
    await p.play(live("Someone"))
    out = await p.seek(-30)
    check("relative seek refused", "live stream" in out, out)
    out = await p.seek_to(120)
    check("absolute seek refused", "live stream" in out, out)

    # A normal file still seeks.
    p2 = make_player(FakeLib([FakeItem(1, "Dune")]))
    await p2.play(FakeItem(1, "Dune"))
    out = await p2.seek(-30)
    check("a normal file still seeks", "live stream" not in out, out)


async def test_stream_end_goes_idle_instead_of_advancing():
    print("\nthe broadcaster going offline does not start the queue:")
    lib = FakeLib([FakeItem(1, "Dune"), FakeItem(2, "Arrival")])
    p = make_player(lib)
    p.mpv._tracks.append({"id": 1, "type": "video", "lang": "eng"})
    await p.play(live("Someone"))
    p.queue.append(2)          # something waiting that must NOT start

    notices = []
    p.on_notice = lambda m: asyncio.sleep(0, result=notices.append(m))

    await p._on_playback_finished("eof", p.mpv.entry_id, None)

    check("went idle", p.idle is True, f"idle={p.idle}")
    check("nothing playing", p.current is None, str(p.current))
    check("queue was not consumed", p.queue == [2], str(p.queue))
    check("said the stream ended", any("ended" in n for n in notices), str(notices))
    check("said they went offline", any("offline" in n for n in notices), str(notices))


async def main():
    test_url_detection()
    test_channel_urls()
    test_routing()
    test_phrasings_that_failed_live()
    test_item_shape()
    await test_library_title_beats_the_channel_lookup()
    await test_offline_is_an_answer()
    await test_missing_ytdlp_is_reported()
    await test_live_cannot_be_seeked()
    await test_stream_end_goes_idle_instead_of_advancing()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
