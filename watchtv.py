"""YouTube TV: find what's live and put it on screen.

Not yt-dlp. YouTube TV is Widevine-protected, so there is no format to fetch
and no amount of cookies changes that — the age-gate workaround in youtube.py
does not generalise here. This drives a real signed-in Chrome instead, which
is the player the subscription is meant to be watched in.

Chrome specifically, and a REAL Chrome: Playwright's bundled Chromium ships
without a Widevine module, so DRM silently fails to play in it. `channel=
"chrome"` points it at the installed browser instead. Firefox is not used
here — browser.py picked it for the age-gate fallback and that choice was
never about this.

WHY SEARCH RATHER THAN A SCHEDULE. An NFL schedule API could say which network
carries a game, but not which feeds this account can actually open: blackouts,
home area, Sunday Ticket and Red Zone entitlements all decide that, and
YouTube TV already knows. Asking it is both simpler and correct; deriving it
ourselves would be neither.

SELECTION IS RANKING, NEVER FILTERING. The same game legitimately appears more
than once — a 4K feed alongside the standard one, a multiview carrying four
games at once, a national feed beside a local affiliate. Excluding any of them
by rule breaks the day the game exists ONLY in the excluded form. So every
candidate is scored and the best one wins, which means an unusual case
degrades to "picked something reasonable" instead of "found nothing".

Everything here is blocking. Call it from a thread.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import config

log = logging.getLogger("athena.watchtv")

BASE = "https://tv.youtube.com"

# What a result IS, as far as selection cares. Deliberately coarse: the point
# is to rank, and a finer taxonomy would be more ways to be wrong about markup
# we cannot see until the season starts.
BROADCAST = "broadcast"      # an ordinary single-game feed
UHD = "4k"                   # the same game, 4K plan
MULTIVIEW = "multiview"      # several games in one stream
UNKNOWN = "unknown"          # parsed, but not recognised — surfaced, not dropped

# Badges and title fragments. Matched case-insensitively against whatever text
# the card carries, because YouTube TV puts this in different places depending
# on the surface (search results, the guide, the sports shelf).
_UHD_HINTS = ("4k", "2160p", "uhd", "ultra hd")
_MULTIVIEW_HINTS = ("multiview", "multi-view", "multiview:", "4 games",
                    "watch 4", "quad")
_LIVE_HINTS = ("live", "on now", "airing now")
# A replay or highlight reel matching a team name is the most likely wrong
# answer of all, so these are named rather than inferred from absence of LIVE.
_REPLAY_HINTS = ("highlights", "replay", "condensed", "recap", "full game",
                 "encore", "rebroadcast")


@dataclass
class Candidate:
    """One thing YouTube TV offered, and what we made of it.

    `raw` is kept so a bad pick can be diagnosed from the log without
    reproducing the search — the first live Sunday will be a tuning session
    and guessing twice is worse than storing a string.
    """

    title: str
    kind: str = UNKNOWN
    live: bool = False
    replay: bool = False
    channel: str = ""
    url: str = ""
    raw: str = ""
    score: int = 0
    reasons: list = field(default_factory=list)

    def describe(self) -> str:
        bits = [self.title or "(untitled)"]
        if self.channel:
            bits.append(self.channel)
        if self.kind == UHD:
            bits.append("4K")
        elif self.kind == MULTIVIEW:
            bits.append("multiview")
        elif self.kind == UNKNOWN:
            bits.append("unrecognised")
        if self.replay:
            bits.append("replay")
        elif not self.live:
            bits.append("not live")
        return " · ".join(bits)


def classify(text: str) -> str:
    """What kind of result this is, from its visible text.

    Order matters: a multiview card can mention 4K, and calling that a 4K
    single-game feed would put four games on when someone asked for one.
    """
    low = (text or "").lower()
    if any(h in low for h in _MULTIVIEW_HINTS):
        return MULTIVIEW
    if any(_word(low, h) for h in _UHD_HINTS):
        return UHD
    return BROADCAST


def _word(haystack: str, needle: str) -> bool:
    """Substring match that will not fire inside another word.

    "4k" appears in the middle of plenty of things that are not a 4K feed,
    and a false 4K reading demotes the correct result.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
                     haystack) is not None


def looks_live(text: str) -> bool:
    low = (text or "").lower()
    return any(_word(low, h) for h in _LIVE_HINTS)


def looks_replay(text: str) -> bool:
    low = (text or "").lower()
    return any(h in low for h in _REPLAY_HINTS)


def _terms(query: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", (query or "").lower()) if t]


def match_strength(query: str, candidate: Candidate) -> int:
    """How well a candidate answers the words that were typed. 0 means not.

    Whole-word matching, so "sea" does not match "Seahawks" and quietly win on
    a shorter title.
    """
    hay = f"{candidate.title} {candidate.channel}".lower()
    terms = _terms(query)
    if not terms:
        return 0
    return sum(1 for t in terms if _word(hay, t))


def rank(candidates: list, query: str = "", prefer: str = BROADCAST) -> list:
    """Score every candidate and return them best-first, with reasons.

    Preference, never exclusion. `prefer` moves a kind to the top of the pile
    but nothing is removed, so asking for a game that exists only as a 4K feed
    or only inside a multiview still plays it — it simply loses whenever an
    ordinary broadcast of the same game is also on offer.

    The reasons are not decoration. When this picks wrong in September, the
    difference between having them and not is an afternoon.
    """
    out = []
    for c in candidates:
        score, why = 0, []

        strength = match_strength(query, c) if query else 0
        if query:
            score += strength * 100
            why.append(f"matched {strength} of {len(_terms(query))} terms")

        # Live is what separates the game from a highlight reel of the game.
        if c.live:
            score += 500
            why.append("live")
        if c.replay:
            score -= 400
            why.append("looks like a replay")
        if not c.live and not c.replay:
            score -= 150
            why.append("not marked live")

        if c.kind == prefer:
            score += 60
            why.append(f"preferred kind ({prefer})")
        elif c.kind == BROADCAST:
            # The default answer to "put the game on" is the game.
            score += 40
            why.append("single-game broadcast")
        elif c.kind == UHD:
            # Plays fine, but this machine downscales it to a 1080p share:
            # see the GPU contention notes in config.py.
            score += 10
            why.append("4K feed, deprioritised for a 1080p share")
        elif c.kind == MULTIVIEW:
            score += 5
            why.append("multiview, not the single game")
        else:
            why.append("unrecognised kind")

        c.score, c.reasons = score, why
        out.append(c)

    out.sort(key=lambda c: -c.score)
    return out


def choose(candidates: list, query: str = "", prefer: str = BROADCAST):
    """(pick, ranked, note). pick is None when nothing is worth playing.

    `note` is what to tell the person when the answer is not simply "playing
    it": nothing live, several equally good matches, or results we could not
    identify. Saying so beats opening something and hoping.
    """
    ranked = rank(candidates, query, prefer)
    if not ranked:
        return None, [], "I could not find anything matching that."

    live = [c for c in ranked if c.live and not c.replay]
    if not live:
        soon = ranked[0]
        return None, ranked, (
            f"Nothing matching that is live right now. The closest I found is "
            f"{soon.describe()}.")

    pick = live[0]
    note = ""

    # A tie between two live results is worth mentioning rather than resolving
    # silently — they are usually the same game on different feeds, but not
    # always, and being told is cheap.
    tied = [c for c in live[1:] if c.score == pick.score]
    if tied:
        note = ("Several equally good matches; playing the first. Also on: "
                + ", ".join(c.describe() for c in tied[:3]))

    unknown = [c for c in ranked if c.kind == UNKNOWN and c.live]
    if unknown and not note:
        note = (f"{len(unknown)} result(s) I could not identify — say so if "
                f"this is the wrong one.")

    return pick, ranked, note


def alternatives(pick, ranked: list) -> str:
    """A short "also available" line for the reply, or "".

    Makes the ranking visible. If the preference ever picks wrong, it is
    obvious immediately instead of being mistaken for a bad stream.
    """
    if pick is None:
        return ""
    others = [c for c in ranked
              if c is not pick and c.live and not c.replay][:3]
    if not others:
        return ""
    return "also available: " + ", ".join(
        c.describe() for c in others)


# ---------------------------------------------------------------- the browser
#
# Everything below drives a real Chrome. It is separated from the scoring above
# on purpose: the scoring is exercised by tests today, while none of this can
# be verified until there is a signed-in profile and something live to point it
# at. Keeping the guesswork in one place means the first live Sunday is a
# selector-tuning session and nothing more.

# UNVERIFIED. These are best guesses at YouTube TV's markup, written before the
# season. Each is a list because the same thing is presented differently on the
# search page and the guide, and the first match wins. When something breaks it
# will be here, and the failure says which lookup gave up rather than clicking
# whatever happens to sit at those coordinates.
SELECTORS = {
    "search_box": ['input[aria-label*="Search" i]',
                   'input[placeholder*="Search" i]',
                   'ytu-search-box input'],
    "search_open": ['a[href*="/search"]', 'button[aria-label*="Search" i]'],
    "result_card": ['ytu-grid-renderer a[href*="/watch"]',
                    'a[href*="/watch/"]',
                    '[class*="result"] a[href*="watch"]'],
    "play_button": ['button[aria-label*="Play" i]',
                    '.ytp-play-button',
                    'button[title*="Play" i]'],
    "fullscreen": ['.ytp-fullscreen-button',
                   'button[aria-label*="Full screen" i]'],
    "signed_out": ['a[href*="accounts.google.com"]',
                   'button[aria-label*="Sign in" i]',
                   'a[aria-label*="Sign in" i]'],
}

_ctx = None          # the persistent context, kept open between commands
_page = None


class NotSignedIn(RuntimeError):
    """The profile has no YouTube TV session. A person has to fix this once."""


class Unavailable(RuntimeError):
    """Chrome or Playwright is not usable. Says which, so it can be fixed."""


def profile_ready() -> bool:
    """Whether the profile directory looks like somebody has signed in.

    Cheap and approximate — the authoritative answer needs a page load. This
    exists so a command can say "sign in first" without spending 30 seconds
    launching a browser to find out.
    """
    import os

    prof = config.WATCHTV_PROFILE
    return os.path.isdir(prof) and os.path.isdir(os.path.join(prof, "Default"))


def _context():
    """The signed-in browser, launched once and kept.

    Persistent rather than per-command: the sign-in lives in the profile, and
    a television does not need reopening between channel changes.
    """
    global _ctx, _page
    if _ctx is not None and _page is not None:
        return _ctx, _page
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise Unavailable(
            "Playwright is not installed in the bot's environment.") from exc

    pw = sync_playwright().start()
    launch = {
        "user_data_dir": config.WATCHTV_PROFILE,
        # Headed. Widevine does not play in headless Chrome, and the whole
        # point is that this ends up on a screen being shared.
        "headless": False,
        "args": ["--autoplay-policy=no-user-gesture-required",
                 "--disable-features=Translate",
                 "--start-maximized"],
        "no_viewport": True,
    }
    if config.WATCHTV_CHROME:
        launch["executable_path"] = config.WATCHTV_CHROME
    else:
        # NOT the bundled Chromium: it has no Widevine module, so DRM fails
        # silently and the page just never plays.
        launch["channel"] = "chrome"
    try:
        _ctx = pw.chromium.launch_persistent_context(**launch)
    except Exception as exc:
        raise Unavailable(f"Could not start Chrome: {exc}") from exc
    _page = _ctx.pages[0] if _ctx.pages else _ctx.new_page()
    _page.set_default_timeout(config.WATCHTV_TIMEOUT * 1000)
    return _ctx, _page


def close() -> None:
    """Shut the browser. Safe to call when it was never opened."""
    global _ctx, _page
    try:
        if _ctx is not None:
            _ctx.close()
    except Exception:
        pass
    _ctx, _page = None, None


def _first(page, key: str, timeout_ms: int = 4000):
    """The first selector in SELECTORS[key] that matches, or None.

    Returning None rather than raising lets a caller decide whether a missing
    element is fatal. What is never done is guessing at a position.
    """
    for sel in SELECTORS.get(key, ()):
        try:
            el = page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            if el:
                return el
        except Exception:
            continue
    return None


def _signed_in(page) -> bool:
    return _first(page, "signed_out", timeout_ms=1500) is None


def _harvest(page) -> list:
    """Every result card on the page, as Candidates.

    Reads the card's whole text rather than hunting for a badge element:
    "LIVE", "4K" and "Multiview" appear in different places on different
    surfaces, and the text contains all of them wherever they sit.
    """
    out = []
    cards = []
    for sel in SELECTORS["result_card"]:
        try:
            found = page.query_selector_all(sel)
        except Exception:
            found = []
        if found:
            cards = found
            break
    for el in cards[:40]:
        try:
            text = (el.inner_text() or "").strip()
            href = el.get_attribute("href") or ""
        except Exception:
            continue
        if not text:
            continue
        flat = " ".join(text.split())
        c = Candidate(
            title=flat.split("\n")[0][:120],
            kind=classify(flat),
            live=looks_live(flat),
            replay=looks_replay(flat),
            url=(BASE + href) if href.startswith("/") else href,
            raw=flat[:300],
        )
        out.append(c)
    return out


def search(query: str) -> list:
    """Ask YouTube TV what it has for `query`. Blocking."""
    _, page = _context()
    page.goto(f"{BASE}/search?q={query}", wait_until="domcontentloaded")
    if not _signed_in(page):
        raise NotSignedIn(
            "That Chrome profile is not signed into YouTube TV yet.")
    page.wait_for_timeout(2500)      # results stream in after the shell
    found = _harvest(page)
    # Logged in full, deliberately: when this picks wrong on a Sunday, the
    # candidate list and the scores are the difference between an afternoon
    # and a season of guessing.
    log.info("watchtv search %r -> %d candidates", query, len(found))
    for c in found:
        log.info("  candidate: %s | %s", c.describe(), c.raw[:120])
    return found


def play(query: str, prefer: str = "") -> tuple:
    """Find and start the best match. Returns (message, ok). Blocking."""
    prefer = (prefer or config.WATCHTV_PREFER or BROADCAST).lower()
    found = search(query)
    pick, ranked, note = choose(found, query=query, prefer=prefer)
    for c in ranked[:8]:
        log.info("  ranked %5d %s | %s", c.score, c.describe(),
                 "; ".join(c.reasons))
    if pick is None:
        return (note or "Nothing matching that is on right now."), False

    _, page = _context()
    try:
        if pick.url:
            page.goto(pick.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        btn = _first(page, "play_button", timeout_ms=6000)
        if btn:
            try:
                btn.click()
            except Exception:
                pass
        page.wait_for_timeout(1500)
        # Player fullscreen, not window fullscreen — the same distinction
        # browser.py documents for the Firefox age-gate path: filling the
        # screen with the page is not filling it with the video.
        fs = _first(page, "fullscreen", timeout_ms=4000)
        if fs:
            try:
                fs.click()
            except Exception:
                page.keyboard.press("f")
        else:
            page.keyboard.press("f")
    except Exception as exc:
        log.warning("watchtv playback step failed: %s", exc)
        return (f"I found {pick.describe()} but could not start it. "
                f"The page may have changed."), False

    msg = f"Playing {pick.describe()}"
    extra = alternatives(pick, ranked)
    if note:
        msg += f" — {note}"
    elif extra:
        msg += f" — {extra}"
    return msg, True


def whats_on(limit: int = 12) -> list:
    """What is live right now, best-first. Blocking.

    Exists so somebody with no YouTube TV subscription of their own can see
    what is available without a second device.
    """
    found = search("live")
    ranked = rank(found, query="", prefer=config.WATCHTV_PREFER or BROADCAST)
    return [c for c in ranked if c.live and not c.replay][:limit]
