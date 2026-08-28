"""Queue advancing: entry-id correlation, the error cap, and freeze recovery."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import FakeItem, FakeLib, FakeMPV, make_player  # noqa: E402

import config  # noqa: E402
import player as player_mod  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


async def test_stale_entry_dropped():
    """An end-file for a file we already replaced must not advance the queue."""
    print("stale end-file events:")
    a, b, c = FakeItem(1, "A"), FakeItem(2, "B"), FakeItem(3, "C")
    lib = FakeLib([a, b, c])
    p = make_player(lib)
    await p.play(a)
    entry_of_a = p._entry_id
    p.queue = [2, 3]

    # A skip loads B, so A's entry is superseded...
    await p.skip()
    check("skip advanced to B", p.current is b, f"current={p.current}")
    check("entry id moved on", p._entry_id != entry_of_a)

    # ...and A's late eof must now be ignored rather than popping C as well.
    await p._on_playback_finished("eof", entry_id=entry_of_a)
    check("late eof for A ignored", p.current is b, f"current={p.current}")
    check("C still queued", p.queue == [3], f"queue={p.queue}")

    # A current eof does advance.
    await p._on_playback_finished("eof", entry_id=p._entry_id)
    check("current eof advances to C", p.current is c, f"current={p.current}")


async def test_missing_entry_id_still_advances():
    """Older mpv omits playlist_entry_id; behaviour must not regress to inert."""
    print("\nmpv without playlist_entry_id:")
    a, b = FakeItem(1, "A"), FakeItem(2, "B")
    p = make_player(FakeLib([a, b]))
    await p.play(a)
    p._entry_id = None          # as an older build would leave it
    p.queue = [2]
    await p._on_playback_finished("eof", entry_id=None)
    check("still advances", p.current is b, f"current={p.current}")


async def test_error_cascade_capped():
    """Three failed loads in a row stops, instead of walking a whole series."""
    print("\nunplayable files:")
    items = [FakeItem(i, f"Ep{i}") for i in range(1, 12)]
    p = make_player(FakeLib(items))
    notices = []
    p.on_notice = lambda m: notices.append(m) or asyncio.sleep(0)
    await p.play(items[0])
    p.queue = [i.ratingKey for i in items[1:]]

    for _ in range(player_mod.MAX_CONSECUTIVE_ERRORS):
        await p._on_playback_finished("error", entry_id=p._entry_id,
                                      file_error="loading failed")

    check("stopped after the cap", p.current is None, f"current={p.current}")
    check("queue cleared", p.queue == [], f"queue={p.queue}")
    check("told someone", len(notices) == 1, f"notices={len(notices)}")
    check("notice explains why", notices and "failed to play" in notices[0])
    # The point of the cap: a handful of load attempts, not all eleven. Counted
    # as loadfiles issued, since giving up clears the queue behind us.
    attempts = len(p.mpv.loads)
    check(f"only {attempts} load attempts, not 11",
          attempts <= player_mod.MAX_CONSECUTIVE_ERRORS + 1, f"loads={attempts}")


async def test_error_counter_resets():
    """One bad file among good ones must not eventually trip the cap."""
    print("\nintermittent failures:")
    items = [FakeItem(i, f"Ep{i}") for i in range(1, 9)]
    p = make_player(FakeLib(items))
    notices = []
    p.on_notice = lambda m: notices.append(m) or asyncio.sleep(0)
    await p.play(items[0])
    p.queue = [i.ratingKey for i in items[1:]]

    for _ in range(4):
        await p._on_playback_finished("error", entry_id=p._entry_id)
        check("  survived one error", p.current is not None)
        await p._after_load()      # the replacement opened fine
    check("never gave up", p.current is not None and not notices)


async def test_freeze_reload_capped():
    """A stream that never progresses gets abandoned, not retried forever."""
    print("\nfrozen playback:")
    item = FakeItem(1, "Stuck")
    p = make_player(FakeLib([item]))
    notices = []
    p.on_notice = lambda m: notices.append(m) or asyncio.sleep(0)
    await p.play(item)

    original_timeout = config.FREEZE_TIMEOUT
    original_interval = player_mod.WATCHDOG_INTERVAL
    config.FREEZE_TIMEOUT = 0            # every pass counts as a stall
    player_mod.WATCHDOG_INTERVAL = 0.02  # ...and passes come fast
    try:
        task = asyncio.get_running_loop().create_task(p._watchdog_loop())
        for _ in range(200):
            await asyncio.sleep(0.02)
            if p.current is None:
                break
        task.cancel()
    finally:
        config.FREEZE_TIMEOUT = original_timeout
        player_mod.WATCHDOG_INTERVAL = original_interval

    check("gave up on the stuck file", p.current is None)
    check("said so", any("Gave up" in n for n in notices), f"notices={notices}")
    check("bounded retries",
          p._freeze_reloads <= player_mod.MAX_FREEZE_RELOADS + 1,
          f"reloads={p._freeze_reloads}")


async def test_freeze_counter_resets_on_progress():
    """Real progress clears the freeze counter so the cap can't creep up."""
    print("\nrecovered playback:")
    item = FakeItem(1, "Fine")
    p = make_player(FakeLib([item]))
    await p.play(item)
    p._freeze_reloads = 2
    p.position, p._last_progress_value = 50.0, 10.0

    original = player_mod.WATCHDOG_INTERVAL
    player_mod.WATCHDOG_INTERVAL = 0.02
    try:
        task = asyncio.get_running_loop().create_task(p._watchdog_loop())
        await asyncio.sleep(0.3)
        task.cancel()
    finally:
        player_mod.WATCHDOG_INTERVAL = original
    check("counter cleared by progress", p._freeze_reloads == 0,
          f"reloads={p._freeze_reloads}")


async def main():
    await test_stale_entry_dropped()
    await test_missing_entry_id_still_advances()
    await test_error_cascade_capped()
    await test_error_counter_resets()
    await test_freeze_reload_capped()
    await test_freeze_counter_resets_on_progress()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
