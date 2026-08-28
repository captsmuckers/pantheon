"""Speaker attribution: matching Discord's speaking frames to an utterance.

No Discord connection and no audio. The tracker's interval bookkeeping and
the overlap rule are plain data, and they are the parts that can be wrong in
ways nobody notices — a mis-attributed line in the tuning log still looks
like a perfectly good log line.

The connection itself is not tested here. It cannot be exercised offline,
which is exactly why everything that CAN be decided without it lives in
functions that take timestamps rather than sockets.
"""

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import speakers  # noqa: E402

failures = []


def check(label, ok, detail=""):
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}   {detail}")
        failures.append(label)


class FakeUser:
    def __init__(self, name):
        self.display_name = name


class FakeBot:
    """Just enough of a bot to resolve ids to names."""

    def __init__(self, users):
        self._users = users

    def get_user(self, user_id):
        return self._users.get(user_id)


def tracker(users=None):
    t = speakers.SpeakerTracker(FakeBot(users or {1: FakeUser("Ada"),
                                                 2: FakeUser("Grace")}))
    # who_spoke() returns None unless connected, since with no connection
    # there are no frames and every answer would be a lie by omission.
    t.voice_client = object()
    return t


def test_overlap_rule():
    print("\nthe overlap rule, with tolerance either side:")
    check("plainly inside", speakers.overlaps((10.0, 12.0), 9.0, 13.0, 0.0))
    check("plainly outside", not speakers.overlaps((10.0, 12.0), 20.0, 21.0, 0.0))
    check("touching counts", speakers.overlaps((10.0, 12.0), 12.0, 14.0, 0.0))

    # The audio and gateway paths have independent latency, so a frame that
    # falls just outside the utterance is still the same speech.
    check("just misses without tolerance",
          not speakers.overlaps((10.0, 11.0), 11.5, 13.0, 0.0))
    check("caught with tolerance",
          speakers.overlaps((10.0, 11.0), 11.5, 13.0, 1.5))


def test_names_the_speaker():
    print("\nnaming whoever was talking:")
    t = tracker()
    t._spoken = [(1, 100.0, 104.0)]
    check("names the one speaker", t.who_spoke(100.5, 103.0) == "Ada",
          t.who_spoke(100.5, 103.0))
    check("silence attributed to nobody", t.who_spoke(200.0, 201.0) is None,
          t.who_spoke(200.0, 201.0))


def test_most_overlap_wins():
    """Two people talking produces one utterance and both names.

    Ordered by how much of the utterance each covered, because with an
    interjection over someone's command the longer speaker is the likelier
    author of the words Whisper actually transcribed.
    """
    print("\ntalking over each other:")
    t = tracker()
    t._spoken = [(1, 100.0, 110.0), (2, 109.0, 109.4)]
    got = t.who_spoke(100.0, 110.0)
    check("both named", got == "Ada, Grace", got)


def test_open_intervals_count():
    """Someone mid-sentence is exactly who is being transcribed.

    Not every client sends the stop frame, so an interval can still be open
    when the utterance is handled. Ignoring those would leave the most
    common case — one person talking right now — unattributed.
    """
    print("\nstill talking:")
    t = tracker()
    import time

    now = time.time()
    t._open = {1: now - 2.0}
    got = t.who_spoke(now - 1.5, now - 0.2)
    check("an unclosed interval still names them", got == "Ada", got)

    # ...but not forever. A start frame whose stop never arrived would
    # otherwise claim every utterance for the rest of the session.
    t._open = {1: now - (speakers._MAX_OPEN_S + 60)}
    check("a stale one is ignored", t.who_spoke(now - 1.0, now) is None)


def test_unknown_user_falls_back_to_id():
    print("\na speaker the bot cannot resolve:")
    t = speakers.SpeakerTracker(FakeBot({}))
    t.voice_client = object()
    t._spoken = [(4242, 10.0, 12.0)]
    got = t.who_spoke(10.0, 12.0)
    check("logs the id rather than nothing", got == "4242", got)


def test_frames_build_intervals():
    """The bookkeeping between a start frame and its stop."""
    print("\nturning frames into intervals:")
    t = tracker()
    t._begin(1)
    check("start opens an interval", 1 in t._open)
    t._end(1)
    check("stop closes it", not t._open and len(t._spoken) == 1)

    # A stop with no matching start is not an interval of unknown length —
    # it is nothing, and recording it would invent one.
    t._end(99)
    check("an unmatched stop is ignored", len(t._spoken) == 1)


def test_disconnected_tracker_says_nothing():
    print("\nwith no voice connection:")
    t = speakers.SpeakerTracker(FakeBot({}))
    t._spoken = [(1, 10.0, 12.0)]
    check("returns None rather than guessing", t.who_spoke(10.0, 12.0) is None)


if __name__ == "__main__":
    print(f"tolerance = {config.VOICE_SPEAKER_TOLERANCE_MS}ms")
    test_overlap_rule()
    test_names_the_speaker()
    test_most_overlap_wins()
    test_open_intervals_count()
    test_unknown_user_falls_back_to_id()
    test_frames_build_intervals()
    test_disconnected_tracker_says_nothing()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("speaker attribution checks passed")
