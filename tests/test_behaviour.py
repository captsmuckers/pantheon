"""Source handoff, queueing while parked, and radio-vs-title precedence."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import FakeItem, FakeLib, FakeSpotify, make_player  # noqa: E402

import wm  # noqa: E402
from brain import Controls, Choice, fast_match  # noqa: E402
from library import TitleEntry  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


class SearchableLib(FakeLib):
    """FakeLib plus the title-search surface Controls uses."""

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


def controls_with(items, titles=(), spotify=None):
    lib = SearchableLib(items, titles)
    player = make_player(lib)
    return Controls(player, lib, spotify if spotify is not None else FakeSpotify()), player, lib


async def test_failed_lookup_leaves_music_alone():
    print("a miss must not stop the music:")
    spot = FakeSpotify()
    controls, player, _ = controls_with([FakeItem(1, "Dune")], [(1, "Dune", "movie", 2021)],
                                        spotify=spot)
    controls.active = "spotify"
    result = await controls.fast("play", query="zzzz nonexistent zzzz")
    check("reports the miss", "Couldn't find" in result, result)
    check("Spotify untouched", spot.paused == 0, f"pauses={spot.paused}")
    check("source still music", controls.active == "spotify", controls.active)

    result = await controls.fast("play", query="Dune")
    check("a real title does switch", "Playing" in result, result)
    check("and pauses the music", spot.paused == 1, f"pauses={spot.paused}")


async def test_ambiguity_leaves_music_alone():
    print("\nan ambiguous title must not stop the music either:")
    spot = FakeSpotify()
    controls, player, lib = controls_with([FakeItem(1, "The Office")], [])
    controls.spotify = spot
    controls.active = "spotify"

    two = [TitleEntry(1, "The Office", "show", 2005), TitleEntry(2, "The Office", "show", 2026)]
    lib.resolve_query = lambda q: (None, two)
    result = await controls.fast("play", query="the office")
    check("returns a picker", isinstance(result, Choice), type(result).__name__)
    check("Spotify untouched", spot.paused == 0, f"pauses={spot.paused}")


async def test_queue_while_parked():
    print("\nqueueing while music plays:")
    a, b = FakeItem(1, "A"), FakeItem(2, "B")
    controls, player, _ = controls_with([a, b], [(1, "A", "movie", 2020), (2, "B", "movie", 2021)])

    await player.play(a)
    parked = await player.park()
    check("video parked", parked is not None and player.has_parked)
    check("would_start_now is False while parked", player.would_start_now is False)

    result = await player.queue_add(b)
    check("queued rather than played", player.current is None, f"current={player.current}")
    check("queue holds it", player.queue == [2], f"queue={player.queue}")
    check("mentions resume video", "resume video" in result, result)

    result = await player.unpark()
    check("unpark resumes the film", player.current is a, f"current={player.current}")
    check("queued item still waiting", player.queue == [2], f"queue={player.queue}")


async def test_queue_when_idle_starts():
    print("\nqueueing with nothing on:")
    a = FakeItem(1, "A")
    controls, player, _ = controls_with([a], [(1, "A", "movie", 2020)])
    check("would_start_now is True when idle", player.would_start_now is True)
    result = await player.queue_add(a)
    check("starts immediately", player.current is a, f"current={player.current}")
    check("says playing", "Playing" in result, result)


async def test_radio_vs_title():
    print("\nradio phrasing that is really a film:")
    film = FakeItem(1, "Pirate Radio")
    spot = FakeSpotify()
    controls, player, _ = controls_with([film], [(1, "Pirate Radio", "movie", 2009)],
                                        spotify=spot)

    action, kwargs = fast_match("play pirate radio")
    check("fast path routes to radio", action == "radio", action)
    check("original text carried through", kwargs.get("text") == "play pirate radio")

    result = await controls.fast(action, **kwargs)
    check("library wins", player.current is film, f"current={player.current}")
    check("no Spotify playback", spot.played == [], f"played={spot.played}")
    check("says it's playing", "Playing" in result, result)


async def test_radio_still_works():
    print("\nradio phrasing that is not a film:")
    spot = FakeSpotify()
    controls, player, _ = controls_with([], [(1, "Pirate Radio", "movie", 2009)], spotify=spot)
    started = []
    controls.start_radio = lambda seed: asyncio.sleep(0, result=f"radio:{seed}") \
        if started.append(seed) is None else None

    action, kwargs = fast_match("system of a down radio")
    result = await controls.fast(action, **kwargs)
    check("goes to Spotify radio", started == ["system of a down"], f"seeds={started}")
    check("nothing played from Plex", player.current is None)
    check("result is the radio result", result == "radio:system of a down", str(result))


async def test_music_keyword_still_works():
    print("\nexplicit music requests are untouched:")
    controls, player, _ = controls_with([], [(1, "Music Box", "movie", 1989)])
    asked = []
    controls.start_music = lambda q: asyncio.sleep(0, result=f"music:{q}") \
        if asked.append(q) is None else None

    action, kwargs = fast_match("music radiohead")
    result = await controls.fast(action, **kwargs)
    check("searched Spotify", asked == ["radiohead"], f"asked={asked}")
    check("nothing played from Plex", player.current is None)

    # ...but "play music box" is the film.
    film = FakeItem(1, "Music Box")
    controls2, player2, _ = controls_with([film], [(1, "Music Box", "movie", 1989)])
    action, kwargs = fast_match("play music box")
    await controls2.fast(action, **kwargs)
    check("'play music box' is the film", player2.current is film, f"current={player2.current}")


async def test_spotify_connecting_message():
    print("\nSpotify still connecting at startup:")
    controls, _, _ = controls_with([], [], spotify=FakeSpotify(enabled=True, connected=False))
    result = await controls.start_music("radiohead")
    check("distinguishes connecting from unconfigured",
          "still connecting" in result, result)

    controls2, _, _ = controls_with([], [], spotify=FakeSpotify(enabled=False))
    result = await controls2.start_music("radiohead")
    check("unconfigured says so", "isn't set up" in result, result)


async def test_parked_video_is_named_and_cleared():
    """The Dune bug: park a film, play something else, ask to go back.

    The model can only choose resume_video over a fresh search if the state
    tells it *which* video is waiting.
    """
    print("\nparked video is identifiable:")
    dune, other = FakeItem(1, "Dune"), FakeItem(2, "Other Film")
    controls, player, _ = controls_with(
        [dune, other], [(1, "Dune", "movie", 2021), (2, "Other Film", "movie", 1999)])

    await player.play(dune)
    player.position = 754.0
    await player.park()

    state = controls.state()
    check("state flags a parked video", state["video_parked"] is True)
    check("state names it", state.get("parked_video") and "Dune" in state["parked_video"],
          str(state.get("parked_video")))
    check("and gives the position", "12:34" in (state.get("parked_video") or ""),
          str(state.get("parked_video")))

    # Playing something else must not leave a stale park behind.
    await player.play(other)
    check("park cleared once another video starts", player.has_parked is False)
    check("state agrees", controls.state()["parked_video"] is None)
    result = await player.unpark()
    check("nothing stale to resume", "Nothing was paused" in result, result)

    # And the normal route still restores the right film at its position.
    await player.play(dune)
    player.position = 754.0
    await player.park()
    await player.unpark()
    check("unpark restores the parked film", player.current is dune, f"{player.current}")
    check("near the saved position", 740 <= player.position <= 754, f"{player.position}")


async def test_swapping_back_to_music_actually_resumes():
    """The reported bug: "go back to the music" paused the film and stopped.

    The phrase reached the model (no fast path had it), and the model did the
    obvious half of the job. Two defences now: the fast path handles it without
    a model at all, and the state names what is paused so the model has
    something to act on if it ever does see it.
    """
    print("\ngoing back to the music:")
    film = FakeItem(1, "Dune")
    spot = FakeSpotify()
    controls, player, _ = controls_with([film], [(1, "Dune", "movie", 2021)], spotify=spot)

    # Play a film, swap to music, then swap back to the film.
    await player.play(film)
    spot.play_uri("spotify:track:x", "Wonderwall — Oasis", "track")
    await controls.to_music()
    await controls.fast("unpark")
    check("film is back", player.current is film, f"{player.current}")
    check("music was paused", spot.playing is False)

    # ...now ask for the music back. This is the step that failed.
    action, kwargs = fast_match("go back to the music")
    check("fast path catches it", action == "music_resume", str(action))
    result = await controls.fast(action, **kwargs)
    check("music actually resumed", spot.playing is True, f"playing={spot.playing}")
    check("source switched to spotify", controls.active == "spotify", controls.active)
    check("said something useful", "Music playing" in str(result), str(result))
    check("film was parked, not just paused", player.has_parked is True)


async def test_state_names_the_paused_music():
    print("\nstate names the paused music:")
    spot = FakeSpotify()
    controls, player, _ = controls_with([], [], spotify=spot)
    spot.play_uri("spotify:track:x", "Wonderwall — Oasis", "track")

    state = controls.state()
    check("reports playing", state["music_playing"] is True)
    check("nothing paused while playing", state["paused_music"] is None,
          str(state["paused_music"]))

    spot.pause()
    state = controls.state()
    check("reports paused", state["music_playing"] is False)
    check("names what is paused", state["paused_music"] == "Wonderwall — Oasis",
          str(state["paused_music"]))


async def test_resume_the_video_does_not_start_music():
    """The mirror defect, found while testing the fix rather than in the wild."""
    print("\n'resume the video' must not start the music:")
    film = FakeItem(1, "Dune")
    spot = FakeSpotify()
    controls, player, _ = controls_with([film], [(1, "Dune", "movie", 2021)], spotify=spot)

    await player.play(film)
    await controls.to_music()          # film parked, current is None
    check("film parked", player.has_parked is True)

    action, kwargs = fast_match("resume the video")
    check("routed to unpark, not resume", action == "unpark", str(action))
    await controls.fast(action, **kwargs)
    check("film resumed", player.current is film, f"{player.current}")
    check("music did not start", spot.playing is False, f"playing={spot.playing}")

    # And with nothing parked it just unpauses instead of erroring.
    await player.pause()
    result = await controls.fast("unpark")
    check("plain unpause when nothing parked",
          "Nothing was paused" not in str(result), str(result))


async def test_confident_titles_skip_the_model():
    """The Nacho Libre bug: a title the library resolves at 1.00 was routed to
    a 7B, which — primed by twenty minutes of music requests — called the music
    tool and then reported the film missing from a library containing it."""
    print("\nconfident titles need no model:")
    nacho = FakeItem(1, "Nacho Libre", 2006)
    controls, player, _ = controls_with(
        [nacho, FakeItem(2, "Dune", 2021)],
        [(1, "Nacho Libre", "movie", 2006), (2, "Dune", "movie", 2021)])

    result = await controls.try_direct_play("play nacho libre")
    check("resolved without the model", result is not None, str(result))
    check("played the film", player.current is nacho, f"{player.current}")
    check("said so", "Playing" in str(result), str(result))

    # Queue verb routes to queueing, not playing.
    controls2, player2, _ = controls_with(
        [nacho], [(1, "Nacho Libre", "movie", 2006)])
    await player2.play(FakeItem(9, "Something Else"))
    result = await controls2.try_direct_play("queue nacho libre")
    check("queue verb queues", "queue" in str(result).lower(), str(result))


async def test_vague_requests_still_reach_the_model():
    print("\nvague requests fall through:")
    controls, player, _ = controls_with(
        [FakeItem(1, "Nacho Libre", 2006)], [(1, "Nacho Libre", "movie", 2006)])
    for phrase in ("play something with wrestling in it",
                   "play that one mexican comedy",
                   "what should i watch tonight",
                   "play a movie about robots"):
        result = await controls.try_direct_play(phrase)
        check(f"{phrase!r} -> model", result is None, str(result))
    check("nothing was played", player.current is None, f"{player.current}")


async def test_ambiguous_titles_ask_without_the_model():
    print("\nambiguity becomes a picker, not a guess:")
    lib_titles = [(1, "The Office", "show", 2005), (2, "The Office", "show", 2026)]
    controls, player, lib = controls_with(
        [FakeItem(1, "The Office"), FakeItem(2, "The Office")], lib_titles)
    two = [TitleEntry(1, "The Office", "show", 2005),
           TitleEntry(2, "The Office", "show", 2026)]
    lib.resolve_query = lambda q: (None, two)

    result = await controls.try_direct_play("play the office")
    check("returned a picker", isinstance(result, Choice), type(result).__name__)
    check("nothing played yet", player.current is None, f"{player.current}")


async def test_spotify_fallback_for_named_artists():
    """A song request the video library can't satisfy goes to Spotify, not to
    a model that will invent a confirmation for it."""
    print("\n'<song> by <artist>' the library lacks -> Spotify:")
    spot = FakeSpotify()
    controls, player, _ = controls_with(
        [FakeItem(1, "Dune", 2021)], [(1, "Dune", "movie", 2021)], spotify=spot)

    asked = []
    controls.start_music = lambda q: asyncio.sleep(0, result=f"Song: {q}") \
        if asked.append(q) is None else None
    queued = []
    controls.queue_music = lambda q: asyncio.sleep(0, result=f"Queued: {q}") \
        if queued.append(q) is None else None

    result = await controls.try_direct_play("play friday by vanessa black")
    check("went to Spotify", asked == ["friday by vanessa black"], str(asked))
    check("did not fall through to the model", result is not None, str(result))
    check("nothing played from Plex", player.current is None)

    result = await controls.try_direct_play("queue friday by vanessa black")
    check("queue verb queues on Spotify", queued == ["friday by vanessa black"], str(queued))


async def test_library_still_wins_over_spotify():
    print("\na title the library HAS never reaches Spotify:")
    spot = FakeSpotify()
    nacho = FakeItem(1, "Nacho Libre", 2006)
    controls, player, _ = controls_with(
        [nacho], [(1, "Nacho Libre", "movie", 2006)], spotify=spot)
    asked = []
    controls.start_music = lambda q: asyncio.sleep(0, result="x") \
        if asked.append(q) is None else None

    await controls.try_direct_play("play nacho libre")
    check("played from Plex", player.current is nacho, f"{player.current}")
    check("Spotify never consulted", asked == [], str(asked))


async def test_vague_requests_do_not_hit_spotify():
    """Without a named artist there is nothing to be confident about, so these
    must still reach the model rather than playing a fuzzy Spotify guess."""
    print("\nvague requests must not be sent to Spotify:")
    spot = FakeSpotify()
    controls, player, _ = controls_with([], [], spotify=spot)
    asked = []
    controls.start_music = lambda q: asyncio.sleep(0, result="x") \
        if asked.append(q) is None else None

    for phrase in ("play something with dragons in it",
                   "play the song that goes blame it all on my boots",
                   "play a movie about robots",
                   "play something cheerful"):
        result = await controls.try_direct_play(phrase)
        check(f"{phrase[:38]!r} -> model", result is None, str(result))
    check("Spotify never consulted", asked == [], str(asked))


async def test_fallback_skipped_when_spotify_is_down():
    print("\nno Spotify configured -> falls through, does not error:")
    controls, player, _ = controls_with(
        [], [], spotify=FakeSpotify(enabled=False))
    result = await controls.try_direct_play("play friday by vanessa black")
    check("returned None for the model to try", result is None, str(result))


async def test_music_shaped_requests_skip_the_model():
    """"Queue up some EDM music" named music outright and still reached the
    model, which answered with search_library — a tool the video library can
    never satisfy. Naming music is as high-precision a signal as naming an
    artist, so it is handled the same way, after the library declines."""
    print("\nrequests that name music go to Spotify without the model:")
    controls, player, _ = controls_with([FakeItem(1, "Dune")], [(1, "Dune", "movie", 2021)])
    played, queued = [], []
    controls.start_music = lambda q: asyncio.sleep(0, result=f"Song: {q}") \
        if played.append(q) is None else None
    controls.queue_music = lambda q: asyncio.sleep(0, result=f"Queued: {q}") \
        if queued.append(q) is None else None

    await controls.try_direct_play("Queue up some EDM music")
    check("queued on Spotify", queued == ["some EDM music"], str(queued))
    await controls.try_direct_play("play some jazz")
    check("a bare genre plays on Spotify", played == ["some jazz"], str(played))
    check("nothing played from Plex", player.current is None, f"{player.current}")


async def test_doubled_verb_is_stripped_for_spotify():
    """"play play friday by vanessa black" left the second verb inside the
    query, so Spotify was searched for "play friday by vanessa black". The
    strip happens only after the library search, or the film "Play Misty for
    Me" would stop resolving."""
    print("\na doubled verb doesn't leak into the Spotify query:")
    misty = FakeItem(1, "Play Misty for Me", 1971)
    controls, player, _ = controls_with(
        [misty], [(1, "Play Misty for Me", "movie", 1971)])
    played = []
    controls.start_music = lambda q: asyncio.sleep(0, result=f"Song: {q}") \
        if played.append(q) is None else None

    await controls.try_direct_play("play play friday by vanessa black")
    check("second verb stripped", played == ["friday by vanessa black"], str(played))

    await controls.try_direct_play("play play misty for me")
    check("film with a verb in its name still wins",
          player.current is misty, f"{player.current}")
    check("Spotify not consulted for it", played == ["friday by vanessa black"], str(played))


async def test_fullscreen_is_reasserted_after_music():
    """mpv comes back from a music detour still fullscreen.

    `fullscreen` is passed once at launch and mpv is launched once and reused.
    Minimising it for Spotify and restoring the window with SW_RESTORE brings
    it back but not mpv's own fullscreen state, so a stream started after a
    music detour arrived windowed.
    """
    print("\nfullscreen survives a trip to Spotify:")
    player = make_player(FakeLib([FakeItem(1, "Dune")]))
    await player.ensure_fullscreen()
    check("fullscreen re-asserted", "yes" in [str(v).lower() for v in player.mpv.sets("fullscreen")]
          or True in player.mpv.sets("fullscreen"),
          str(player.mpv.sets("fullscreen")))


async def test_karaoke_window_swap_failure_is_reported():
    """The reported bug: "karaoke on" said it swapped when it hadn't.

    restore()/bring_to_front() can silently fail — Windows refusing focus
    to a background process is normal, not an error — and the karaoke
    branch used to report success unconditionally regardless. Measured
    live 2026-08-14: mpv ended up minimized AND holding foreground at
    once, and the reply still said "lyrics on the mpv window."
    """
    print("\nkaraoke on: a window swap that fails is reported, not hidden:")
    # The requirement is unchanged from the Windows side — a swap that did not
    # take must be said out loud, because a karaoke reply once claimed "lyrics
    # on the mpv window" while mpv sat minimised. The MECHANISM changed: mpv
    # exposes no windows to the macOS Accessibility API, so this no longer goes
    # through wm at all. Player.show_window asks mpv itself whether it is up.
    spot = FakeSpotify()
    controls, player, _ = controls_with([], [], spotify=spot)
    controls.active = "spotify"

    original_show = player.show_window
    async def show_fails():
        return False
    player.show_window = show_fails
    try:
        result = await controls.set_karaoke(True)
        check("failure surfaced to the user", "wouldn't come to the front" in result, result)
    finally:
        player.show_window = original_show
        await controls.karaoke.stop()

    async def show_works():
        return True
    player.show_window = show_works
    try:
        result = await controls.set_karaoke(True)
        check("no spurious failure note on success",
              "wouldn't come to the front" not in result, result)
        check("still gives the normal karaoke reply", "Karaoke mode on" in result, result)
    finally:
        player.show_window = original_show
        await controls.karaoke.stop()

    # The old third case patched wm.find_window to None, for "the mpv window
    # isn't there at all". That distinction no longer exists: mpv is asked
    # directly, and a dead mpv and an un-raisable one both come back as
    # show_window() returning False, which the first case already covers.


async def main():
    await test_fullscreen_is_reasserted_after_music()
    await test_karaoke_window_swap_failure_is_reported()
    await test_failed_lookup_leaves_music_alone()
    await test_ambiguity_leaves_music_alone()
    await test_queue_while_parked()
    await test_queue_when_idle_starts()
    await test_radio_vs_title()
    await test_radio_still_works()
    await test_music_keyword_still_works()
    await test_spotify_connecting_message()
    await test_parked_video_is_named_and_cleared()
    await test_swapping_back_to_music_actually_resumes()
    await test_state_names_the_paused_music()
    await test_resume_the_video_does_not_start_music()
    await test_confident_titles_skip_the_model()
    await test_vague_requests_still_reach_the_model()
    await test_ambiguous_titles_ask_without_the_model()
    await test_spotify_fallback_for_named_artists()
    await test_library_still_wins_over_spotify()
    await test_vague_requests_do_not_hit_spotify()
    await test_fallback_skipped_when_spotify_is_down()
    await test_music_shaped_requests_skip_the_model()
    await test_doubled_verb_is_stripped_for_spotify()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
