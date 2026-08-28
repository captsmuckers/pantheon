"""Playing/queueing from a pasted Spotify link, checking what's playing, and
clearing the queue. No network — sp is a minimal fake standing in for
spotipy's client.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeItem, FakeLib, FakeSpotify, make_player  # noqa: E402

from brain import Controls, fast_match  # noqa: E402
from spotify import SpotifyController, find_uri  # noqa: E402

PASS = []

TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"  # a real-shaped (22-char) Spotify id


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_find_uri():
    print("recognising Spotify links:")
    good = [
        (f"https://open.spotify.com/track/{TRACK_ID}", f"spotify:track:{TRACK_ID}"),
        (f"https://open.spotify.com/track/{TRACK_ID}?si=abc123", f"spotify:track:{TRACK_ID}"),
        (f"open.spotify.com/album/{TRACK_ID}", f"spotify:album:{TRACK_ID}"),
        (f"https://open.spotify.com/intl-en/playlist/{TRACK_ID}", f"spotify:playlist:{TRACK_ID}"),
        (f"play https://open.spotify.com/track/{TRACK_ID}", f"spotify:track:{TRACK_ID}"),
        (f"spotify:track:{TRACK_ID}", f"spotify:track:{TRACK_ID}"),
    ]
    for text, expected in good:
        check(f"{text[:50]!r}", find_uri(text) == expected, str(find_uri(text)))

    bad = ["play dune", "https://open.spotify.com/user/someone", "", "spotify:track:short"]
    for text in bad:
        check(f"not a link: {text[:40]!r}", find_uri(text) is None, str(find_uri(text)))


def test_fast_match_routes_links():
    print("\nfast_match routes a pasted link, play by default, queue on request:")
    url = f"https://open.spotify.com/track/{TRACK_ID}"
    uri = f"spotify:track:{TRACK_ID}"

    hit = fast_match(url)
    check("bare link plays", hit == ("spotify_link", {"uri": uri, "mode": "play"}), str(hit))

    hit = fast_match(f"play {url}")
    check("link with a verb still plays",
          hit == ("spotify_link", {"uri": uri, "mode": "play"}), str(hit))

    hit = fast_match(f"queue {url}")
    check("queue verb queues instead",
          hit == ("spotify_link", {"uri": uri, "mode": "queue"}), str(hit))

    hit = fast_match(f"spotify:track:{TRACK_ID}")
    check("a bare uri routes the same way",
          hit == ("spotify_link", {"uri": uri, "mode": "play"}), str(hit))


class FakeSp:
    def __init__(self, track_item=None, current=None, queued=()):
        self.calls = []
        self._track_item = track_item
        self._current = current
        self._queued = list(queued)

    def track(self, uri):
        self.calls.append(("track", uri))
        return self._track_item

    def start_playback(self, device_id=None, uris=None, context_uri=None, position_ms=None):
        self.calls.append(("start_playback", uris, position_ms))

    def add_to_queue(self, uri, device_id=None):
        self.calls.append(("add_to_queue", uri))

    def current_playback(self):
        self.calls.append("current_playback")
        return self._current

    def queue(self):
        self.calls.append("queue")
        return {"queue": self._queued}

    def next_track(self, device_id=None):
        self.calls.append("next_track")


def controller(sp) -> SpotifyController:
    s = SpotifyController.__new__(SpotifyController)
    s.enabled = True
    s.premium = True
    s.playing = False
    s.last_label = ""
    s.device_id = None
    s.sp = sp
    s.ensure_device = lambda: "device-1"
    return s


def test_play_link_looks_up_a_label():
    print("\nplay_link() names the track instead of echoing the uri:")
    uri = f"spotify:track:{TRACK_ID}"
    item = {"name": "Sicko Mode", "artists": [{"name": "Travis Scott"}]}
    sp = FakeSp(track_item=item)
    s = controller(sp)

    result = s.play_link(uri)
    check("looked up the track", ("track", uri) in sp.calls, str(sp.calls))
    check("played it", ("start_playback", [uri], None) in sp.calls, str(sp.calls))
    check("used the real title, not the uri", "Sicko Mode" in result, result)


def test_play_link_survives_a_lookup_failure():
    print("\nplay_link() still plays if the label lookup fails:")
    uri = f"spotify:track:{TRACK_ID}"
    sp = FakeSp(track_item=None)  # sp.track() returns None -> no label
    s = controller(sp)
    result = s.play_link(uri)
    check("still played", ("start_playback", [uri], None) in sp.calls, str(sp.calls))
    check("falls back to the uri as the label", uri in result, result)


def test_queue_link():
    print("\nqueue_link() adds without interrupting:")
    uri = f"spotify:track:{TRACK_ID}"
    item = {"name": "Sicko Mode", "artists": [{"name": "Travis Scott"}]}
    sp = FakeSp(track_item=item)
    s = controller(sp)
    result = s.queue_link(uri)
    check("added to queue, not played",
          ("add_to_queue", uri) in sp.calls
          and not any(isinstance(c, tuple) and c[0] == "start_playback" for c in sp.calls),
          str(sp.calls))
    check("named the track", "Sicko Mode" in result, result)


def test_clear_queue_skips_through_it():
    print("\nclear_queue() skips through what's queued (restart-in-place measured not to work):")
    queued = [{"uri": f"spotify:track:queued{i}"} for i in range(3)]
    sp = FakeSp(queued=queued)
    s = controller(sp)
    result = s.clear_queue()
    check("read the queue first", sp.calls[0] == "queue", str(sp.calls))
    check("skipped once per queued track",
          sp.calls.count("next_track") == 3, str(sp.calls))
    check("never tried restarting in place",
          not any(isinstance(c, tuple) and c[0] == "start_playback" for c in sp.calls),
          str(sp.calls))
    check("reports how many it skipped", "3" in result, result)


def test_clear_queue_already_empty():
    print("\nclear_queue() when there's nothing queued:")
    sp = FakeSp(queued=[])
    s = controller(sp)
    result = s.clear_queue()
    check("says it's already empty", "already empty" in result.lower(), result)
    check("never skipped anything", "next_track" not in sp.calls, str(sp.calls))


async def test_controls_dispatch_the_new_actions():
    print("\nControls.fast() wires the new actions through:")
    spot = FakeSpotify()
    lib = FakeLib([FakeItem(1, "Dune")])
    controls = Controls(make_player(lib), lib, spot)

    uri = f"spotify:track:{TRACK_ID}"
    result = await controls.fast("spotify_link", uri=uri, mode="play")
    check("spotify_link plays", spot.played == [uri], str(spot.played))
    check("active source switched to spotify", controls.active == "spotify", controls.active)

    result = await controls.fast("spotify_link", uri=uri, mode="queue")
    check("spotify_link with mode=queue calls queue, not play",
          spot.played == [uri], str(spot.played))  # unchanged since the first call

    spot.playing = True
    spot.last_label = "Karma Police — Radiohead"
    result = await controls.fast("spotify_status")
    check("spotify_status reports it", "Karma Police" in result, result)

    result = await controls.fast("spotify_clear_queue")
    check("spotify_clear_queue reaches the fake", "Cleared" in result, result)


async def main():
    test_find_uri()
    test_fast_match_routes_links()
    test_play_link_looks_up_a_label()
    test_play_link_survives_a_lookup_failure()
    test_queue_link()
    test_clear_queue_skips_through_it()
    test_clear_queue_already_empty()
    await test_controls_dispatch_the_new_actions()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
