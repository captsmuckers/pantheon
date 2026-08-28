"""Startup re-entrancy, cache backoff, and lock serialisation."""

import asyncio
import os
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import FakeItem, FakeLib, make_player  # noqa: E402

import library as library_mod  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


async def test_startup_runs_once():
    """A gateway reconnect during the slow Library build must not start twice."""
    print("on_ready re-entrancy:")
    starts = []
    lock = asyncio.Lock()
    started = False

    async def start_services():
        starts.append(time.monotonic())
        await asyncio.sleep(0.3)      # stands in for the full library scan

    async def on_ready():
        nonlocal started
        async with lock:
            if started:
                return
            started = True
            try:
                await start_services()
            except Exception:
                started = False

    # Five reconnects landing inside the build window.
    await asyncio.gather(*(on_ready() for _ in range(5)))
    check("services started exactly once", len(starts) == 1, f"starts={len(starts)}")

    # The old guard, for contrast: a flag set only after the slow work.
    naive_starts = []
    player_obj = None

    async def naive_on_ready():
        nonlocal player_obj
        if player_obj is not None:
            return
        naive_starts.append(1)
        await asyncio.sleep(0.3)
        player_obj = object()

    await asyncio.gather(*(naive_on_ready() for _ in range(5)))
    check("old guard would have started 5 times", len(naive_starts) == 5,
          f"starts={len(naive_starts)}")


async def test_startup_failure_retries():
    print("\nfailed startup retries on reconnect:")
    lock = asyncio.Lock()
    started = False
    attempts = []

    async def on_ready(should_fail):
        nonlocal started
        async with lock:
            if started:
                return
            started = True
            try:
                attempts.append(should_fail)
                if should_fail:
                    raise RuntimeError("Plex is down")
            except Exception:
                started = False

    await on_ready(True)
    check("first attempt failed and reset", started is False)
    await on_ready(False)
    check("second attempt ran", len(attempts) == 2, f"attempts={attempts}")
    check("and stuck", started is True)


class FakePlex:
    """Answers plex.query() with XML, like a real server, and counts the calls.

    Distinguishes cheap token probes from full section fetches, because the
    whole point of the cache is that the expensive one stops happening.
    """

    machineIdentifier = "test-server-uuid"

    SECTIONS = (
        '<MediaContainer size="2">'
        '<Directory key="1" type="movie" title="Movies"/>'
        '<Directory key="2" type="show" title="TV Shows"/>'
        '</MediaContainer>'
    )

    def __init__(self):
        self.working = True
        self.full_fetches = 0
        self.token_probes = 0
        self.counts = {"1": 3, "2": 2}
        self.updated = {"1": 1000, "2": 1000}

    def _items(self, key, n):
        return "".join(
            f'<Video ratingKey="{int(key) * 1000 + i}" title="Title {key}-{i}" '
            f'type="movie" year="2020" addedAt="1700000000" viewCount="0" '
            f'updatedAt="{self.updated[key]}"><Genre tag="Drama"/></Video>'
            for i in range(n)
        )

    def query(self, path, timeout=None, **kw):
        if not self.working:
            raise ConnectionError("plex unreachable")
        if path.startswith("/library/sections") and "/all" not in path:
            return ET.fromstring(self.SECTIONS)
        key = path.split("/library/sections/")[1].split("/")[0]
        if "X-Plex-Container-Size=1" in path:
            self.token_probes += 1
            body = (f'<MediaContainer size="1" totalSize="{self.counts[key]}">'
                    f'{self._items(key, 1)}</MediaContainer>')
            return ET.fromstring(body)
        self.full_fetches += 1
        body = (f'<MediaContainer size="{self.counts[key]}" '
                f'totalSize="{self.counts[key]}">'
                f'{self._items(key, self.counts[key])}</MediaContainer>')
        return ET.fromstring(body)


def make_library(cache_file=None):
    lib = library_mod.Library.__new__(library_mod.Library)
    lib._by_section = {}
    lib._tokens = {}
    lib._next_refresh_at = 0.0
    lib._lock = threading.Lock()
    lib._refresh_lock = threading.Lock()
    lib._degraded = False
    lib.plex = FakePlex()
    path = cache_file or os.path.join(tempfile.gettempdir(), "athena-test-cache.json")
    lib._cache_path = lambda: path
    return lib


def test_only_changed_sections_refetch():
    print("\nrevalidation refetches only what changed:")
    lib = make_library()
    lib.revalidate(force=True)
    check("both sections fetched", lib.plex.full_fetches == 2, f"{lib.plex.full_fetches}")
    check("all titles cached", len(lib.titles) == 5, f"{len(lib.titles)}")

    lib.plex.full_fetches = 0
    lib.revalidate()
    check("nothing changed -> no refetch", lib.plex.full_fetches == 0,
          f"fetches={lib.plex.full_fetches}")
    check("but tokens were checked", lib.plex.token_probes >= 2,
          f"probes={lib.plex.token_probes}")

    # An addition to one section only.
    lib.plex.counts["1"] = 4
    lib.plex.full_fetches = 0
    lib.revalidate()
    check("only the changed section refetched", lib.plex.full_fetches == 1,
          f"fetches={lib.plex.full_fetches}")
    check("new item present", len(lib.titles) == 6, f"{len(lib.titles)}")

    # An edit that leaves the count alone still gets caught, via updatedAt.
    lib.plex.updated["2"] = 2000
    lib.plex.full_fetches = 0
    lib.revalidate()
    check("edit detected by updatedAt", lib.plex.full_fetches == 1,
          f"fetches={lib.plex.full_fetches}")


def test_disk_cache_round_trip():
    print("\ndisk cache survives a restart:")
    path = os.path.join(tempfile.gettempdir(), "athena-roundtrip.json")
    if os.path.exists(path):
        os.remove(path)

    lib = make_library(path)
    lib.revalidate(force=True)
    check("cache written", os.path.exists(path))
    original = sorted(e.title for e in lib.titles)

    # A "restart": fresh object, same file.
    lib2 = make_library(path)
    loaded = lib2._load_cache()
    check("cache loaded", loaded is True)
    check("same titles", sorted(e.title for e in lib2.titles) == original,
          f"{len(lib2.titles)} vs {len(original)}")
    check("no Plex calls needed to load",
          lib2.plex.full_fetches == 0 and lib2.plex.token_probes == 0)
    entry = lib2.titles[0]
    check("fields survived", entry.year == 2020 and entry.genres == ("Drama",)
          and entry.library in ("Movies", "TV Shows"), str(entry))

    # A cache from another server must be refused, not silently served.
    lib3 = make_library(path)
    lib3.plex.machineIdentifier = "a-different-server"
    check("foreign cache rejected", lib3._load_cache() is False)

    # ...as must a corrupt one.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json at all")
    lib4 = make_library(path)
    check("corrupt cache rejected", lib4._load_cache() is False)
    os.remove(path)


def test_cache_backoff():
    print("\ntitle cache when Plex is unreachable:")
    lib = make_library()
    lib.plex.working = False

    lib.revalidate(force=True)
    check("retry scheduled, not immediate",
          lib._next_refresh_at > time.time() + lib.RETRY_INTERVAL - 5,
          f"in {lib._next_refresh_at - time.time():.0f}s")
    check("marked degraded", lib._degraded is True)

    before = lib.plex.token_probes
    for _ in range(50):
        lib.maybe_refresh_titles()
    check("50 searches caused no extra work", lib.plex.token_probes == before,
          f"probes={lib.plex.token_probes - before}")

    lib._next_refresh_at = 0.0
    lib.plex.working = True
    lib.maybe_refresh_titles()
    check("recovers when due", len(lib.titles) == 5, f"{len(lib.titles)}")
    check("no longer degraded", lib._degraded is False)
    check("long interval after success",
          lib._next_refresh_at > time.time() + lib.REFRESH_INTERVAL - 5)


def test_stale_cache_still_served():
    print("\nstale titles are served rather than nothing:")
    lib = make_library()
    lib.revalidate(force=True)
    check("populated", len(lib.titles) == 5)
    lib.plex.working = False
    lib._next_refresh_at = 0.0
    lib.maybe_refresh_titles()
    check("still serving the old titles", len(lib.titles) == 5, f"{len(lib.titles)}")


def test_refresh_is_serialised():
    print("\nconcurrent searches don't stack up scans:")
    lib = make_library()
    slow = threading.Event()
    real_query = lib.plex.query

    def slow_query(path, timeout=None, **kw):
        if "/all" in path and "Container-Size=1" not in path:
            slow.wait(0.5)
        return real_query(path, timeout=timeout, **kw)

    lib.plex.query = slow_query
    threads = [threading.Thread(target=lambda: lib.revalidate(force=True)) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    slow.set()
    for t in threads:
        t.join(timeout=5)
    check("only one pass ran", lib.plex.full_fetches == 2,
          f"full_fetches={lib.plex.full_fetches}")


async def test_lock_serialises_playback():
    """Concurrent play/skip/stop must not interleave into a torn state."""
    print("\nconcurrent playback commands:")
    items = [FakeItem(i, f"T{i}") for i in range(1, 8)]
    p = make_player(FakeLib(items))
    await p.play(items[0])
    p.queue = [i.ratingKey for i in items[1:4]]

    # Fire a skip, a play and a stop at once.
    await asyncio.gather(p.skip(), p.play(items[5]), p.stop())

    check("player left in one consistent state",
          (p.current is None and p.idle) or (p.current is not None and not p.idle),
          f"current={p.current} idle={p.idle}")
    check("queue is a real list", isinstance(p.queue, list), f"queue={p.queue}")
    check("no torn position", p.position >= 0.0, f"position={p.position}")

    # Every loadfile that happened was issued while holding the lock, so the
    # entry id always matches the last load.
    check("entry id tracks the last load",
          p._entry_id == p.mpv.entry_id or p.current is None,
          f"entry={p._entry_id} mpv={p.mpv.entry_id}")


async def main():
    await test_startup_runs_once()
    await test_startup_failure_retries()
    test_only_changed_sections_refetch()
    test_disk_cache_round_trip()
    test_cache_backoff()
    test_stale_cache_still_served()
    test_refresh_is_serialised()
    await test_lock_serialises_playback()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
