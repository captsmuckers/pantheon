"""Everything that talks to the Plex *server*.

Note what is NOT here: any attempt to talk to a Plex *client*. The Companion
remote-control API is the thing that was breaking, and we no longer use it.
Plex is now purely a metadata and file source; mpv does the playing.

All calls here are blocking. Call them from a thread (asyncio.to_thread).
"""

import difflib
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass

import plexapi
from plexapi.server import PlexServer

import config

# Identify ourselves to the server. Without this the bot's activity shows up
# anonymously in the Plex dashboard, and Plex has less to go on when working out
# where a session came from.
plexapi.X_PLEX_PRODUCT = "Athena"
plexapi.X_PLEX_DEVICE_NAME = config.PLEX_DEVICE_NAME
plexapi.X_PLEX_PLATFORM = "Windows"
plexapi.X_PLEX_DEVICE = "PC"
plexapi.X_PLEX_IDENTIFIER = config.PLEX_CLIENT_ID

log = logging.getLogger("athena.library")

# "The Office s3e5", "the office 3x05", "the office season 3 episode 5"
_SXXEXX = re.compile(
    r"^(?P<show>.+?)\s+(?:"
    r"[sS](?P<s1>\d{1,2})\s*[eExX](?P<e1>\d{1,3})"
    r"|(?P<s2>\d{1,2})\s*[xX]\s*(?P<e2>\d{1,3})"
    r"|season\s+(?P<s3>\d{1,2})\s+episode\s+(?P<e3>\d{1,3})"
    r")\s*$"
)


_LATEST_PRE = re.compile(
    r"^(?:the\s+)?(?:latest|newest|most\s+recent|last|final)\s+(?:episode|ep)\s*(?:of\s+)?(?P<show>.+)$",
    re.I,
)
_LATEST_POST = re.compile(
    r"^(?P<show>.+?)\s+(?:the\s+)?(?:latest|newest|most\s+recent|last|final)"
    r"(?:\s+(?:episode|ep))?$",
    re.I,
)
# "newest king of the hill episode" — adjective, then show, then "episode" at
# the end, distinct from _LATEST_PRE ("newest episode of X") and _LATEST_POST
# ("X's newest episode").
_LATEST_MID = re.compile(
    r"^(?:the\s+)?(?:latest|newest|most\s+recent|last|final)\s+"
    r"(?P<show>.+?)\s+(?:episode|ep)$",
    re.I,
)
# "new season of king of the hill" / "latest season of X" — no season number
# given, so this is a proxy for "whatever's newest", same as _LATEST_*.
_NEW_SEASON = re.compile(
    r"^(?:the\s+)?(?:new|newest|latest|most\s+recent)\s+season\s+(?:of\s+)?(?P<show>.+)$",
    re.I,
)
_NEXT_PRE = re.compile(
    r"^(?:next\s+(?:episode|ep)\s+(?:of\s+)?|continue\s+|keep\s+watching\s+|resume\s+)(?P<show>.+)$",
    re.I,
)
_NEXT_POST = re.compile(r"^(?P<show>.+?)\s+next(?:\s+(?:episode|ep))?$", re.I)
# "a random king of the hill episode", "random severance"
_RANDOM_QUERY = re.compile(r"^(?:a\s+|an\s+|some\s+)?random\s+(?P<rest>.+)$", re.I)
# A bare "episode"/"season" anywhere in a query is proof they mean the series.
# Libraries hold films and shows with identical names — King of the Hill is
# both a 1993 film and a 1997 series — and only one of them has episodes.
_EPISODE_WORD = re.compile(r"\b(?:episodes?|eps?|seasons?)\b", re.I)


@dataclass
class TitleEntry:
    rating_key: int
    title: str
    kind: str  # 'movie' or 'show'
    year: int | None
    library: str = ""
    added_at: float = 0.0
    genres: tuple = ()
    view_count: int = 0

    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    def long_label(self) -> str:
        """Enough detail to tell two same-named things apart."""
        kind = "Movie" if self.kind == "movie" else "Show"
        bits = [kind]
        if self.library:
            bits.append(self.library)
        return f"{self.label()} — {' · '.join(bits)}"


class Library:
    def __init__(self):
        self.plex = PlexServer(config.PLEX_URL, config.PLEX_TOKEN)
        log.info("Connected to Plex server: %s", self.plex.friendlyName)
        # Titles kept per section so a single changed library can be refetched
        # without rebuilding all of them.
        self._by_section: dict[str, list[TitleEntry]] = {}
        self._tokens: dict[str, list] = {}
        # When the cache is next due. A failed refresh retries soon but not
        # immediately — otherwise an unreachable Plex means every single search
        # kicks off another full scan that fails slowly.
        self._next_refresh_at = 0.0
        self._lock = threading.Lock()
        # Serialises refreshes: searches run in worker threads and would
        # otherwise stack up duplicate full scans of every library.
        self._refresh_lock = threading.Lock()
        self._degraded = False

        if self._load_cache():
            log.info(
                "Loaded %d titles from the disk cache — revalidating in the background",
                len(self.titles),
            )
            self._next_refresh_at = 0.0  # due immediately, but off the hot path
        else:
            # Nothing to serve, so this one has to block.
            log.info("No usable title cache — building one (this takes a moment)")
            self.revalidate(force=True)

    # ---------- title cache ----------

    # How long a good cache lasts, and how soon to retry after a failure.
    REFRESH_INTERVAL = 1800
    RETRY_INTERVAL = 60

    def refresh_titles(self) -> None:
        """Force a full rebuild of the title cache."""
        self.revalidate(force=True)

    # ---- raw section reads ----
    #
    # Deliberately not section.all(). plexapi builds a full Movie/Show object
    # per item — media parts, streams, guids — and for 11k films that costs
    # ~63s of pure object construction on top of a ~14s transfer. The eight
    # fields below are all this cache has ever used, so the XML is read
    # directly. plex.query() is reused for the token/session handling but
    # returns a bare Element, so none of that construction happens.

    def _sections(self) -> tuple[list[tuple[str, str, str]], list[str]]:
        """(key, title, type) for searchable sections, plus the skipped names."""
        root = self.plex.query("/library/sections")
        wanted, skipped = [], []
        for node in root.findall("Directory"):
            title, kind, key = node.get("title", ""), node.get("type"), node.get("key")
            if kind not in ("movie", "show"):
                continue
            if self.is_excluded(title):
                skipped.append(title)
                continue
            wanted.append((key, title, kind))
        return wanted, skipped

    def _section_token(self, key: str) -> list:
        """A cheap fingerprint of a section's contents.

        totalSize catches additions and deletions; the newest updatedAt catches
        edits to titles already present. One small request per section, versus
        refetching tens of megabytes to discover nothing changed.
        """
        root = self.plex.query(
            f"/library/sections/{key}/all"
            "?X-Plex-Container-Start=0&X-Plex-Container-Size=1&sort=updatedAt:desc"
        )
        newest = list(root)
        return [
            int(root.get("totalSize") or root.get("size") or 0),
            int(newest[0].get("updatedAt") or 0) if newest else 0,
        ]

    def _fetch_section(self, key: str, title: str) -> list[TitleEntry]:
        root = self.plex.query(f"/library/sections/{key}/all", timeout=300)
        entries = []
        for el in root:
            rating_key = el.get("ratingKey")
            if not rating_key:
                continue
            year = el.get("year")
            entries.append(
                TitleEntry(
                    rating_key=int(rating_key),
                    title=el.get("title") or "",
                    kind=el.get("type") or "",
                    year=int(year) if year else None,
                    library=title,
                    added_at=float(el.get("addedAt") or 0),
                    genres=tuple(
                        g.get("tag") for g in el.findall("Genre") if g.get("tag")
                    ),
                    # Per-account, not library-wide — this is plays by
                    # whichever account the bot's token belongs to.
                    view_count=int(el.get("viewCount") or 0),
                )
            )
        return entries

    def revalidate(self, force: bool = False) -> None:
        """Refetch only the sections whose fingerprint changed.

        Safe to call often: with nothing changed it costs one small request per
        section. force=True rebuilds everything regardless.
        """
        if not self._refresh_lock.acquire(blocking=False):
            return  # another thread is already doing this
        try:
            self._revalidate_locked(force)
        finally:
            self._refresh_lock.release()

    def _revalidate_locked(self, force: bool) -> None:
        started = time.monotonic()
        try:
            sections, skipped = self._sections()
            fetched = []
            for key, title, _kind in sections:
                token = self._section_token(key)
                if not force and self._tokens.get(key) == token and key in self._by_section:
                    continue
                entries = self._fetch_section(key, title)
                with self._lock:
                    self._by_section[key] = entries
                    self._tokens[key] = token
                fetched.append(f"{title} ({len(entries)})")

            # Drop sections that have gone away or become excluded.
            live = {key for key, _, _ in sections}
            with self._lock:
                for gone in [k for k in self._by_section if k not in live]:
                    del self._by_section[gone]
                    self._tokens.pop(gone, None)
                self._next_refresh_at = time.time() + self.REFRESH_INTERVAL
        except Exception:
            self._next_refresh_at = time.time() + self.RETRY_INTERVAL
            if not self._degraded:
                # Once per outage, not once per search.
                self._degraded = True
                log.exception(
                    "Failed to refresh the title cache — serving %d cached titles "
                    "and retrying every %ds until Plex answers",
                    len(self.titles), self.RETRY_INTERVAL,
                )
            else:
                log.debug("Title cache refresh still failing")
            return

        if self._degraded:
            log.info("Plex is answering again — title cache refreshed")
            self._degraded = False

        elapsed = time.monotonic() - started
        if not fetched:
            # The common case now — don't narrate it every half hour.
            log.debug("Title cache unchanged (checked in %.1fs)", elapsed)
            return

        self._save_cache()
        log.info(
            "Title cache: %d titles, refetched %s in %.1fs",
            len(self.titles), ", ".join(fetched), elapsed,
        )
        if skipped:
            log.info("Ignoring libraries: %s", ", ".join(skipped))

        # Safety net: EXCLUDE_LIBRARIES has gone missing from .env more than
        # once across config updates. A library whose name looks like 4K
        # content but wasn't skipped is worth a loud warning, not a silent
        # regression back into search results. Only reachable on a real
        # refetch, which is exactly when such a section would have appeared.
        suspect = [
            name for name in sorted({e.library for e in self.titles})
            if "4k" in name.lower()
        ]
        if suspect:
            log.warning(
                "%s look%s like 4K content but %s NOT excluded — search and "
                "playback can select it. If unintentional, check EXCLUDE_LIBRARIES "
                "in .env (currently: %s).",
                ", ".join(f"'{n}'" for n in suspect),
                "s" if len(suspect) == 1 else "",
                "is" if len(suspect) == 1 else "are",
                ", ".join(config.EXCLUDE_LIBRARIES) or "(empty)",
            )

    @staticmethod
    def is_excluded(name: str) -> bool:
        lowered = (name or "").lower()
        return any(token in lowered for token in config.EXCLUDE_LIBRARIES)

    def maybe_refresh_titles(self) -> None:
        """Lazy safety net. bot.py revalidates on a timer, so this rarely fires."""
        if time.time() >= self._next_refresh_at:
            self.revalidate()

    @property
    def titles(self) -> list[TitleEntry]:
        with self._lock:
            return [entry for entries in self._by_section.values() for entry in entries]

    # ---------- disk cache ----------
    #
    # Rebuilding from Plex is tens of seconds even reading raw XML, almost all
    # of it transfer. Persisting the parsed result turns a restart into a ~10ms
    # load, with the freshness check moved off the startup path entirely.

    CACHE_VERSION = 2

    def _cache_path(self) -> str:
        path = config.TITLE_CACHE_FILE
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        return path

    def _load_cache(self) -> bool:
        try:
            with open(self._cache_path(), "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except FileNotFoundError:
            return False
        except Exception:
            log.warning("Title cache unreadable — rebuilding", exc_info=True)
            return False

        if blob.get("version") != self.CACHE_VERSION:
            log.info("Title cache is from an older version — rebuilding")
            return False
        # A cache from a different server would be silently wrong, which is
        # worse than being slow.
        if blob.get("server") != getattr(self.plex, "machineIdentifier", None):
            log.info("Title cache belongs to a different Plex server — rebuilding")
            return False

        by_section, tokens = {}, {}
        try:
            for key, section in (blob.get("sections") or {}).items():
                title = section["title"]
                if self.is_excluded(title):
                    continue  # exclusions may have changed since it was written
                tokens[key] = section["token"]
                by_section[key] = [
                    TitleEntry(
                        rating_key=row[0], title=row[1], kind=row[2], year=row[3],
                        library=title, added_at=row[4], genres=tuple(row[6]),
                        view_count=row[5],
                    )
                    for row in section["entries"]
                ]
        except Exception:
            log.warning("Title cache is malformed — rebuilding", exc_info=True)
            return False

        if not by_section:
            return False
        with self._lock:
            self._by_section = by_section
            self._tokens = tokens
        return True

    def _save_cache(self) -> None:
        with self._lock:
            blob = {
                "version": self.CACHE_VERSION,
                "server": getattr(self.plex, "machineIdentifier", None),
                "sections": {
                    key: {
                        "title": entries[0].library if entries else "",
                        "token": self._tokens.get(key, []),
                        "entries": [
                            [e.rating_key, e.title, e.kind, e.year,
                             e.added_at, e.view_count, list(e.genres)]
                            for e in entries
                        ],
                    }
                    for key, entries in self._by_section.items()
                },
            }
        path = self._cache_path()
        try:
            # Write-then-rename: a crash mid-write must not leave a truncated
            # cache that reads as valid-but-short.
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            log.warning("Could not write the title cache", exc_info=True)

    # ---------- search ----------

    def libraries(self) -> list[str]:
        return sorted({e.library for e in self.titles if e.library})

    _STOPWORDS = {"the", "a", "an", "of", "in", "on", "and", "to"}

    @classmethod
    def _shares_a_word(cls, query: str, title: str) -> bool:
        q_words = {w for w in re.findall(r"[a-z0-9]+", query) if len(w) >= 3 and w not in cls._STOPWORDS}
        t_words = {w for w in re.findall(r"[a-z0-9]+", title) if len(w) >= 3 and w not in cls._STOPWORDS}
        return bool(q_words & t_words)

    # Exact, starts-with and substring hits (0.85 and up) are trusted outright.
    # Below that the score is character similarity, which is where unrelated
    # titles creep in, so it has to be corroborated by word overlap.
    _TRUST_SCORE = 0.85
    _MIN_FUZZY_SCORE = 0.60
    _MIN_WORD_OVERLAP = 0.5

    @classmethod
    def _confident_match(cls, query: str, scored_entry) -> bool:
        """Is this good enough to play, as opposed to good enough to list?"""
        score, entry = scored_entry
        if score >= cls._TRUST_SCORE:
            return True
        if score < cls._MIN_FUZZY_SCORE:
            return False
        q_words = {w for w in re.findall(r"[a-z0-9]+", query.lower())
                   if len(w) >= 3 and w not in cls._STOPWORDS}
        t_words = {w for w in re.findall(r"[a-z0-9]+", entry.title.lower())
                   if len(w) >= 3 and w not in cls._STOPWORDS}
        if not q_words or not t_words:
            return False
        # Against the shorter side: "lord of the rings fellowship" covers all
        # of its own words against a much longer official title, and should
        # pass. One word out of six should not.
        overlap = len(q_words & t_words) / min(len(q_words), len(t_words))
        return overlap >= cls._MIN_WORD_OVERLAP

    def search(
        self,
        query: str,
        kind: str | None = None,
        limit: int = 25,
        library: str | None = None,
        year: int | None = None,
    ) -> list[TitleEntry]:
        """Fuzzy title search against the local cache, best matches first."""
        return [entry for _, entry in self.scored_search(query, kind, limit, library, year)]

    def scored_search(
        self,
        query: str,
        kind: str | None = None,
        limit: int = 25,
        library: str | None = None,
        year: int | None = None,
    ) -> list[tuple[float, TitleEntry]]:
        self.maybe_refresh_titles()
        q = query.strip().lower()
        if not q:
            return []
        scored: list[tuple[float, TitleEntry]] = []
        for entry in self.titles:
            if kind and entry.kind != kind:
                continue
            if library and entry.library.lower() != library.lower():
                continue
            if year and entry.year != year:
                continue
            t = entry.title.lower()
            if t == q:
                score = 1.0
            elif t.startswith(q):
                score = 0.95
            elif q in t:
                score = 0.85
            else:
                score = difflib.SequenceMatcher(None, q, t).ratio()
                # Character-level similarity alone lets unrelated titles sneak
                # in — "Return to Me" scores 0.615 against "Adventure Time"
                # despite sharing no actual words. Require real word overlap
                # for anything that isn't already an exact/prefix/substring hit.
                if score < 0.55 or not self._shares_a_word(q, t):
                    continue
            scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1].title))
        return scored[:limit]

    def fetch(self, rating_key: int):
        return self.plex.fetchItem(int(rating_key))

    # ---------- resolution ----------

    def _parse_qualifiers(self, query: str):
        """Pull 'movie', 'show', a library name, or a year out of the query.

        Lets you disambiguate inline: "attack on titan (show)",
        "attack on titan from anime", "attack on titan 2013".
        """
        q = query.strip()
        kind = library = None
        year = None
        known = {name.lower(): name for name in self.libraries()}

        m = re.search(r"\s*\((?P<q>[^)]+)\)\s*$", q)
        if m:
            token = m.group("q").strip().lower()
            if token in ("movie", "film"):
                kind, q = "movie", q[: m.start()].strip()
            elif token in ("show", "series", "tv", "tv show"):
                kind, q = "show", q[: m.start()].strip()
            elif token in known:
                library, q = known[token], q[: m.start()].strip()
            elif token.isdigit() and len(token) == 4:
                year, q = int(token), q[: m.start()].strip()

        m = re.match(
            r"^(?P<title>.+)\s+(?:from|in|on)\s+(?:the\s+)?(?P<lib>.+?)(?:\s+library)?$",
            q,
            re.I,
        )
        if m and m.group("lib").strip().lower() in known:
            library = known[m.group("lib").strip().lower()]
            q = m.group("title").strip()

        m = re.match(r"^(?:the\s+)?(?P<word>movie|film|show|series|anime)\s+(?P<title>.+)$", q, re.I)
        if m:
            word = m.group("word").lower()
            if word in known:
                library = known[word]
            elif word in ("movie", "film"):
                kind = "movie"
            else:
                kind = "show"
            q = m.group("title").strip()

        m = re.match(r"^(?P<title>.+?)\s+(?P<year>(?:19|20)\d{2})$", q)
        if m and year is None:
            year, q = int(m.group("year")), m.group("title").strip()

        # Last, because the word carries no title information: if they said
        # "episode" or "season" anywhere, they mean the series. Without this,
        # "a king of the hill episode" ties the 1993 film against the 1997
        # show and asks a question with an obvious answer.
        if kind is None and _EPISODE_WORD.search(q):
            kind = "show"
            q = _EPISODE_WORD.sub(" ", q)
            q = re.sub(r"\s{2,}", " ", q).strip()

        return q, kind, library, year

    def resolve_random(self, query: str):
        """A randomly chosen episode of the named show.

        Returns (item, candidates), matching resolve_query, so callers handle
        an ambiguous show name the same way they always do.
        """
        clean = _EPISODE_WORD.sub(" ", query)
        clean = re.sub(r"\s{2,}", " ", clean).strip()
        # "a random episode of the office" leaves a dangling "of" once the
        # word episode is removed.
        clean = re.sub(r"^(?:of|from)\s+", "", clean, flags=re.I).strip()
        if not clean:
            return None, []

        show, options = self._best_show_or_options(clean)
        if options:
            return None, options
        if show is None:
            # No such series. It may still be a film, in which case "random"
            # has no meaning and playing it is the sensible reading.
            hits = self.scored_search(clean, kind="movie", limit=1)
            if hits and hits[0][0] >= 0.9:
                return self.fetch(hits[0][1].rating_key), []
            return None, []
        try:
            episodes = show.episodes()
        except Exception:
            log.exception("Could not list episodes for a random pick")
            return None, []
        if not episodes:
            return None, []
        return random.choice(episodes), []

    def resolve_query(self, query: str):
        """Resolve to one item, or report an ambiguity.

        Returns (item, candidates). An item means we're confident; a non-empty
        candidates list means several things matched equally and someone
        should choose.
        """
        q = query.strip()
        if not q:
            return None, []

        # "play a random X episode". Handled here rather than only in the fast
        # path so the model's play_media gets the same behaviour — otherwise
        # "random" is silently dropped and the on-deck episode plays instead,
        # which is a different answer to the one that was asked for.
        m = _RANDOM_QUERY.match(q)
        if m:
            # ...but "Random Hearts" and "Random Harvest" are films. A strong
            # match on the whole phrase means they named a title, not a mood.
            titled = self.scored_search(q, limit=1)
            if not (titled and titled[0][0] >= 0.9):
                return self.resolve_random(m.group("rest"))

        # Explicit forms are never ambiguous — and a failed explicit match must
        # not fall through to a general search, or "the office s9e10" with a
        # show that isn't found can silently resolve to an unrelated movie.
        matched, explicit, explicit_options = self._resolve_explicit(q)
        if matched:
            return explicit, explicit_options

        clean, kind, library, year = self._parse_qualifiers(q)
        scored = self.scored_search(clean, kind=kind, limit=8, library=library, year=year)
        if not scored:
            scored = self.scored_search(q, limit=8)

        # Weak title match? The tail may be an episode name rather than noise.
        if not scored or scored[0][0] < 0.9:
            episode = self._episode_from_prefix(q)
            if episode is not None:
                return episode, []
        if not scored:
            return None, []

        # Committing to play something needs a higher bar than listing it as a
        # candidate. scored_search deliberately keeps weak matches so `search`
        # can show near misses, but this function's answer gets played.
        #
        # "play hit me baby one more time" scored 0.58 against "The Land Before
        # Time" — one shared word out of six, the word being "time" — and was
        # played, resuming at 24 minutes in. A threshold alone cannot fix that:
        # "lord of the rings fellowship" is a correct match at 0.73, lower than
        # several wrong ones. Word overlap is what actually separates them, so
        # a mid-band score has to be corroborated by the query and the title
        # genuinely sharing most of their words.
        if not self._confident_match(clean or q, scored[0]):
            return None, []

        tied = self._disambiguation_pool(scored)
        if len(tied) > 1:
            return None, tied[:5]

        item = self.fetch(scored[0][1].rating_key)
        if item.type == "show":
            # NOT "or item" — a series is not playable, so falling back to it
            # only defers the failure to stream_url as an AttributeError. None
            # reads as "couldn't find it", which is both true and actionable.
            return self.up_next(item), []
        return item, []

    def _resolve_explicit(self, q: str):
        """Forms that name exactly what they want.

        Returns (matched, item, options).
        - matched=False: this text isn't an explicit form at all; the caller
          should fall through to a general fuzzy search.
        - matched=True, options non-empty: the show name is ambiguous (e.g.
          two shows both called "The Office") — ask, don't guess.
        - matched=True, item=None, no options: a real miss (show not found,
          or that season/episode doesn't exist on the show we did find).
          Must NOT fall back to a general search — that's how this used to
          end up silently playing an unrelated movie.
        - matched=True, item set: resolved cleanly.
        """
        for pattern, picker in (
            (_NEXT_PRE, self.up_next),
            (_NEXT_POST, self.up_next),
            (_LATEST_PRE, self.latest_episode),
            (_LATEST_POST, self.latest_episode),
            (_LATEST_MID, self.latest_episode),
            (_NEW_SEASON, self.latest_episode),
        ):
            m = pattern.match(q)
            if not m:
                continue
            show, options = self._best_show_or_options(m.group("show"))
            if options:
                return True, None, options
            return True, (picker(show) if show is not None else None), []

        m = _SXXEXX.match(q)
        if m:
            show, options = self._best_show_or_options(m.group("show"))
            if options:
                return True, None, options
            if show is None:
                return True, None, []
            season = int(m.group("s1") or m.group("s2") or m.group("s3"))
            episode = int(m.group("e1") or m.group("e2") or m.group("e3"))
            try:
                return True, show.episode(season=season, episode=episode), []
            except Exception:
                return True, None, []
        return False, None, []

    def genres_available(self) -> list[str]:
        seen = set()
        for entry in self.titles:
            seen.update(entry.genres)
        return sorted(seen)

    def browse(
        self,
        kind: str | None = None,
        sort: str = "newest_release",
        genre: str | None = None,
        library: str | None = None,
        limit: int = 10,
    ) -> list[TitleEntry]:
        """Answer 'what's the newest X' / 'any horror movies' questions from
        real data instead of the model guessing. sort: newest_release (by
        year), recently_added (by when it was added to Plex), oldest, title."""
        rows = self.titles
        if kind:
            rows = [e for e in rows if e.kind == kind]
        if library:
            rows = [e for e in rows if e.library.lower() == library.lower()]
        if genre:
            wanted = genre.strip().lower()
            rows = [e for e in rows if any(wanted in g.lower() for g in e.genres)]

        if sort == "recently_added":
            rows = sorted(rows, key=lambda e: e.added_at, reverse=True)
        elif sort == "oldest":
            rows = sorted(rows, key=lambda e: e.year or 9999)
        elif sort == "title":
            rows = sorted(rows, key=lambda e: e.title.lower())
        elif sort == "most_watched":
            rows = [e for e in rows if e.view_count > 0]
            rows = sorted(rows, key=lambda e: e.view_count, reverse=True)
        else:  # newest_release
            rows = sorted(rows, key=lambda e: e.year or 0, reverse=True)
        return rows[:limit]

    def resolve(self, query: str):
        """One item or None. Ambiguity is resolved by taking the best match."""
        item, options = self.resolve_query(query)
        if item is not None:
            return item
        if options:
            best = self.fetch(options[0].rating_key)
            return self.up_next(best) or best if best.type == "show" else best
        return None

    def _best_show(self, name: str):
        """Single best guess, no ambiguity check. Kept for callers that can't
        surface a picker (e.g. resolving an already-chosen episode)."""
        matches = self.search(name, kind="show", limit=1)
        if not matches:
            return None
        return self.fetch(matches[0].rating_key)

    @staticmethod
    def _disambiguation_pool(scored: list[tuple[float, "TitleEntry"]]) -> list["TitleEntry"]:
        """Which of a scored search's results should be treated as candidates
        for 'did you mean' — not just ones within a hair of the top score.

        A near-exact match (>=0.85, i.e. an exact or starts-with hit) against
        a second, third, etc result is exactly the case worth asking about —
        "The Office" and "The Office (US)" shouldn't silently pick one just
        because the tie-margin from the top score is technically wide.
        """
        if not scored:
            return []
        top = scored[0][0]
        if top >= 0.85:
            pool = [entry for score, entry in scored if score >= 0.85]
        else:
            pool = [entry for score, entry in scored if score >= top - 0.02]
        return pool

    def _best_show_or_options(self, name: str, limit: int = 5):
        """Like _best_show, but returns (None, candidates) when the name is
        genuinely ambiguous — e.g. two shows both named 'The Office' (a 2026
        reboot and the 2005 US original). Deliberately loose: any show whose
        title starts with or nearly matches the query counts as a candidate,
        not just ones within a tight score band of the top result — 'The
        Office' (exact) and 'The Office (US)' (starts-with) should both
        surface rather than silently picking the exact match."""
        scored = self.scored_search(name, kind="show", limit=limit)
        if not scored:
            return None, []
        pool = self._disambiguation_pool(scored)
        if len(pool) > 1:
            return None, pool
        return self.fetch(scored[0][1].rating_key), []

    def up_next(self, show):
        """The episode Plex thinks you should watch next, else the first one."""
        if show is None:
            return None
        try:
            on_deck = show.onDeck()
            if on_deck is not None:
                return on_deck
        except Exception:
            pass
        try:
            episodes = show.episodes()
        except Exception:
            return None
        for ep in episodes:
            if not _is_played(ep):
                return ep
        return episodes[0] if episodes else None

    def latest_episode(self, show):
        """Most recent episode by season/episode order, ignoring specials."""
        if show is None:
            return None
        try:
            episodes = show.episodes()
        except Exception:
            return None
        if not episodes:
            return None
        regular = [e for e in episodes if (getattr(e, "seasonNumber", 0) or 0) > 0]
        pool = regular or episodes
        return max(
            pool,
            key=lambda e: (
                getattr(e, "seasonNumber", 0) or 0,
                getattr(e, "episodeNumber", 0) or 0,
            ),
        )

    def find_episode_by_title(self, show, text: str):
        """Fuzzy-match an episode title within a show."""
        wanted = (text or "").strip().lower()
        if not show or not wanted:
            return None
        try:
            episodes = show.episodes()
        except Exception:
            return None
        best, best_score = None, 0.0
        for ep in episodes:
            title = (getattr(ep, "title", "") or "").lower()
            if not title:
                continue
            if title == wanted:
                return ep
            if wanted in title or title in wanted:
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, wanted, title).ratio()
            if score > best_score:
                best, best_score = ep, score
        return best if best_score >= 0.62 else None

    def _episode_from_prefix(self, query: str):
        """'king of the hill next of shin' -> that episode.

        Finds the longest show title that prefixes the query, then matches the
        remainder against that show's episode titles.
        """
        lowered = query.strip().lower()
        best_entry, best_len, remainder = None, 0, None
        for entry in self.titles:
            if entry.kind != "show":
                continue
            title = entry.title.lower()
            if lowered.startswith(title + " ") and len(title) > best_len:
                best_entry = entry
                best_len = len(title)
                remainder = query.strip()[len(title):].strip()
        if not best_entry or not remainder:
            return None
        return self.find_episode_by_title(self.fetch(best_entry.rating_key), remainder)

    def next_episode(self, episode):
        """The episode immediately after this one, crossing season boundaries."""
        if episode is None or episode.type != "episode":
            return None
        try:
            show = episode.show()
            episodes = show.episodes()
        except Exception:
            log.exception("Could not list episodes for next-episode lookup")
            return None
        for i, ep in enumerate(episodes):
            if int(ep.ratingKey) == int(episode.ratingKey):
                return episodes[i + 1] if i + 1 < len(episodes) else None
        return None

    # ---------- playback plumbing ----------

    def stream_url(self, item) -> str | None:
        """A direct-play HTTP URL mpv can open.

        No transcode decision, no session negotiation — mpv handles basically
        every codec Plex would otherwise transcode for.
        """
        # A series has no media parts of its own. Reaching here with one is a
        # caller bug, but it arrived twice from real requests ("Dragon Ball
        # Super", "Regular Show") and produced an AttributeError traceback for
        # what is a foreseeable case, so say it plainly instead.
        if getattr(item, "type", None) == "show":
            log.error("Asked for a stream URL for the series %r — needs an episode",
                      getattr(item, "title", "?"))
            return None
        try:
            part = item.media[0].parts[0]
            return self.plex.url(part.key, includeToken=True)
        except Exception:
            log.exception("Could not build stream URL for %s", getattr(item, "title", "?"))
            return None

    def external_subtitles(self, item) -> list[dict]:
        """Sidecar subtitle streams that aren't embedded in the video file."""
        out = []
        try:
            for media in item.media:
                for part in media.parts:
                    for stream in part.streams:
                        if stream.streamType != 3:
                            continue
                        key = getattr(stream, "key", None)
                        if not key:
                            continue  # embedded — mpv will find it itself
                        out.append(
                            {
                                "url": self.plex.url(key, includeToken=True),
                                "title": getattr(stream, "title", None) or "",
                                "lang": getattr(stream, "languageCode", None) or "und",
                            }
                        )
        except Exception:
            log.exception("Could not enumerate external subtitles")
        return out

    def resume_offset(self, item) -> float:
        """Seconds to start at, honouring Plex's saved position."""
        offset_ms = getattr(item, "viewOffset", 0) or 0
        duration_ms = getattr(item, "duration", 0) or 0
        seconds = offset_ms / 1000.0
        if seconds < config.RESUME_MIN_SECONDS:
            return 0.0
        if duration_ms and offset_ms > duration_ms * config.RESUME_MAX_FRACTION:
            return 0.0
        return seconds

    def report_progress(self, item, position_s: float, state: str) -> None:
        """Push playback position back to Plex so watch state stays in sync.

        state is one of: playing, paused, stopped.
        """
        try:
            item.updateTimeline(
                int(position_s * 1000),
                state=state,
                duration=getattr(item, "duration", None),
            )
        except Exception as exc:
            log.debug("Timeline update failed (harmless): %s", exc)

    def mark_played(self, item) -> None:
        try:
            if hasattr(item, "markPlayed"):
                item.markPlayed()
            else:
                item.markWatched()
        except Exception as exc:
            log.debug("markPlayed failed: %s", exc)


def _is_played(item) -> bool:
    for attr in ("isPlayed", "isWatched"):
        if hasattr(item, attr):
            try:
                return bool(getattr(item, attr))
            except Exception:
                continue
    return bool(getattr(item, "viewCount", 0))


def describe(item) -> str:
    """Human label for any Plex item."""
    if item is None:
        return "nothing"
    if item.type == "episode":
        return (
            f"{item.grandparentTitle} "
            f"S{item.seasonNumber:02d}E{item.episodeNumber:02d} — {item.title}"
        )
    year = getattr(item, "year", None)
    return f"{item.title} ({year})" if year else item.title