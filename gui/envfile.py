"""Read and write .env without destroying what is already in it.

This is the only thing in the GUI that writes a file the bot will try to start
from, so it is deliberately the most careful module here.

Four things it has to get right, all of which have already gone wrong once:

  COMMENTS AND ORDER SURVIVE.  .env is not a database dump; it is a file a
  person has been reading and annotating. Rewriting it from a dict would
  silently discard every comment in it and reorder the rest. So an edit is a
  line-by-line rewrite: known keys are substituted in place, everything else is
  copied through byte for byte.

  UNKNOWN KEYS SURVIVE.  A .env written by a newer version, or by hand, will
  contain settings this build has never heard of. Dropping them would turn
  "downgrade for an afternoon" into "lose your configuration". They are copied
  through untouched, exactly like comments.

  THE WRITE IS ATOMIC.  Written to a temporary file in the same directory and
  renamed over the original, so a crash or a full disk leaves the old file
  intact rather than a half-written one. os.replace is atomic within a
  filesystem, which is why the temporary file cannot go in /tmp.

  THE MODE IS 0600.  It holds a Discord token, a Plex token and a Spotify
  client secret. This file was found mode 777 once. It is set explicitly on
  every write rather than trusted to be inherited, and the temporary file is
  created 0600 from the start so there is no window where it is readable.

There is also a smaller trap, which cost a corrupted .env: appending a new key
to a file whose last line has no trailing newline produces `FOO=1BAR=2`, and
python-dotenv reads that as one setting named FOO with a very strange value.
Every append here checks first.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# KEY=value, tolerating `export ` and whitespace around the name, which
# python-dotenv accepts and which people do write by hand.
_LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$", re.S)

# A value that needs quoting when written back. Bare `#` would be read as the
# start of a comment, and leading or trailing space would be stripped.
_NEEDS_QUOTES = re.compile(r'(^\s)|(\s$)|[#\r\n"\']')


def _unquote(raw: str) -> str:
    """The value python-dotenv would see for this raw right-hand side."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        body = raw[1:-1]
        if raw[0] == '"':
            # Only the escapes dotenv actually honours inside double quotes.
            body = (body.replace("\\n", "\n").replace("\\r", "\r")
                        .replace('\\"', '"').replace("\\\\", "\\"))
        return body
    # Unquoted: dotenv treats a ` #` as the start of an inline comment.
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _quote(value: str) -> str:
    """The raw right-hand side to write for this value."""
    if value == "" or not _NEEDS_QUOTES.search(value):
        return value
    body = (value.replace("\\", "\\\\").replace('"', '\\"')
                 .replace("\n", "\\n").replace("\r", "\\r"))
    return f'"{body}"'


def read(path: str | os.PathLike) -> dict:
    """Every setting in the file, as the bot would see it.

    A key repeated in the file resolves to the last occurrence, which is what
    python-dotenv does and what `env_value` in scripts/_common.sh does with its
    `tail -1`. Worth matching exactly: a GUI that disagreed with the launcher
    about which of two lines wins would be maddening to debug.
    """
    out: dict = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text("utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(2)] = _unquote(m.group(4))
    return out


def write(path: str | os.PathLike, changes: dict, *, prune: tuple = ()) -> list:
    """Apply `changes` to the file, and report which keys actually moved.

    Returns the names whose value differs from what was there before. The
    caller needs that and not the full set of submitted keys: a settings form
    posts every field on the page, and restarting the bot because someone
    pressed Save without editing anything would be indefensible.

    `prune` removes keys outright — for a setting that no longer exists rather
    than one being blanked. Anything not in `changes` or `prune` is untouched.
    """
    p = Path(path)
    old = read(p)
    moved = [k for k, v in changes.items() if old.get(k) != v]
    if not moved and not any(k in old for k in prune):
        return []

    lines = (p.read_text("utf-8", errors="replace").splitlines()
             if p.exists() else [])
    seen, out = set(), []
    for line in lines:
        m = _LINE.match(line) if line.strip() and not line.lstrip().startswith("#") else None
        if not m:
            out.append(line)
            continue
        key = m.group(2)
        if key in prune:
            continue
        if key in changes and key not in seen:
            out.append(f"{m.group(1)}{key}{m.group(3)}{_quote(changes[key])}")
        elif key in changes:
            # A duplicate of a key we have already rewritten. read() resolves
            # to the last occurrence, so leaving this one would make the file
            # disagree with what the GUI just showed. Drop it.
            continue
        else:
            out.append(line)
        seen.add(key)

    added = [k for k in changes if k not in seen]
    if added:
        if out and out[-1].strip():
            out.append("")
        out.append("# Added by the Athena settings page.")
        out.extend(f"{k}={_quote(changes[k])}" for k in added)

    _atomic_write(p, "\n".join(out) + "\n")
    return moved


def _atomic_write(p: Path, text: str) -> None:
    """Replace p's contents, or leave them entirely alone."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".env.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        os.chmod(p, 0o600)
    except BaseException:
        # A failed write must not leave the temporary file lying next to the
        # real one, where the next reader could mistake it for a backup.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
