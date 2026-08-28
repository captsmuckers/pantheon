"""play_uri interrupts playback, but shouldn't silently wipe what was
queued: it reads the queue first and re-adds it after. No network — sp is a
minimal fake standing in for spotipy's client.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spotify import SpotifyController  # noqa: E402

PASS = []

# _PLAYABLE_URI requires a real-looking (16+ char) base62 id, so short fake
# ids like "a"/"b" get refused before any of this logic runs.
NEW = "spotify:track:1111111111111111"
SOLO = "spotify:track:5555555555555555"
TRACK_A = "spotify:track:2222222222222222"
TRACK_B = "spotify:track:3333333333333333"
TRACK_C = "spotify:track:4444444444444444"


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


class FakeSp:
    def __init__(self, queued_uris=(), fail_start=None, fail_queue_after=None):
        self.calls = []
        self._queued = [{"uri": u} for u in queued_uris]
        self._fail_start = fail_start  # an Exception to raise from start_playback
        self._fail_queue_after = fail_queue_after  # int: add_to_queue calls before failing

    def queue(self):
        self.calls.append("queue")
        return {"queue": self._queued}

    def start_playback(self, device_id=None, uris=None, context_uri=None):
        self.calls.append(("start_playback", uris, context_uri))
        if self._fail_start:
            raise self._fail_start

    def add_to_queue(self, uri, device_id=None):
        n = sum(1 for c in self.calls if isinstance(c, tuple) and c[0] == "add_to_queue")
        if self._fail_queue_after is not None and n >= self._fail_queue_after:
            raise RuntimeError("queue add failed")
        self.calls.append(("add_to_queue", uri))


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


def test_queue_preserved_across_an_interrupting_play():
    print("an interrupting play keeps what was already queued:")
    sp = FakeSp(queued_uris=[TRACK_A, TRACK_B])
    s = controller(sp)

    result = s.play_uri(NEW, "New Song", "track")

    check("played the requested track",
          ("start_playback", [NEW], None) in sp.calls, str(sp.calls))
    check("queue read before interrupting",
          sp.calls.index("queue") < sp.calls.index(("start_playback", [NEW], None)),
          str(sp.calls))
    check("both preserved tracks re-added",
          ("add_to_queue", TRACK_A) in sp.calls and ("add_to_queue", TRACK_B) in sp.calls,
          str(sp.calls))
    check("re-added after the interrupting play started",
          sp.calls.index(("add_to_queue", TRACK_A)) > sp.calls.index(("start_playback", [NEW], None)),
          str(sp.calls))
    check("says what happened", "Kept 2 queued tracks" in result, result)
    check("still announces the new track", "New Song" in result, result)


def test_nothing_queued_means_no_note():
    print("\nnothing queued -> plain play, no extra note:")
    sp = FakeSp(queued_uris=[])
    s = controller(sp)
    result = s.play_uri(SOLO, "Solo Song", "track")
    check("no 'kept' note when there was nothing to keep", "Kept" not in result, result)
    check("normal play still works", "Solo Song" in result, result)


def test_failed_playback_does_not_touch_the_queue():
    print("\na failed play doesn't try to restore anything:")
    sp = FakeSp(queued_uris=[TRACK_A], fail_start=Exception("boom"))
    s = controller(sp)
    result = s.play_uri(NEW, "New Song", "track")
    check("reports the failure", "Couldn't start" in result, result)
    check("never tried to re-add anything",
          not any(isinstance(c, tuple) and c[0] == "add_to_queue" for c in sp.calls), str(sp.calls))


def test_premium_required_403_does_not_touch_the_queue():
    print("\na genuine Premium-required 403 doesn't try to restore anything either:")
    sp = FakeSp(queued_uris=[TRACK_A],
                fail_start=Exception("403: Player command failed: Premium required"))
    s = controller(sp)
    result = s.play_uri(NEW, "New Song", "track")
    check("reports needing Premium", "Premium" in result, result)
    check("never tried to re-add anything",
          not any(isinstance(c, tuple) and c[0] == "add_to_queue" for c in sp.calls), str(sp.calls))
    check("premium flag actually latched", s.premium is False)


def test_non_premium_403_does_not_disable_premium_features():
    """The actual live bug, 2026-08-14: a 403 for an unrelated reason
    ("Player command failed: Restriction violated" — no active device,
    not a plan issue) was treated the same as a genuine Premium-required
    403, latching self.premium = False and silently breaking every
    premium-gated feature (queueing included) for the rest of the running
    process on a genuinely Premium account.
    """
    print("\na 403 that ISN'T about Premium must not disable Premium features:")
    sp = FakeSp(queued_uris=[TRACK_A],
                fail_start=Exception("403: Player command failed: Restriction violated"))
    s = controller(sp)
    result = s.play_uri(NEW, "New Song", "track")
    check("does not falsely claim a Premium problem", "Premium" not in result, result)
    check("premium flag left alone", s.premium is True, str(s.premium))


def test_partial_restore_is_reported_honestly():
    print("\nif restoring stops partway, the count reflects what actually landed:")
    sp = FakeSp(queued_uris=[TRACK_A, TRACK_B, TRACK_C], fail_queue_after=1)
    s = controller(sp)
    result = s.play_uri(NEW, "New Song", "track")
    check("only the tracks that actually got re-added are counted",
          "Kept 1 queued track" in result, result)


def test_queue_read_failure_is_survivable():
    print("\ncan't read the queue -> play still goes through, just nothing to restore:")
    sp = FakeSp(queued_uris=[TRACK_A])
    sp.queue = lambda: (_ for _ in ()).throw(RuntimeError("network blip"))
    s = controller(sp)
    result = s.play_uri(NEW, "New Song", "track")
    check("play still succeeds", "New Song" in result, result)
    check("no false claim of preserving anything", "Kept" not in result, result)


def test_duplicate_queue_entries_do_not_compound():
    """The actual live bug, 2026-08-14: GET /me/player/queue itself
    returned the same track repeated 9 times (Spotify padding a thin
    queue, not anything this bot added). Faithfully re-adding every
    entry compounded on each interrupting play — nine copies became
    dozens after a couple of "play X" calls. Confirmed live: the fix is
    deduplicating the snapshot, not the restore.
    """
    print("\na Spotify queue response padded with duplicates doesn't compound:")
    sp = FakeSp(queued_uris=[TRACK_A, TRACK_A, TRACK_A, TRACK_B, TRACK_A])
    s = controller(sp)
    result = s.play_uri(NEW, "New Song", "track")
    adds = [c[1] for c in sp.calls if isinstance(c, tuple) and c[0] == "add_to_queue"]
    check("each track re-added only once", adds.count(TRACK_A) == 1, str(adds))
    check("unrelated track unaffected", adds.count(TRACK_B) == 1, str(adds))
    check("count in the reply matches what was actually deduplicated",
          "Kept 2 queued tracks" in result, result)

    # And it must not compound across repeated interrupting plays either —
    # this is what actually happened live: each successive "play X" mirrored
    # in whatever duplicates the previous restore had already re-added.
    sp2 = FakeSp(queued_uris=[TRACK_A] * 9)
    s2 = controller(sp2)
    s2.play_uri(NEW, "New Song", "track")
    adds2 = [c[1] for c in sp2.calls if isinstance(c, tuple) and c[0] == "add_to_queue"]
    check("nine duplicate entries collapse to one add, not nine",
          adds2 == [TRACK_A], str(adds2))


def test_the_just_played_track_is_never_restored():
    print("\nthe track just played never gets re-added to its own queue (defence in depth):")
    sp = FakeSp(queued_uris=[NEW, TRACK_A])  # NEW already present in the "old" queue somehow
    s = controller(sp)
    result = s.play_uri(NEW, "New Song", "track")
    adds = [c[1] for c in sp.calls if isinstance(c, tuple) and c[0] == "add_to_queue"]
    check("the just-played uri is excluded from restoration", NEW not in adds, str(adds))
    check("other tracks still restored", TRACK_A in adds, str(adds))


def main():
    test_queue_preserved_across_an_interrupting_play()
    test_nothing_queued_means_no_note()
    test_failed_playback_does_not_touch_the_queue()
    test_premium_required_403_does_not_touch_the_queue()
    test_non_premium_403_does_not_disable_premium_features()
    test_partial_restore_is_reported_honestly()
    test_queue_read_failure_is_survivable()
    test_duplicate_queue_entries_do_not_compound()
    test_the_just_played_track_is_never_restored()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


main()
