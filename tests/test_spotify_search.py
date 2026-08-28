"""Spotify search: 'by' handling, and never substituting a different artist.

No network — sp.search is stubbed with the shapes the real API returns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spotify import SpotifyController  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def track(name, *artists):
    return {"name": name, "uri": f"spotify:track:{name}",
            "artists": [{"name": a} for a in artists]}


class FakeSp:
    """Answers field-filtered and free-text searches differently, as Spotify does."""

    def __init__(self, filtered=None, plain=None):
        self.filtered = filtered if filtered is not None else []
        self.plain = plain if plain is not None else []
        self.queries = []

    def search(self, q, type="", limit=5):
        self.queries.append(q)
        rows = self.filtered if ("artist:" in q or "track:" in q) else self.plain
        out = {}
        for t in type.split(","):
            out[t + "s"] = {"items": rows if t == "track" else []}
        return out


def controller(filtered=None, plain=None):
    s = SpotifyController.__new__(SpotifyController)
    s.enabled = True
    s.premium = True
    s.playing = False
    s.last_label = ""
    s.device_id = None
    s.sp = FakeSp(filtered, plain)
    return s


def test_by_becomes_a_field_filter():
    print("'<title> by <artist>' uses Spotify field filters:")
    s = controller(filtered=[track("Sometimes", "One True God", "Cxssidy")])
    opts, _ = s.search_options("song sometimes by one true god")
    check("used a filtered query",
          any('artist:"one true god"' in q for q in s.sp.queries), str(s.sp.queries))
    check("returned the right track",
          opts and "One True God" in opts[0]["label"], str(opts))


def test_splits_on_the_last_by():
    print("\ntitles containing 'by' survive:")
    s = controller(filtered=[track("Stand By Me", "Ben E. King")])
    s.search_options("song stand by me by ben e king")
    q = next((x for x in s.sp.queries if "track:" in x), "")
    check('title kept as "stand by me"', 'track:"stand by me"' in q, q)
    check('artist parsed as "ben e king"', 'artist:"ben e king"' in q, q)


def test_no_by_is_untouched():
    print("\nqueries without 'by' are unchanged:")
    s = controller(plain=[track("Murmaider", "Dethklok")])
    opts, _ = s.search_options("song murmaider")
    check("no field filter used",
          not any("track:" in q for q in s.sp.queries), str(s.sp.queries))
    check("found it", opts and opts[0]["label"].startswith("Murmaider"), str(opts))


def test_wrong_artist_is_not_substituted():
    """The Vanessa Black case: she exists, that song doesn't, and free-text
    search happily offers a different artist instead."""
    print("\na miss must not become someone else's song:")
    s = controller(
        filtered=[],                                   # no such track by her
        plain=[track("Friday Night", "Eric Paslay"),   # ...what Spotify offers
               track("Doowutchyalike", "Digital Underground")],
    )
    opts, note = s.search_options("song friday by vanessa black")
    check("nothing returned", opts == [], str(opts))
    check("no misleading note", note is None, str(note))


def test_misspelled_artist_still_matches():
    print("\na misspelled artist still matches its real one:")
    s = controller(
        filtered=[],
        plain=[track("1234 1234", "Streetlight Manifesto")],
    )
    opts, _ = s.search_options("song 1234 1234 by streeghtlight manifesto")
    check("kept the near-match", opts and "Streetlight" in opts[0]["label"], str(opts))


def test_artist_match_helper():
    print("\nartist matching is loose but not blind:")
    m = SpotifyController._artist_matches
    cases = [
        ("Streetlight Manifesto", "streeghtlight manifesto", True),
        ("Ben E. King", "ben e king", True),
        ("One True God, Cxssidy", "one true god", True),
        ("Eric Paslay", "vanessa black", False),
        ("Digital Underground", "vanessa black", False),
        ("System Of A Down", "system of a down", True),
    ]
    for names, wanted, expected in cases:
        item = {"artists": [{"name": n.strip()} for n in names.split(",")]}
        got = m(item, wanted)
        check(f"{wanted!r} vs {names!r} -> {expected}", got is expected, str(got))


def test_unusable_uris_are_refused():
    """A missing or invented uri must not reach the Spotify API.

    "tell me a poem" produced a music_play_uri call carrying a label and no uri
    at all — the model invented a pick rather than taking one from a picker.
    The empty string fell through to start_playback(context_uri="") and came
    back 400 "Invalid context uri", logging a traceback for a bad argument.
    """
    print("\nunusable Spotify uris are refused before the API:")
    import spotify as spotify_mod

    for bad in ("", None, "   ", "The Road Not Taken", "spotify:track:",
                "spotify:track:short", "spotify:banana:4uLU6hMCjMI75M1A2tKUQC",
                "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"):
        check(f"refused {bad!r}", not spotify_mod.playable_uri(bad))

    for good in ("spotify:track:4uLU6hMCjMI75M1A2tKUQC",
                 "spotify:album:4uLU6hMCjMI75M1A2tKUQC",
                 "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"):
        check(f"accepted {good[:36]!r}", spotify_mod.playable_uri(good))


def main():
    test_unusable_uris_are_refused()
    test_by_becomes_a_field_filter()
    test_splits_on_the_last_by()
    test_no_by_is_untouched()
    test_wrong_artist_is_not_substituted()
    test_misspelled_artist_still_matches()
    test_artist_match_helper()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
