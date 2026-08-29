"""Updating the install from the repository it was cloned from.

WHAT THIS WILL NOT DO, which is most of the design. Updating code that is
currently running somebody's media, on a machine they may not be sitting at,
is a place to be timid:

  FAST-FORWARD ONLY. Never merge, never rebase, never reset. If the checkout
  has diverged — someone committed a local change — this refuses and says so.
  Reconciling that needs a human who knows which side to keep, and guessing
  wrong destroys work that exists nowhere else.

  NEVER TOUCH A DIRTY TREE. Uncommitted edits to tracked files stop an update
  before it starts. `git merge --ff-only` would refuse anyway, but refusing
  early with a list of the files is a better answer than git's.

  NEVER CLEAN, NEVER FORCE. No `git clean`, no `reset --hard`, no `-f`
  anywhere. The one thing worse than a failed update is one that succeeded by
  deleting something.

  .env IS NEVER IN THE WAY. It is gitignored, so it is untracked and an update
  cannot touch it. Worth stating because it is the file people worry about.

WHAT IT DOES DO. Fetches, reports what is new in plain language, and on request
fast-forwards, reinstalls dependencies if a requirements file changed, and says
which services need restarting. Restarting is left to a person: an update that
bounced the bot mid-film would be its own kind of rude.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Changing one of these means an update is not just new code. Each maps to the
# thing that has to happen before the new code will actually run.
DEPENDENCY_FILES = {
    "requirements.txt": ("bot", "the bot's dependencies"),
    "requirements-dev.txt": ("voice", "voice input packages"),
    "tts/requirements.txt": ("tts", "speech dependencies"),
}


def _git(*args, timeout=120):
    try:
        p = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out"


def _is_repo() -> bool:
    return (ROOT / ".git").exists() and _git("rev-parse", "--git-dir")[0] == 0


def status(fetch: bool = False) -> dict:
    """Where this install stands relative to its remote.

    `fetch` costs a network round trip, so the page asks for it explicitly
    rather than every poll doing it.
    """
    if not _is_repo():
        return {"ok": False, "kind": "not-a-repo",
                "message": "This install is not a git checkout, so it cannot "
                           "update itself. Downloading a zip does that; "
                           "cloning does not.",
                "hint": "git clone https://github.com/captsmuckers/pantheon.git"}

    code, remote, _ = _git("remote", "get-url", "origin")
    if code != 0 or not remote:
        return {"ok": False, "kind": "no-remote",
                "message": "This checkout has no 'origin' remote to update from."}

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")[1]
    if branch == "HEAD":
        return {"ok": False, "kind": "detached",
                "message": "This checkout is on a detached HEAD, not a branch. "
                           "Check out a branch before updating."}

    if fetch:
        code, _, err = _git("fetch", "--quiet", "origin", branch, timeout=180)
        if code != 0:
            return {"ok": False, "kind": "fetch-failed",
                    "message": "Could not reach the repository.",
                    "detail": err[:400]}

    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")[1]
    if not upstream:
        upstream = f"origin/{branch}"

    # --untracked-files=no is deliberate. A fast-forward does not care about
    # files git has never heard of, and blocking an update because somebody
    # left a note.txt in the directory would be absurd. An untracked file that
    # genuinely collides with one the update adds is rare, and git refuses that
    # on its own with a clearer message than a pre-check could invent.
    # Split rather than sliced at a fixed offset. Porcelain is "XY path" with
    # a two-character status, but _git strips its output, which eats the
    # leading space of an unstaged change (" M README.md") and turned the
    # filename into "EADME.md".
    dirty = []
    for ln in _git("status", "--porcelain",
                   "--untracked-files=no")[1].splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2:
            # A rename is reported as "old -> new"; the new name is the useful one.
            dirty.append(parts[1].split(" -> ")[-1].strip())
    counts = _git("rev-list", "--left-right", "--count", f"{upstream}...HEAD")[1]
    try:
        behind, ahead = (int(x) for x in counts.split())
    except ValueError:
        behind, ahead = 0, 0

    out = {"ok": True, "branch": branch, "remote": remote,
           "behind": behind, "ahead": ahead, "dirty": dirty,
           "current": _git("rev-parse", "--short", "HEAD")[1],
           "current_subject": _git("log", "-1", "--format=%s")[1],
           "current_date": _git("log", "-1", "--format=%ad", "--date=short")[1]}

    if behind:
        raw = _git("log", "--format=%h\x1f%ad\x1f%s", "--date=short",
                   f"HEAD..{upstream}")[1]
        out["commits"] = [
            dict(zip(("hash", "date", "subject"), line.split("\x1f", 2)))
            for line in raw.splitlines() if line
        ]
        changed = _git("diff", "--name-only", f"HEAD..{upstream}")[1].splitlines()
        out["changed"] = changed
        out["dependencies"] = [
            {"file": f, "service": DEPENDENCY_FILES[f][0],
             "what": DEPENDENCY_FILES[f][1]}
            for f in changed if f in DEPENDENCY_FILES
        ]

    # Why an update cannot proceed, if it cannot. Checked in the order that
    # produces the most useful message rather than the order git would.
    if ahead and behind:
        out["blocked"] = ("This checkout has diverged — "
                          f"{ahead} local commit{'s' if ahead != 1 else ''} the "
                          "repository does not have. Updating would need a "
                          "merge, which is not something to do unattended.")
    elif ahead:
        out["blocked"] = (f"This checkout is {ahead} commit"
                          f"{'s' if ahead != 1 else ''} ahead of the "
                          "repository. Nothing to update to.")
    elif dirty:
        out["blocked"] = (f"{len(dirty)} tracked file"
                          f"{'s have' if len(dirty) != 1 else ' has'} "
                          "uncommitted changes. Commit or discard them first — "
                          "an update will not overwrite your edits.")
    return out


def plan() -> dict:
    """What applying an update would do, without doing any of it."""
    st = status(fetch=True)
    if not st.get("ok"):
        return st
    st["can_update"] = bool(st.get("behind")) and "blocked" not in st
    return st


def apply() -> dict:
    """Fast-forward to the remote, reinstalling dependencies if they changed.

    Re-checks immediately before acting rather than trusting whatever the page
    was last shown. A tab left open for an hour is not evidence about the
    working tree now, and the checks are cheap.
    """
    st = plan()
    if not st.get("ok"):
        return {"ok": False, "message": st.get("message", "Cannot update.")}
    if st.get("blocked"):
        return {"ok": False, "message": st["blocked"]}
    if not st.get("behind"):
        return {"ok": False, "message": "Already up to date."}

    branch = st["branch"]
    upstream = f"origin/{branch}"

    # --ff-only, and no other git subcommand that can lose work. If this
    # cannot fast-forward, it fails and says so; it does not fall back to
    # anything cleverer.
    steps = [["git", "merge", "--ff-only", upstream]]

    python = ROOT / ".venv" / "bin" / "python"
    tts_python = ROOT / "tts" / ".venv" / "bin" / "python"
    for dep in st.get("dependencies", []):
        f = dep["file"]
        if f == "tts/requirements.txt" and tts_python.exists():
            steps.append([str(tts_python), "-m", "pip", "install", "-r", str(ROOT / f)])
        elif f != "tts/requirements.txt" and python.exists():
            steps.append([str(python), "-m", "pip", "install", "-r", str(ROOT / f)])

    from gui import setup as setup_mod
    n = st["behind"]
    result = setup_mod.start_steps(
        f"Updating {n} commit{'s' if n != 1 else ''}", steps)
    result["restarts"] = _restarts_for(st.get("changed", []))
    return result


def _restarts_for(changed: list) -> list:
    """Which services a set of changed files actually requires bouncing.

    Written as separate questions rather than one condition, because the first
    attempt was a tangle of ands and ors that claimed a README change needed
    the bot restarted and a tts/ change needed both.

    The panel is listed but never restarted automatically: it is the process
    serving the page that asked, and pulling it out from under itself would
    look exactly like a crash.
    """
    def any_in(*prefixes):
        return any(f.startswith(prefixes) for f in changed)

    out = []
    if any_in("tts/"):
        out.append("tts")

    # The bot is everything at the top level that is not the panel, the tests,
    # or the helper scripts — plus its own requirements.
    bot = [f for f in changed
           if (f.endswith(".py")
               and not f.startswith(("gui/", "tests/", "tts/", "scripts/")))
           or f == "requirements.txt"]
    if bot:
        out.append("bot")

    if any_in("gui/"):
        out.append("panel")
    return out
