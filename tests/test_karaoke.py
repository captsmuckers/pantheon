"""LRC/enhanced-LRC parsing and the karaoke overlay builder — no Spotify/mpv
or network needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lyrics import Karaoke, parse_enhanced_lrc, parse_lrc  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_parse_lrc():
    print("plain LRC parsing:")
    lines = parse_lrc("[00:12.50]hello world\n[00:15.00]second line\nnot a lyric line")
    check("two stamped lines parsed", len(lines) == 2, str(lines))
    check("timestamp with centiseconds", lines[0][0] == 12.5, str(lines[0]))
    check("sorted by time", lines[0][0] < lines[1][0], str(lines))
    check("unstamped line dropped", "not a lyric line" not in [t for _, t in lines])

    multi = parse_lrc("[00:10.00][00:20.00]repeated line")
    check("a line with two stamps becomes two entries", len(multi) == 2, str(multi))


def test_parse_enhanced_lrc():
    print("\nMusixmatch enhanced (word-level) LRC parsing:")
    text = "[00:09.73] <00:09.73> If <00:09.86> you <00:10.21> run \n[00:11.00] <00:11.00> away "
    parsed = parse_enhanced_lrc(text)
    check("two lines parsed", len(parsed) == 2, str(parsed))
    line_stamp, full_text, words = parsed[0]
    check("line stamp correct", line_stamp == 9.73, str(line_stamp))
    check("full text joins the words", full_text == "If you run", full_text)
    check("three words with real timestamps",
          words == [(9.73, "If"), (9.86, "you"), (10.21, "run")], str(words))

    check("plain LRC (no word tags) returns None", parse_enhanced_lrc(
        "[00:12.50]hello world") is None)
    check("empty text returns None", parse_enhanced_lrc("") is None)
    check("garbage with a stray < returns None, not a crash",
          parse_enhanced_lrc("this < is not lrc at all") is None)


def test_word_fill_uses_real_gaps():
    print("\n_word_fill() times each word by the gap to the NEXT real timestamp:")
    k = Karaoke(None, None)
    # "hi" gets 1s (10.0 -> 11.0), "there" gets 2s (11.0 -> 13.0, the next line)
    k._lines = [(10.0, "hi there", [(10.0, "hi"), (11.0, "there")]), (13.0, "next line", None)]

    out = k._word_fill(0, position=10.0)  # right at the line's start
    parts = out.split("{\\k")
    hi_cs = int(parts[1].split("}")[0])
    there_cs = int(parts[2].split("}")[0])
    check("both words present", "hi" in out and "there" in out, out)
    check("'there' (2s gap) gets a bigger duration than 'hi' (1s gap)",
          there_cs > hi_cs, f"hi={hi_cs} there={there_cs}")

    # Fully past the line: both words already "sung".
    out = k._word_fill(0, position=20.0)
    check("fully elapsed -> both \\k0", out.count(r"{\k0}") == 2, out)


def test_word_fill_last_line_caps():
    print("\n_word_fill() caps the last word when there's no next line to bound it:")
    k = Karaoke(None, None)
    k._lines = [(0.0, "only word", [(0.0, "only")])]
    out = k._word_fill(0, position=0.0)
    cs = int(out.split("{\\k")[1].split("}")[0])
    from lyrics import MAX_LAST_WORD_SECONDS
    check("capped at MAX_LAST_WORD_SECONDS, not left unbounded",
          cs == round(MAX_LAST_WORD_SECONDS * 100), f"cs={cs}")


def test_build_only_fires_on_a_line_change():
    print("\n_build() only rebuilds when the visible lyric line changes:")
    k = Karaoke(None, None)
    k._lines = [(0.0, "one", None), (2.0, "two", None), (4.0, "three", None)]
    k._track_id = "abc"

    first = k._build(0.5)
    check("first call on line 0 builds something", first is not None, str(first))
    check("current line's text is present", "one" in first, first)

    again = k._build(1.5)
    check("same line: no rebuild", again is None, str(again))

    changed = k._build(2.1)
    check("next line triggers a rebuild", changed is not None, str(changed))
    check("shows the new current line", "two" in changed, changed)

    same_again = k._build(2.1)
    check("nothing changed this time: no rebuild", same_again is None, str(same_again))


def test_build_uses_word_fill_when_available():
    print("\n_build() uses real \\k fill for a word-synced line, flat highlight otherwise:")
    k = Karaoke(None, None)
    k._lines = [(0.0, "synced line", [(0.0, "synced"), (0.5, "line")]),
                (3.0, "plain line", None)]
    k._track_id = "abc"

    synced = k._build(0.1)
    check("word-synced line gets \\k tags", r"{\k" in synced, synced)

    plain = k._build(3.1)
    check("plain line gets the flat highlight instead", r"{\c&H00FFFF&}plain line" in plain, plain)
    check("plain line has no \\k tags", r"{\k" not in plain, plain)


def test_build_before_first_line():
    print("\nbefore the first lyric line:")
    k = Karaoke(None, None)
    k._lines = [(5.0, "first line", None)]
    k._track_id = "abc"

    pre = k._build(1.0)
    check("no visible text before the first line — style tags render nothing on their own",
          pre is not None and "first line" not in pre, repr(pre))
    again = k._build(2.0)
    check("still before it: no rebuild", again is None, str(again))


def test_build_no_synced_lyrics():
    print("\na track with no synced lyrics at all:")
    k = Karaoke(None, None)
    k._track_id = "abc"
    k._label = "Some Obscure Remix — DJ Nobody"
    k._lines = []

    out = k._build(10.0)
    check("names the track", "Some Obscure Remix" in out, out)
    check("says lyrics weren't found", "no synced lyrics" in out.lower(), out)


def test_build_before_any_resync():
    print("\nbefore the very first resync (nothing known yet):")
    k = Karaoke(None, None)
    out = k._build(0.0)
    check("blank, doesn't claim 'no lyrics' prematurely",
          "no synced lyrics" not in (out or "").lower(), repr(out))


def test_build_uses_ass_linebreaks():
    print("\n_build() uses \\N, not a literal newline (real ASS text ignores plain \\n):")
    k = Karaoke(None, None)
    k._lines = [(0.0, "hello there world", None), (3.0, "next line", None)]
    k._track_id = "abc"

    text = k._build(1.0)
    check("rows joined with the ASS line break", r"\N" in text, text)
    check("no literal newline characters", "\n" not in text, repr(text))


def test_queue_overlay_is_a_numbered_vertical_list():
    print("\n_build_queue_overlay(): a numbered, top-left, vertical list:")
    k = Karaoke(None, None)
    k._queue = [f"Track {i}" for i in range(5)]

    out = k._build_queue_overlay()
    check("top-left alignment", r"{\an7" in out, out)
    check("all 5 present, numbered", all(f"{i + 1}. Track {i}" in out for i in range(5)), out)
    check("has a header", "Up Next" in out, out)
    check("vertical — one per line via \\N", out.count(r"\N") >= 5, out)

    k2 = Karaoke(None, None)
    k2._queue = []
    check("empty overlay when nothing's queued", k2._build_queue_overlay() == "")


def test_nowplaying_overlay():
    print("\n_build_nowplaying(): top-right label:")
    k = Karaoke(None, None)
    k._label = "Karma Police — Radiohead"
    out = k._build_nowplaying()
    check("top-right alignment", r"{\an9" in out, out)
    check("names the track", "Now Playing: Karma Police — Radiohead" in out, out)

    k2 = Karaoke(None, None)
    check("empty before anything's identified", k2._build_nowplaying() == "")


class FakeSp:
    def __init__(self, tracks):
        self._tracks = tracks

    def queue(self):
        return {"queue": self._tracks}


class FakeSpotify:
    def __init__(self, tracks):
        self.sp = FakeSp(tracks)

    @staticmethod
    def _label(item, kind):
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        return f"{item['name']} — {artists}" if artists else item["name"]


def test_fetch_queue_caps_at_five():
    print("\n_fetch_queue() caps at QUEUE_DISPLAY_LIMIT even if Spotify returns more:")
    tracks = [{"name": f"Track {i}", "artists": [{"name": "Someone"}]} for i in range(10)]
    spot = FakeSpotify(tracks)
    k = Karaoke(spot, None)
    labels = k._fetch_queue()
    check("capped at 5", len(labels) == 5, str(labels))
    check("formatted with the artist", labels[0] == "Track 0 — Someone", labels[0])


def test_fetch_queue_no_spotify():
    print("\n_fetch_queue() with no Spotify configured:")
    k = Karaoke(None, None)
    check("empty, not an error", k._fetch_queue() == [])


def test_fetch_lines_prefers_enhanced_falls_back_to_lrclib():
    print("\n_fetch_lines() tries enhanced first, falls back to LRCLIB:")
    k = Karaoke(None, None)

    k._fetch_lines.__func__  # sanity: it's an instance method we can monkeypatch around
    import lyrics as lyrics_mod
    original_enhanced = lyrics_mod.fetch_enhanced
    original_lrc = lyrics_mod.fetch_synced
    try:
        lyrics_mod.fetch_enhanced = lambda artist, track: (
            "[00:01.00] <00:01.00> hi "
        )
        lines = k._fetch_lines("A", "T", "Al", 100)
        check("used the enhanced result", lines and lines[0][2] is not None, str(lines))

        lyrics_mod.fetch_enhanced = lambda artist, track: None
        lyrics_mod.fetch_synced = lambda artist, track, album, duration: "[00:02.00]plain line"
        lines = k._fetch_lines("A", "T", "Al", 100)
        check("fell back to LRCLIB when enhanced had nothing",
              lines == [(2.0, "plain line", None)], str(lines))

        lyrics_mod.fetch_synced = lambda artist, track, album, duration: None
        lines = k._fetch_lines("A", "T", "Al", 100)
        check("empty when neither source has anything", lines == [], str(lines))
    finally:
        lyrics_mod.fetch_enhanced = original_enhanced
        lyrics_mod.fetch_synced = original_lrc


def main():
    test_parse_lrc()
    test_parse_enhanced_lrc()
    test_word_fill_uses_real_gaps()
    test_word_fill_last_line_caps()
    test_build_only_fires_on_a_line_change()
    test_build_uses_word_fill_when_available()
    test_build_before_first_line()
    test_build_no_synced_lyrics()
    test_build_before_any_resync()
    test_build_uses_ass_linebreaks()
    test_queue_overlay_is_a_numbered_vertical_list()
    test_nowplaying_overlay()
    test_fetch_queue_caps_at_five()
    test_fetch_queue_no_spotify()
    test_fetch_lines_prefers_enhanced_falls_back_to_lrclib()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
