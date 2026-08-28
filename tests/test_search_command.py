"""`search` looks in both collections and never plays anything."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeItem, FakeLib, FakeSpotify, make_player  # noqa: E402

from brain import Controls, fast_match  # noqa: E402
from library import TitleEntry  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


class SearchLib(FakeLib):
    def __init__(self, titles):
        super().__init__([])
        self._entries = [
            TitleEntry(rating_key=k, title=t, kind="movie", year=y, library="Movies")
            for k, t, y in titles
        ]

    def search(self, query, kind=None, limit=25, library=None, year=None):
        q = (query or "").lower()
        return [e for e in self._entries if q in e.title.lower()][:limit]


class SearchSpotify(FakeSpotify):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.queries = []

    def search_list(self, query, limit=6):
        self.queries.append(query)
        return [dict(r) for r in self.rows][:limit], None


def controls_with(titles=(), rows=()):
    lib = SearchLib(titles)
    spot = SearchSpotify(rows)
    return Controls(make_player(lib), lib, spot), spot


def song(label):
    return {"uri": "spotify:track:x", "kind": "track", "label": label}


async def test_searches_both_collections():
    print("a plain search covers library and Spotify:")
    controls, spot = controls_with(
        titles=[(1, "Nacho Libre", 2006)],
        rows=[song("Nacho Libre Tribute — Mister Loco")],
    )
    out = await (controls.fast("search_debug", query="nacho libre"))
    check("library section shown", "In your library" in out, out[:70])
    check("spotify section shown", "On Spotify" in out, out[:70])
    check("nothing was played", controls.player.current is None)
    check("tells you how to start one", "play" in out.lower())


async def test_scope_words_narrow_it():
    print("\nscope words narrow the search:")
    controls, spot = controls_with(
        titles=[(1, "Dune", 1984)], rows=[song("Dune — Someone")])

    out = await (controls.fast("search_debug", query="movie dune"))
    check("movie -> library only", "On Spotify" not in out, out[:60])

    out = await (controls.fast("search_debug", query="spotify dune"))
    check("spotify -> Spotify only", "In your library" not in out, out[:60])


async def test_scope_word_is_not_part_of_the_query():
    """'search spotify toxicity' must not ask Spotify for 'spotify toxicity',
    which returns playlists *about* Spotify."""
    print("\nthe scope word is stripped before searching:")
    controls, spot = controls_with(rows=[song("Toxicity — System Of A Down")])
    await (controls.fast("search_debug", query="spotify toxicity"))
    check("searched for 'toxicity'", spot.queries == ["toxicity"], str(spot.queries))


async def test_irrelevant_results_are_dropped():
    """Spotify always returns something; a search must not report nonsense."""
    print("\nunrelated fuzzy matches are filtered out:")
    controls, spot = controls_with(
        rows=[song("Nothing — Jeezy"), song("Ww — Other Nothing"),
              song("Nothingness — Weedpecker")],
    )
    out = await (controls.fast("search_debug", query="zzzznothingzzz"))
    check("reported an honest miss", "Nothing matching" in out, out[:70])

    # ...but a shared word is enough to keep a near-miss.
    controls, spot = controls_with(
        rows=[song("1234 1234 — Streetlight Manifesto")])
    out = await (controls.fast("search_debug",
                                    query="streeghtlight manifesto"))
    check("kept the near-match", "Streetlight" in out, out[:70])


async def test_search_phrasings_reach_the_command():
    print("\nphrasings that mean search:")
    for text in ("search dune", "search for dune", "find dune", "look up dune"):
        hit = fast_match(text)
        ok = hit and hit[0] == "search_debug" and hit[1]["query"] == "dune"
        check(f"{text!r}", ok, str(hit))


async def main():
    await test_searches_both_collections()
    await test_scope_words_narrow_it()
    await test_scope_word_is_not_part_of_the_query()
    await test_irrelevant_results_are_dropped()
    await test_search_phrasings_reach_the_command()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
