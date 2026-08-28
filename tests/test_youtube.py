"""YouTube playback: routing, and not breaking the Plex-shaped assumptions."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeItem, FakeLib, FakeSpotify, make_player  # noqa: E402

import config  # noqa: E402
import youtube  # noqa: E402
from brain import Controls, fast_match  # noqa: E402
from library import describe  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_url_detection():
    print("recognising links:")
    good = [
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        "http://youtube.com/watch?v=aqz-KE-bpKQ",
        "https://youtu.be/aqz-KE-bpKQ",
        "https://m.youtube.com/watch?v=aqz-KE-bpKQ&t=30s",
        "https://www.youtube.com/shorts/abc123def45",
        "https://www.youtube.com/live/abc123def45",
    ]
    bad = [
        "play dune",
        "youtube big buck bunny",
        "https://example.com/watch?v=abc",
        "https://open.spotify.com/track/123",
        "watch the office",
        "",
    ]
    for u in good:
        check(f"{u[:46]!r}", youtube.is_url(u))
    for u in bad:
        check(f"not a link: {u[:38]!r}", not youtube.is_url(u))


def test_routing():
    print("\nphrasings that mean YouTube:")
    for text, expected in (
        ("https://youtu.be/aqz-KE-bpKQ", "https://youtu.be/aqz-KE-bpKQ"),
        ("youtube big buck bunny", "big buck bunny"),
        ("yt big buck bunny", "big buck bunny"),
        ("play youtube big buck bunny", "big buck bunny"),
        ("watch youtube some clip", "some clip"),
    ):
        hit = fast_match(text)
        ok = hit and hit[0] == "youtube" and hit[1]["query"] == expected
        check(f"{text[:44]!r}", ok, str(hit))

    print("\n...and phrasings that must NOT:")
    for text in ("play dune", "music radiohead", "queue the sound of silence",
                 "play a random king of the hill episode"):
        hit = fast_match(text)
        check(f"{text[:44]!r}", not (hit and hit[0] == "youtube"), str(hit))


def test_item_shape():
    print("\na YouTube item behaves like something the player can hold:")
    item = youtube.YouTubeItem("https://youtu.be/x", "Big Buck Bunny", 635.0, "Blender")
    check("marked external", item.external is True)
    check("has a stream url", item.stream_url == "https://youtu.be/x")
    check("duration in ms, like Plex", item.duration == 635_000, str(item.duration))
    check("title includes uploader", "Blender" in item.title, item.title)
    check("describe() handles it", describe(item) == item.title, describe(item))
    check("no rating key", item.ratingKey is None)
    check("no library section", item.librarySectionTitle is None)

    live = youtube.YouTubeItem("https://youtu.be/y", "A Live Stream", None)
    check("live stream has zero duration", live.duration == 0)


async def test_playback_skips_plex_specifics():
    """The player must not try to build a Plex URL, read a resume position, or
    report a timeline for something that isn't in Plex."""
    print("\nplaying it doesn't touch Plex:")
    lib = FakeLib([FakeItem(1, "Dune")])
    called = []
    lib.stream_url = lambda item: called.append("stream_url") or "SHOULD-NOT-BE-USED"
    lib.resume_offset = lambda item: called.append("resume_offset") or 999.0
    lib.report_progress = lambda *a: called.append("report_progress")
    lib.mark_played = lambda item: called.append("mark_played")

    player = make_player(lib)
    item = youtube.YouTubeItem("https://youtu.be/x", "Big Buck Bunny", 635.0)
    result = await player.play(item)

    check("played", player.current is item, str(player.current))
    check("says the title", "Big Buck Bunny" in result, result)
    check("no Plex calls at all", called == [], str(called))
    check("mpv got the youtube url",
          "https://youtu.be/x" in player.mpv.loads, str(player.mpv.loads))
    check("started at the beginning, no resume", player.position == 0.0)

    # A finished YouTube video must not be marked watched in Plex.
    await player._on_playback_finished("eof", entry_id=player._entry_id)
    check("not marked played", "mark_played" not in called, str(called))


async def test_queueing_is_refused_clearly():
    print("\nqueueing is refused with a reason, not a crash:")
    lib = FakeLib([FakeItem(1, "Dune")])
    player = make_player(lib)
    await player.play(FakeItem(1, "Dune"))          # something already on
    item = youtube.YouTubeItem("https://youtu.be/x", "Big Buck Bunny", 635.0)
    result = await player.queue_add(item)
    check("refused", "can't queue" in result.lower(), result)
    check("explains why", "immediately" in result.lower(), result)
    check("queue untouched", player.queue == [], str(player.queue))


async def test_controls_report_failures():
    print("\nfailures read as failures:")
    lib = FakeLib([])
    controls = Controls(make_player(lib), lib, FakeSpotify())
    controls.to_video = lambda: asyncio.sleep(0)

    original = youtube.resolve
    youtube.resolve = lambda q: None
    try:
        out = await controls.play_youtube("something unfindable")
        check("miss is reported", "Couldn't find" in out, out)
        check("mentions likely causes", "age-restricted" in out, out)
    finally:
        youtube.resolve = original

    saved = config.YTDL_PATH
    config.YTDL_PATH = ""
    try:
        out = await controls.play_youtube("anything")
        check("missing yt-dlp says so", "yt-dlp" in out, out)
    finally:
        config.YTDL_PATH = saved


def test_age_restriction_detection():
    print("\nwhich yt-dlp errors mean an age gate:")
    hits = [
        "ERROR: Sign in to confirm your age",
        "This video may be inappropriate for some users.",
        "content is age-restricted",
    ]
    misses = [
        "ERROR: Video unavailable",
        "This video is private",
        "HTTP Error 404: Not Found",
        "",
    ]
    for msg in hits:
        check(f"caught: {msg[:40]!r}", youtube._looks_age_restricted(Exception(msg)))
    for msg in misses:
        check(f"not confused by: {msg[:40]!r}", not youtube._looks_age_restricted(Exception(msg)))


def test_age_restricted_only_raised_for_links():
    print("\nAgeRestricted only fires when there's a real URL to hand off:")

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            raise Exception("Sign in to confirm your age")

    fake_module = type(sys)("yt_dlp")
    fake_module.YoutubeDL = FakeYDL
    saved = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = fake_module
    try:
        url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
        try:
            youtube.resolve(url)
            check("raises for a link", False, "no exception raised")
        except youtube.AgeRestricted as exc:
            check("raises for a link", exc.url == url, exc.url)

        # A search phrase never reaches the URL branch, even on the same error.
        check("returns None for a search phrase, no raise",
              youtube.resolve("some age gated search") is None)
    finally:
        if saved is not None:
            sys.modules["yt_dlp"] = saved
        else:
            del sys.modules["yt_dlp"]


async def test_age_restricted_link_opens_in_browser():
    print("\nan age-gated link falls back to a browser instead of failing:")
    import browser as browser_mod

    lib = FakeLib([])
    controls = Controls(make_player(lib), lib, FakeSpotify())
    controls.to_video = lambda: asyncio.sleep(0)

    original_resolve = youtube.resolve
    original_open = browser_mod.open_video
    opened = []

    def fake_resolve(q):
        raise youtube.AgeRestricted("https://www.youtube.com/watch?v=gated")

    def fake_open(url):
        opened.append(url)
        return "", 424242

    youtube.resolve = fake_resolve
    browser_mod.open_video = fake_open
    try:
        out = await controls.play_youtube("https://www.youtube.com/watch?v=gated")
        check("opened the right url", opened == ["https://www.youtube.com/watch?v=gated"], str(opened))
        check("says what happened", "browser" in out.lower(), out)
        check("active source switched", controls.active == "browser", controls.active)
        check("hwnd remembered", controls._browser_hwnd == 424242, str(controls._browser_hwnd))
        check("assumed playing (autoplay)", controls._browser_playing is True)
    finally:
        youtube.resolve = original_resolve
        browser_mod.open_video = original_open

    saved = config.YOUTUBE_BROWSER_FALLBACK
    config.YOUTUBE_BROWSER_FALLBACK = False
    youtube.resolve = fake_resolve
    try:
        out = await controls.play_youtube("https://www.youtube.com/watch?v=gated")
        check("fallback disabled: no browser call", "browser" not in out.lower(), out)
        check("fallback disabled: still gives the link", "gated" in out, out)
    finally:
        youtube.resolve = original_resolve
        config.YOUTUBE_BROWSER_FALLBACK = saved


async def test_browser_pause_resume_is_a_tracked_toggle():
    print("\nbrowser pause/resume: a keypress toggle aimed by tracked state:")
    import wm

    lib = FakeLib([])
    controls = Controls(make_player(lib), lib, FakeSpotify())
    controls.active = "browser"
    controls._browser_hwnd = 99
    controls._browser_playing = True

    original_showing = wm.is_showing
    original_front = wm.bring_to_front
    original_key = wm.send_key
    keys_sent = []
    wm.is_showing = lambda hwnd: True
    wm.bring_to_front = lambda hwnd: True
    wm.send_key = lambda vk: keys_sent.append(vk)
    try:
        out = await controls.fast("pause")
        check("pause sends the key", keys_sent == [wm.VK_SPACE], str(keys_sent))
        check("pause flips tracked state", controls._browser_playing is False)
        check("says paused", "paused" in out.lower(), out)

        out = await controls.fast("pause")
        check("pause again sends nothing", keys_sent == [wm.VK_SPACE], str(keys_sent))
        check("says already paused", "already" in out.lower(), out)

        out = await controls.fast("resume")
        check("resume sends the key", keys_sent == [wm.VK_SPACE, wm.VK_SPACE],
              str(keys_sent))
        check("resume flips tracked state back", controls._browser_playing is True)
        check("says resumed", "resumed" in out.lower(), out)

        wm.is_showing = lambda hwnd: False
        out = await controls.fast("pause")
        check("closed window: no key sent",
              keys_sent == [wm.VK_SPACE, wm.VK_SPACE], str(keys_sent))
        check("closed window: says nothing's open", "nothing" in out.lower(), out)
        check("closed window: hwnd forgotten", controls._browser_hwnd is None)
    finally:
        wm.is_showing = original_showing
        wm.bring_to_front = original_front
        wm.send_key = original_key


async def test_start_is_never_none_over_ipc():
    """Assigning start="none" as a runtime property makes mpv reject EDL
    sources, which is how YouTube arrives once yt-dlp splits video and audio.
    It failed with "no audio or video data played" while the identical value
    on the command line was harmless."""
    print("\nthe start property is never set to 'none':")
    lib = FakeLib([FakeItem(1, "Dune")])
    player = make_player(lib)
    await player.play(youtube.YouTubeItem("https://youtu.be/x", "A Video", 60.0))
    starts = player.mpv.sets("start")
    check("start was set", starts, str(starts))
    check("never the string 'none'", "none" not in starts, str(starts))
    check("cleared to zero instead", "0" in starts, str(starts))

    # ...and a real resume offset still gets through.
    player2 = make_player(FakeLib([FakeItem(1, "Dune")]))
    await player2.play(FakeItem(1, "Dune"), offset=125.0)
    check("a real offset is passed", "125" in player2.mpv.sets("start"),
          str(player2.mpv.sets("start")))


async def test_failed_external_load_is_reported():
    """A dead link must not be announced as 'Playing'."""
    print("\na failed YouTube load is not announced as success:")
    import player as player_mod
    lib = FakeLib([])
    p = make_player(lib)

    original = player_mod.EXTERNAL_LOAD_TIMEOUT
    player_mod.EXTERNAL_LOAD_TIMEOUT = 2
    try:
        # mpv reports the failure asynchronously, exactly as it does live.
        async def fail_soon():
            await asyncio.sleep(0.4)
            p._last_end_file = (p.mpv.entry_id, "error", "no audio or video data played")
        asyncio.get_running_loop().create_task(fail_soon())
        result = await p.play(youtube.YouTubeItem("https://youtu.be/dead", "Dead Link", 60.0))
    finally:
        player_mod.EXTERNAL_LOAD_TIMEOUT = original

    check("does not claim it played", "Playing" not in result, result)
    check("says what went wrong", "wouldn't play" in result, result)
    check("relays mpv's reason", "no audio or video" in result, result)
    check("left idle, not falsely current", p.current is None, str(p.current))


async def test_successful_external_load_is_announced():
    print("\na good load still reports normally:")
    p = make_player(FakeLib([]))
    # FakeMPV returns a non-empty track_list, so the load confirms.
    p.mpv._tracks.append({"id": 1, "type": "video", "lang": "eng"})
    result = await p.play(youtube.YouTubeItem("https://youtu.be/ok", "Good Video", 60.0))
    check("announced", "Playing" in result and "Good Video" in result, result)


async def main():
    test_url_detection()
    test_routing()
    test_item_shape()
    await test_playback_skips_plex_specifics()
    await test_queueing_is_refused_clearly()
    await test_controls_report_failures()
    test_age_restriction_detection()
    test_age_restricted_only_raised_for_links()
    await test_age_restricted_link_opens_in_browser()
    await test_browser_pause_resume_is_a_tracked_toggle()
    await test_start_is_never_none_over_ipc()
    await test_failed_external_load_is_reported()
    await test_successful_external_load_is_announced()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
