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
import re
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
    "password": None,         # legacy single password; migrated into "users"
    # {name: {"salt","alg","hash","role"}}. role is "admin" or "user".
    "users": {},
    "theme": "dark",
    # Paths, not uploaded blobs. A private key posted through a web form
    # crosses the network — possibly over plain HTTP, since TLS is what you are
    # trying to set up — and is then written by the web server itself. Pointing
    # at a file the operator placed avoids both.
    "tls_cert": None,
    "tls_key": None,
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


ROLES = ("admin", "user")
# What a non-admin may touch. Everything else — every token in .env, security,
# setup, updates, service control — is admin-only, enforced server side in
# gui/server.py. Listed here because it is a policy statement, not a UI detail:
# the settings API filters reads AND writes against it, so a crafted request
# from a signed-in General User still cannot reach DISCORD_TOKEN.
USER_SETTINGS = (
    "TTS_VOICE",            # which built-in timbre
    "TTS_VOICE_DESIGN",     # the voice described in words
    "TTS_VOICE_REF",        # an uploaded clip to clone
    "TTS_VOICE_REF_TEXT",   # what is said in it
    "TTS_QWEN_MODEL",       # preset / described / cloned - the kind of voice
)


def _valid_username(name: str) -> bool:
    """Short, printable, no surprises. Used as a dict key and shown in HTML."""
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name or ""))


def migrate_users(prefs: dict) -> dict:
    """Turn a pre-RBAC single password into an administrator account.

    Called on every load. The old `password` is kept rather than deleted so a
    downgrade still works, and so nobody is locked out by a half-applied
    upgrade — but once `users` exists it is what logins are checked against.
    """
    if prefs.get("users"):
        return prefs
    legacy = prefs.get("password")
    if not legacy:
        return prefs
    prefs["users"] = {"admin": {**legacy, "role": "admin"}}
    return prefs


def list_users(prefs: dict) -> list:
    """Names and roles only. Never the hashes."""
    return sorted(({"name": n, "role": u.get("role", "user")}
                   for n, u in (prefs.get("users") or {}).items()),
                  key=lambda u: (u["role"] != "admin", u["name"]))


def set_user(prefs: dict, name: str, password: str, role: str) -> str:
    """Create or update an account. Returns "" on success, else why not."""
    if not _valid_username(name):
        return "Names may use letters, numbers, dot, dash and underscore, up to 32."
    if role not in ROLES:
        return "Unknown role."
    users = dict(prefs.get("users") or {})
    existing = users.get(name)
    if password:
        salt = secrets.token_bytes(16)
        alg = "scrypt" if _HAS_SCRYPT else "pbkdf2"
        cred = {"salt": salt.hex(), "alg": alg,
                "hash": _derive(password, salt, alg).hex()}
    elif existing:
        cred = {k: existing[k] for k in ("salt", "alg", "hash") if k in existing}
    else:
        return "A new account needs a password."
    users[name] = {**cred, "role": role}
    prefs["users"] = users
    return ""


def delete_user(prefs: dict, name: str) -> str:
    """Remove an account. Refuses to remove the last administrator."""
    users = dict(prefs.get("users") or {})
    if name not in users:
        return "No such account."
    admins = [n for n, u in users.items() if u.get("role") == "admin"]
    if users[name].get("role") == "admin" and len(admins) <= 1:
        # Otherwise the panel becomes unadministrable and the only way back is
        # editing .athena-gui.json by hand — over SSH, on a headless machine.
        return "That is the only administrator. Make someone else an admin first."
    del users[name]
    prefs["users"] = users
    return ""


def check_user(prefs: dict, name: str, password: str) -> str:
    """The role this name and password authenticate as, or "" for no.

    Always does the full derivation, even for an unknown name, so a wrong
    username does not answer faster than a wrong password.
    """
    users = prefs.get("users") or {}
    stored = users.get(name) or {}
    salt_hex = stored.get("salt") or secrets.token_bytes(16).hex()
    want_hex = stored.get("hash") or "00" * 32
    alg = stored.get("alg", "scrypt")
    try:
        got = _derive(password, bytes.fromhex(salt_hex), alg)
        ok = hmac.compare_digest(got, bytes.fromhex(want_hex))
    except (ValueError, TypeError):
        return ""
    return stored.get("role", "user") if ok and stored else ""


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


def tls_paths(prefs: dict):
    """(cert, key) if both are configured and readable, else None."""
    cert, key = prefs.get("tls_cert"), prefs.get("tls_key")
    if not cert or not key:
        return None
    cert, key = Path(cert).expanduser(), Path(key).expanduser()
    if not cert.is_file() or not key.is_file():
        return None
    return cert, key


def tls_context(prefs: dict):
    """An SSLContext, or (None, reason). Never raises the server out of life.

    The certificate is loaded HERE rather than at bind time so a bad path or a
    mismatched key is a message instead of a panel that will not start — which
    would be the worst possible failure, since the panel is how you would fix
    it.
    """
    import ssl
    paths = tls_paths(prefs)
    if paths is None:
        return None, "not configured"
    cert, key = paths
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        # TLS 1.2 floor: everything below it is deprecated, and nothing that
        # would connect to this panel needs it.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx, "ok"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def describe_cert(prefs: dict) -> dict:
    """What the configured certificate actually is, for the settings page."""
    paths = tls_paths(prefs)
    if paths is None:
        cert, key = prefs.get("tls_cert"), prefs.get("tls_key")
        if not cert and not key:
            return {"configured": False}
        return {"configured": True, "valid": False,
                "error": "one of the files is missing or unreadable",
                "cert": cert, "key": key}
    cert, key = paths
    ctx, why = tls_context(prefs)
    info = {"configured": True, "valid": ctx is not None,
            "cert": str(cert), "key": str(key)}
    if ctx is None:
        info["error"] = why
        return info
    try:
        import ssl
        data = ssl._ssl._test_decode_cert(str(cert))
        info["subject"] = dict(x[0] for x in data.get("subject", ()))\
            .get("commonName")
        info["expires"] = data.get("notAfter")
        names = [v for k, v in data.get("subjectAltName", ()) if k == "DNS"]
        info["names"] = names
    except Exception:
        pass
    return info
