"""Query resolution: random picks, and 'episode' implying the series.

Both come from one real ambiguity: libraries hold films and shows with the
same name. King of the Hill is a 1993 film and a 1997 series.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import fast_match  # noqa: E402
from library import Library, TitleEntry  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


class FakeEpisode:
    type = "episode"

    def __init__(self, n):
        self.ratingKey = 9000 + n
        self.title = f"Episode {n}"
        self.seasonNumber, self.episodeNumber = 1, n
        self.grandparentTitle = "King of the Hill"


class FakeMovie:
    type = "movie"

    def __init__(self, key, title, year):
        self.ratingKey, self.title, self.year = key, title, year


class FakeShow:
    type = "show"

    def __init__(self, key, count=20):
        self.ratingKey = key
        self.type = "show"
        self._eps = [FakeEpisode(i) for i in range(1, count + 1)]

    def episodes(self):
        return self._eps

    def onDeck(self):
        return self._eps[0]


class StubLibrary(Library):
    """Library with the real resolution logic over a fixed title list."""

    def __init__(self, entries, items=None):
        self._by_section = {"1": list(entries)}
        self._tokens = {}
        self._next_refresh_at = float("inf")   # never revalidate
        import threading
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._degraded = False
        self._items = items or {}

    def fetch(self, rating_key):
        return self._items[int(rating_key)]


FILM = TitleEntry(1, "King of the Hill", "movie", 1993, "Movies")
SHOW = TitleEntry(2, "King of the Hill", "show", 1997, "TV Shows")
OTHER = TitleEntry(3, "Severance", "show", 2022, "TV Shows")
RANDOM_FILM = TitleEntry(4, "Random Hearts", "movie", 1999, "Movies")


def make_lib():
    return StubLibrary(
        [FILM, SHOW, OTHER, RANDOM_FILM],
        {
            1: FakeMovie(1, "King of the Hill", 1993),
            2: FakeShow(2),
            3: FakeShow(3),
            4: FakeMovie(4, "Random Hearts", 1999),
        },
    )


def test_episode_word_implies_show():
    print("'episode' picks the series over the same-named film:")
    lib = make_lib()
    item, options = lib.resolve_query("king of the hill episode")
    check("no ambiguity question", not options, str([o.label() for o in options]))
    check("resolved to an episode", getattr(item, "type", None) == "episode", str(item))

    # Without the word, the tie is real and should still be asked about.
    item, options = lib.resolve_query("king of the hill")
    check("bare title still asks", len(options) == 2, str([o.label() for o in options]))


def test_random_picks_vary():
    print("\nrandom actually varies:")
    lib = make_lib()
    seen = set()
    for _ in range(30):
        item, options = lib.resolve_query("random king of the hill episode")
        check_ok = not options and getattr(item, "type", None) == "episode"
        if not check_ok:
            check("every pick resolved", False, str(item))
            return
        seen.add(item.ratingKey)
    check("resolves every time", True)
    check("picks differ across calls", len(seen) > 1, f"{len(seen)} distinct of 20 episodes")


def test_random_phrasings():
    print("\nrandom phrasings:")
    lib = make_lib()
    for phrase in ("random king of the hill episode",
                   "a random king of the hill episode",
                   "a random episode of king of the hill",
                   "random severance",
                   "some random severance episodes"):
        item, options = lib.resolve_query(phrase)
        ok = not options and getattr(item, "type", None) == "episode"
        check(f"{phrase!r}", ok, str(item))


def test_random_titled_film_survives():
    print("\na film whose name starts with 'random' is not a random request:")
    lib = make_lib()
    item, options = lib.resolve_query("random hearts")
    check("played the film", getattr(item, "title", None) == "Random Hearts", str(item))
    check("not an episode", getattr(item, "type", None) != "episode")


def test_random_reaches_the_fast_path():
    print("\nrandom needs no model:")
    for phrase, action in (
        ("play a random king of the hill episode", "play"),
        ("play random severance", "play"),
        ("queue a random king of the hill episode", "queue"),
    ):
        hit = fast_match(phrase)
        ok = hit and hit[0] == action
        check(f"{phrase!r}", ok, str(hit))


def main():
    test_episode_word_implies_show()
    test_random_picks_vary()
    test_random_phrasings()
    test_random_titled_film_survives()
    test_random_reaches_the_fast_path()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
