"""YouTube TV selection: ranking, never filtering.

The browser half of watchtv.py cannot be tested until there is a signed-in
Chrome and a live NFL Sunday. The half that decides WHAT TO PLAY can be tested
now, and it is the half that will be wrong in interesting ways — so it is a
pure function over candidate lists, and this is it under load.

The cases that matter are the ones where the same game appears more than once:
a 4K feed beside the standard one, a multiview carrying four games, a replay
whose title matches better than the live broadcast's. Getting those wrong is
not a crash, it is somebody watching the wrong thing on a Sunday.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watchtv  # noqa: E402
from watchtv import BROADCAST, MULTIVIEW, UHD, UNKNOWN, Candidate  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def cand(title, *, kind=None, live=True, replay=False, channel=""):
    text = f"{title} {channel}"
    return Candidate(
        title=title,
        kind=kind if kind is not None else watchtv.classify(text),
        live=live,
        replay=replay,
        channel=channel,
        raw=text,
    )


def test_classify():
    print("\nclassify")
    check("plain game is a broadcast",
          watchtv.classify("Seahawks at 49ers") == BROADCAST)
    check("4K badge is recognised",
          watchtv.classify("Seahawks at 49ers 4K") == UHD)
    check("multiview is recognised",
          watchtv.classify("Multiview: 4 games") == MULTIVIEW)
    # A multiview card mentioning 4K must not read as a single 4K game, or
    # asking for one game puts four on.
    check("multiview beats 4K when both appear",
          watchtv.classify("Multiview 4K: 4 games") == MULTIVIEW)
    # "4k" inside another token is not a 4K feed. A false positive here
    # demotes the correct result.
    check("'4k' inside a word does not count",
          watchtv.classify("Match4king Cup") == BROADCAST,
          watchtv.classify("Match4king Cup"))


def test_live_and_replay():
    print("\nlive vs replay")
    check("LIVE badge reads live", watchtv.looks_live("LIVE · FOX"))
    check("'on now' reads live", watchtv.looks_live("On now"))
    check("highlights read as replay", watchtv.looks_replay("Seahawks Highlights"))
    check("condensed reads as replay", watchtv.looks_replay("Condensed Game"))
    check("a plain title is neither",
          not watchtv.looks_live("Seahawks at 49ers")
          and not watchtv.looks_replay("Seahawks at 49ers"))


def test_match_strength():
    print("\nmatch strength")
    c = cand("Seahawks at 49ers", channel="FOX")
    check("full term matches", watchtv.match_strength("seahawks", c) == 1)
    check("both terms match", watchtv.match_strength("seahawks 49ers", c) == 2)
    check("partial word does not match",
          watchtv.match_strength("sea", c) == 0,
          "'sea' must not match 'Seahawks'")
    check("channel name counts",
          watchtv.match_strength("fox", c) == 1)


def test_broadcast_beats_4k():
    print("\nthe duplicate case: same game, two feeds")
    pick, ranked, note = watchtv.choose(
        [cand("Seahawks at 49ers 4K", channel="FOX"),
         cand("Seahawks at 49ers", channel="FOX")],
        query="seahawks")
    check("standard feed wins over 4K",
          pick is not None and pick.kind == BROADCAST, pick.describe())
    check("the 4K feed is still offered, not dropped",
          any(c.kind == UHD for c in ranked))
    check("alternatives mention it",
          "4K" in watchtv.alternatives(pick, ranked),
          watchtv.alternatives(pick, ranked))


def test_4k_only_still_plays():
    print("\nthe case a hardcoded rule would break")
    # The whole reason selection ranks instead of filtering: if the game is
    # ONLY on the 4K feed, it must still play.
    pick, ranked, note = watchtv.choose(
        [cand("Seahawks at 49ers 4K", channel="FOX")], query="seahawks")
    check("4K-only game still plays",
          pick is not None and pick.kind == UHD, pick.describe() if pick else "none")


def test_multiview_ranks_below_the_game():
    print("\nmultiview")
    pool = [cand("Multiview: Seahawks, Cowboys, Bills, Chiefs"),
            cand("Seahawks at 49ers", channel="FOX")]
    pick, ranked, _ = watchtv.choose(pool, query="seahawks")
    check("dedicated broadcast beats a multiview containing it",
          pick.kind == BROADCAST, pick.describe())
    # ...but asking for it gets it.
    pick2, _, _ = watchtv.choose(pool, query="seahawks", prefer=MULTIVIEW)
    check("preferring multiview selects it",
          pick2.kind == MULTIVIEW, pick2.describe())
    # ...and it plays when it is the only thing carrying the game.
    pick3, _, _ = watchtv.choose(
        [cand("Multiview: Seahawks, Cowboys, Bills, Chiefs")], query="seahawks")
    check("multiview plays when it is the only match",
          pick3 is not None and pick3.kind == MULTIVIEW)


def test_replay_never_beats_live():
    print("\nreplays")
    # The replay's title matches the query BETTER than the live game's.
    pick, ranked, _ = watchtv.choose(
        [cand("Seahawks Highlights", replay=True, live=False),
         cand("Seahawks at 49ers", channel="FOX")],
        query="seahawks")
    check("live game beats a better-matching replay",
          pick is not None and not pick.replay, pick.describe())


def test_nothing_live_says_so():
    print("\nnothing live")
    pick, ranked, note = watchtv.choose(
        [cand("Seahawks at 49ers", live=False)], query="seahawks")
    check("no pick when nothing is live", pick is None)
    check("and it says what it found", "Nothing matching that is live" in note,
          note)


def test_ties_are_reported():
    print("\nambiguity")
    pick, ranked, note = watchtv.choose(
        [cand("Seahawks at 49ers", channel="FOX"),
         cand("Seahawks at 49ers", channel="FOX")],
        query="seahawks")
    check("a tie is mentioned rather than resolved silently",
          "equally good" in note, note)


def test_unknown_is_surfaced():
    print("\nunrecognised results")
    pick, ranked, note = watchtv.choose(
        [cand("Seahawks at 49ers", channel="FOX"),
         cand("something we cannot parse", kind=UNKNOWN)],
        query="seahawks")
    check("still plays the good match", pick is not None and pick.kind == BROADCAST)
    check("but says something was unidentifiable",
          "could not identify" in note, note)


def test_empty():
    print("\nnothing at all")
    pick, ranked, note = watchtv.choose([], query="seahawks")
    check("no pick", pick is None)
    check("and an answer", bool(note), note)


def main():
    test_classify()
    test_live_and_replay()
    test_match_strength()
    test_broadcast_beats_4k()
    test_4k_only_still_plays()
    test_multiview_ranks_below_the_game()
    test_replay_never_beats_live()
    test_nothing_live_says_so()
    test_ties_are_reported()
    test_unknown_is_surfaced()
    test_empty()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
