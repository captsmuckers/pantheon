"""Finding, tailing and following the log files.

Small module, one genuine hazard: this is the only place in the GUI that turns
something from a URL into a path on disk. `?log=../../.env` reads the Discord
token if that is allowed to work, and the page is one checkbox away from being
reachable off this machine. So nothing here takes a path — callers pass a KEY
from a fixed set, and the set is built by scanning the logs directory. A name
that did not come from that scan cannot be read, whatever it looks like.

Logs are also large — a day's worth of bot log reaches a few hundred kilobytes
— and are read while being written. Both tails seek from the end rather than
reading the file in, and the follower reports the byte offset it stopped at so
the page can ask for "everything since" instead of re-reading the whole file
every few seconds.
"""

from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGDIR = ROOT / "logs"

# The bot writes one file per run (scripts/_common.sh new_log), so "the bot
# log" means the newest athena-*.log, not a fixed name. Sorted by modification
# time rather than by the timestamp in the name: a run that outlives a newer
# one — which happens when a supervised restart overlaps — should still be the
# one you are shown.
STREAMS = (
    ("athena", "Athena", "athena-*.log", "what the bot did"),
    ("tts", "Speech", "tts-*.log", "what it said, and how long it took to say it"),
    ("mpv", "Player", "mpv-stderr.log", "mpv's own complaints"),
    ("launchd", "Supervisor", "athena-launchd-stderr.log",
     "why launchd could not start it"),
)

LEVELS = re.compile(r"\b(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\b")


def available() -> list:
    """Every log a person may ask for, newest first within each stream."""
    out = []
    for key, title, pattern, why in STREAMS:
        matches = sorted(LOGDIR.glob(pattern), key=lambda p: p.stat().st_mtime
                         if p.exists() else 0, reverse=True)
        if not matches:
            out.append({"key": key, "title": title, "why": why, "path": None,
                        "size": 0, "mtime": 0, "runs": 0})
            continue
        newest = matches[0]
        st = newest.stat()
        out.append({"key": key, "title": title, "why": why,
                    "path": str(newest), "name": newest.name,
                    "size": st.st_size, "mtime": st.st_mtime,
                    "runs": len(matches)})
    return out


def resolve(key: str) -> Path | None:
    """The file for a stream key, or None. The only key→path step there is."""
    for entry in available():
        if entry["key"] == key and entry["path"]:
            p = Path(entry["path"]).resolve()
            # Belt and braces: the key came from our own scan, so this cannot
            # currently fail, but it is the assertion that keeps it that way if
            # STREAMS ever grows a pattern with a slash in it.
            if p.is_relative_to(LOGDIR.resolve()):
                return p
    return None


def tail(path: Path, lines: int = 400) -> tuple:
    """(text, offset) — the last `lines` lines, and the size they end at.

    Reads backwards in blocks. A bot log that has been running all day is a few
    hundred kilobytes and there is no reason to pull all of it through memory
    to show the last screenful.
    """
    if not path or not path.exists():
        return "", 0
    size = path.stat().st_size
    block, data, pos = 65536, b"", size
    with open(path, "rb") as fh:
        while pos > 0 and data.count(b"\n") <= lines:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:]), size


def since(path: Path, offset: int) -> tuple:
    """(new text, new offset) — whatever was appended after `offset`.

    A file smaller than the offset has been rotated or replaced (each run gets
    its own file, so this happens on every restart). Starting again from the
    beginning is right: the alternative is a page that silently stops updating
    after a restart, which looks exactly like a bot that has stopped logging.
    """
    if not path or not path.exists():
        return "", offset
    size = path.stat().st_size
    if size < offset:
        offset = 0
    if size == offset:
        return "", offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    return data.decode("utf-8", errors="replace"), size


def as_html(text: str) -> str:
    """Escaped, with the level word wrapped so CSS can colour it.

    Escaped FIRST and then marked up: a log line contains whatever a Discord
    user typed, and this is rendered into a page that can hold a token field.
    """
    out = []
    for line in text.splitlines():
        safe = html.escape(line)
        safe = LEVELS.sub(lambda m: f'<b class="lv {m.group(1).lower()}">{m.group(1)}</b>',
                          safe, count=1)
        out.append(safe)
    return "\n".join(out)
