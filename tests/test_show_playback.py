"""A series must never reach stream_url.

Caught live: "put something on with dragons in it" searched the library, found
the SERIES Dragon Ball Super, and passed its rating_key to play_media. A series
has no media parts, so stream_url raised

    AttributeError: 'Show' object has no attribute 'media'

and logged a traceback. It happened again minutes later with Regular Show, so
it is systematic rather than a quirk of one title.

Three places resolve a show to an episode. Two of them did; Controls._resolve,
the one the tool path uses for an explicit rating_key, did not. The two that
did also fell back to `up_next(item) or item`, which hands the series on when
there is no next episode and only defers the same crash.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeItem, FakeLib, make_player  # noqa: E402

from brain import Controls  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


class ShowLib(FakeLib):
    """A library whose only match is a series, with a switchable next episode."""

    def __init__(self, episode=None):
        super().__init__([])
        self.show = FakeItem(271377, "Dragon Ball Super", 2015)
        self.show.type = "show"
        self.episode = episode
        self.stream_url_called_with = []

    def fetch(self, key):
        return self.show

    def up_next(self, show):
        return self.episode

    def stream_url(self, item):
        self.stream_url_called_with.append(item)
        return None if getattr(item, "type", None) == "show" else "http://x/file.mkv"


async def test_rating_key_for_a_series_resolves_to_an_episode():
    print("a series named by rating_key becomes an episode:")
    episode = FakeItem(271400, "Dragon Ball Super S01E01", 2015)
    lib = ShowLib(episode=episode)
    controls = Controls(make_player(lib), lib, None)
    item, options = await controls._resolve({"rating_key": 271377})
    check("resolved to the episode", item is episode,
          f"got {getattr(item, 'title', item)!r}")
    check("no disambiguation needed", options == [], str(options))


async def test_series_with_no_episode_resolves_to_nothing():
    print("\na series with nothing playable resolves to nothing, not the series:")
    lib = ShowLib(episode=None)
    controls = Controls(make_player(lib), lib, None)
    item, _ = await controls._resolve({"rating_key": 271377})
    check("returns None", item is None, str(item))


async def test_picker_choice_explains_instead_of_crashing():
    print("\npicking a series from the disambiguation list says so:")
    lib = ShowLib(episode=None)
    controls = Controls(make_player(lib), lib, None)
    result = await controls.play_media_option(271377)
    check("explains the problem", "playable episode" in result.lower(), result)
    check("never asked for a stream URL", not lib.stream_url_called_with,
          str(lib.stream_url_called_with))


async def main():
    await test_rating_key_for_a_series_resolves_to_an_episode()
    await test_series_with_no_episode_resolves_to_nothing()
    await test_picker_choice_explains_instead_of_crashing()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
