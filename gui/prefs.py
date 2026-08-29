"""The GUI's own settings, which are not the bot's.

Deliberately a separate file from .env. Three reasons, and the third is the one
that matters:

  * .env is the bot's input. A password for a control panel is not a thing the
    bot should be handed at startup, and putting it there would mean every
    process that reads config also holds it.
  * .env is what a person edits by hand and pastes into a support thread. This
    file is not.
  * .env is generated from schema.py, and a test asserts that every setting in
    one appears in the other. A GUI password in .env would either fail that
    test or have to be excused from it, and excusing things from that test is
    how the schema starts lying.

The password is stored as an scrypt hash, never as the password. scrypt is in
hashlib, so this costs no dependency, and it is memory-hard, which matters
because the realistic attack is an offline guess against a file someone has
already copied — not an online one against a page that could rate-limit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFS = ROOT / ".athena-gui.json"

# Cost parameters. n=2**15 keeps a single verification well under a second on
# this class of machine — unnoticeable to someone typing a password, and
# expensive enough to make bulk guessing against a stolen file miserable.
#
# maxmem is not optional. scrypt needs 128*n*r bytes — 32MB at these settings —
# and OpenSSL's default ceiling is exactly 32MB, so leaving it out raises
# "memory limit exceeded" rather than choosing softer parameters. Stated here
# so nobody quietly lowers n to make that error go away.
_N, _R, _P = 1 << 15, 8, 1
_MAXMEM = 128 * _N * _R * 2

# scrypt is not always there. The stock /usr/bin/python3 on macOS is built
# against an OpenSSL without it, and hashlib.scrypt simply does not exist —
# which matters because running on that interpreter, before anything is
# installed, is the whole point of this panel. Setting a first password raised
# AttributeError and the page returned a bare 500.
#
# PBKDF2-HMAC-SHA256 is the fallback: always present, not memory-hard, so the
# iteration count carries the cost instead. Which algorithm was used is stored
# with the hash, so a password set on one interpreter still verifies on the
# other and neither has to guess.
_HAS_SCRYPT = hasattr(hashlib, "scrypt")
_PBKDF2_ROUNDS = 600_000


def _derive(password: str, salt: bytes, alg: str) -> bytes:
    if alg == "scrypt":
        return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                              n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               _PBKDF2_ROUNDS)

DEFAULTS = {
    "port": 8086,             # 8085 is the TTS server; do not collide with it
    "remote_access": False,   # bind beyond loopback. Requires a password.
    "password": None,         # {"salt": hex, "hash": hex}
    "theme": "dark",
}


def load() -> dict:
    out = dict(DEFAULTS)
    if PREFS.exists():
        try:
            out.update(json.loads(PREFS.read_text("utf-8")))
        except (ValueError, OSError):
            # A corrupt prefs file must not stop the GUI from starting: the GUI
            # is the tool you reach for when things are broken.
            pass
    return out


def save(prefs: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(ROOT), prefix=".athena-gui.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(prefs, fh, indent=2, sort_keys=True)
        os.replace(tmp, PREFS)
        os.chmod(PREFS, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_password(prefs: dict, password: str) -> dict:
    """Hash and store a password. An empty one removes it."""
    if not password:
        prefs["password"] = None
        # Removing the password must also close the door it was holding open.
        # Leaving remote access on with no password is the single worst state
        # this file could be left in, so it is made unreachable rather than
        # merely discouraged in the UI.
        prefs["remote_access"] = False
        return prefs
    salt = secrets.token_bytes(16)
    alg = "scrypt" if _HAS_SCRYPT else "pbkdf2"
    prefs["password"] = {"salt": salt.hex(), "alg": alg,
                         "hash": _derive(password, salt, alg).hex()}
    return prefs


def check_password(prefs: dict, password: str) -> bool:
    stored = prefs.get("password")
    if not stored:
        return False
    try:
        salt = bytes.fromhex(stored["salt"])
        want = bytes.fromhex(stored["hash"])
    except (KeyError, ValueError):
        return False
    # Default to scrypt for a hash written before the algorithm was recorded.
    alg = stored.get("alg", "scrypt")
    if alg == "scrypt" and not _HAS_SCRYPT:
        return False
    try:
        got = _derive(password, salt, alg)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(got, want)


def has_password(prefs: dict) -> bool:
    return bool(prefs.get("password"))


def bind_host(prefs: dict) -> str:
    """The address to listen on.

    Remote access is ignored unless a password is actually set. The UI already
    refuses to enable one without the other, but this is the check that counts:
    a hand-edited JSON file saying {"remote_access": true, "password": null}
    would otherwise publish a Discord token to the LAN.
    """
    if prefs.get("remote_access") and has_password(prefs):
        return "0.0.0.0"
    return "127.0.0.1"
