"""The control panel: an HTTP server, on the standard library alone.

WHY NOT A FRAMEWORK. aiohttp is already installed — discord.py depends on it —
so it would have been free in the ordinary sense. It was still the wrong
choice, because this is the tool you open when the bot will not start, and
half of the reasons a bot will not start are a broken virtualenv. A control
panel that needs the environment it exists to repair is a control panel that is
missing whenever it is needed. Everything here runs on a bare `python3`, which
is also what makes it possible to serve a setup page before anything is
installed. tts_server.py made the same call for the same reason.

WHAT PROTECTS IT. This binds to loopback and shows a page with a Discord token
field on it, so "only I can reach it" has to survive more than good intentions:

  HOST HEADER.  A page on the public internet can point a domain it controls at
  127.0.0.1 and make your browser issue requests to this server — DNS
  rebinding, and binding to loopback does nothing against it, because the
  request really does come from your machine. Every request must carry a Host
  this server recognises, or it is refused before routing.

  CUSTOM HEADER ON WRITES.  Every mutating request must carry X-Pantheon-CSRF.
  A cross-origin form post cannot set a header, and a cross-origin fetch that
  tries is stopped by the preflight this server never approves.

  SAMESITE COOKIES.  The session cookie is SameSite=Strict and HttpOnly, so it
  is neither sent on a cross-site request nor readable from script.

  A PASSWORD, ONCE IT IS OFF THIS MACHINE.  Loopback with no password is the
  default and is fine: anything that can reach it can already read .env. The
  moment remote access is on, a password is mandatory — enforced in
  prefs.bind_host, not merely in the UI.

WHAT IT NEVER DOES. It never sends a secret to the browser. Not masked, not
partially, not "just this once for editing". A token field renders as whether
something is set and its last four characters, and a save that leaves it
untouched leaves the stored value alone. There is no route that reads one back.
"""

from __future__ import annotations

import json
import mimetypes
import re
import os
import secrets as _secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import schema                                    # noqa: E402
from gui import envfile, logs, pages, prefs, services  # noqa: E402
from gui import setup as setup_mod                     # noqa: E402
from gui import updates as updates_mod                 # noqa: E402
from gui import system as system_mod                   # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
ENV_PATH = ROOT / ".env"

# Sessions live in memory: restarting the GUI signs you out, which is the right
# trade for not persisting a bearer token to disk next to the password hash.
SESSIONS: dict = {}
SESSION_LIFETIME = 12 * 3600
_LOCK = threading.Lock()


def _new_session(user: str = "", role: str = "admin") -> str:
    """A session now remembers WHO, because authorisation depends on it.

    The role is captured at sign-in rather than looked up per request. That is
    deliberate: demoting someone should not silently change what their open
    tabs can do halfway through: it takes effect when they next sign in, and
    an admin who wants it immediate deletes the account, which drops the
    session with it.
    """
    token = _secrets.token_urlsafe(32)
    with _LOCK:
        now = time.time()
        for k, rec in list(SESSIONS.items()):
            if rec["exp"] < now:
                del SESSIONS[k]
        SESSIONS[token] = {"exp": now + SESSION_LIFETIME, "user": user,
                           "role": role}
    return token


def _session_of(token: str) -> dict:
    """The live session record, or {} — expiry is checked here, once."""
    with _LOCK:
        rec = SESSIONS.get(token or "")
        if rec is None:
            return {}
        if rec["exp"] < time.time():
            del SESSIONS[token]
            return {}
        return dict(rec)


def _valid_session(token: str) -> bool:
    return bool(_session_of(token))


def drop_sessions_for(user: str) -> None:
    """Sign a named account out everywhere. Used when it is deleted."""
    with _LOCK:
        for k, rec in list(SESSIONS.items()):
            if rec.get("user") == user:
                del SESSIONS[k]


# Biggest voice reference accepted, in bytes. Generous because the useful
# source is often a long recording the user trims with the start offset, and
# stingy enough that the panel — a single-threaded stdlib server — cannot be
# made to buffer something absurd. Only /api/tts/voice-ref may reach this.
VOICE_REF_MAX = 60_000_000

# WHAT A GENERAL USER MAY REACH. An allowlist, not a blocklist: a route added
# later is admin-only until somebody deliberately opens it, which is the safe
# direction to fail in. Everything absent from here — settings beyond the voice
# fields, security, setup, updates, service control, the panel restart — is
# administrator-only and refused in dispatch, before any handler runs, so it
# cannot be reached by a crafted request from a signed-in friend.
USER_PATHS = frozenset({
    "/", "/voice",                 # status, and the voice page
    "/api/status", "/api/logout",
    "/api/tts/voices", "/api/tts/preview", "/api/tts/voice-ref",
    "/api/tts/health",
    "/api/settings",               # FILTERED by role - see _settings_for_role
    "/api/service",                # RESTRICTED to bot/tts restart - see _service
})

class Handler(BaseHTTPRequestHandler):
    server_version = "athena-gui"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):
        # The access log is noise on a single-user panel, and it would record
        # query strings. Errors still surface through _fail.
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page shows a token field and runs only its own script. Nothing
        # here should ever be framed, sniffed, or allowed to load a remote font.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "media-src 'self' blob:; "
                         "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict, extra: dict = None) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _html(self, body: str, code: int = 200, extra: dict = None) -> None:
        self._send(code, body.encode("utf-8"), "text/html; charset=utf-8", extra)

    def _fail(self, code: int, message: str) -> None:
        if self._wants_json():
            self._json(code, {"ok": False, "error": message})
        else:
            self._html(pages.error_page(code, message), code)

    def _wants_json(self) -> bool:
        return (self.path.startswith("/api/")
                or "application/json" in (self.headers.get("Accept") or ""))

    def _read_body(self) -> None:
        """Consume the request body, ALWAYS, before anything can refuse it.

        HTTP/1.1 keep-alive means the connection is reused. A POST that is
        refused — wrong host, missing CSRF header, not signed in, unknown path
        — without its body being read leaves those bytes in the socket, and the
        next request on that connection is parsed starting from them:

            Bad request syntax ('{"remote_access": true}GET /api/status ...')

        which surfaces as a 400 or, when the leftovers do not parse as a
        method at all, a bare 501 error page with nothing in the log. Reading
        the body up front rather than inside each handler makes it impossible
        for a new early return to reintroduce this.
        """
        self._raw_body = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        # One path keeps more than the rest. Everything here is JSON well under
        # a megabyte except a voice reference, which is an audio file the user
        # picked in the browser — a minute of 48kHz stereo WAV is ~11MB. The
        # cap is per-path rather than global so an ordinary settings POST still
        # cannot be used to make the panel hold 60MB.
        keep = (VOICE_REF_MAX if self.path == "/api/tts/voice-ref"
                else 1_000_000)
        # Still drained when oversized, just not kept: leaving it unread would
        # desynchronise the connection exactly as before.
        remaining = length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
            if length <= keep:
                chunks.append(chunk)
        self._raw_body = b"".join(chunks)

    def _body(self) -> dict:
        try:
            return json.loads(self._raw_body.decode("utf-8"))
        except (ValueError, AttributeError):
            return {}

    def _forwarded_https(self) -> bool:
        """Did this request reach the proxy over HTTPS?

        Only consulted to decide whether the session cookie may carry Secure.
        NOT used for access control: X-Forwarded-* headers are trivially forged
        by anything that can reach the port directly, so trusting them to
        authorise anything would undo the Host check entirely.

        Marking the cookie Secure when the browser did arrive over HTTPS is
        strictly better — it stops the session being replayed over plain HTTP —
        and marking it Secure when it did NOT would break the login loop
        silently, because the browser would refuse to send it back.
        """
        if getattr(self.server, "tls", False):
            return True
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        return proto.split(",")[0].strip() == "https"

    def _session_cookie(self, token: str) -> str:
        parts = [f"athena_session={token}", "Path=/", "HttpOnly",
                 "SameSite=Strict", f"Max-Age={SESSION_LIFETIME}"]
        if self._forwarded_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _cookies(self) -> dict:
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    # ------------------------------------------------------------------- gates

    def _host_ok(self) -> bool:
        """Refuse a Host this server does not answer to.

        This is the defence against DNS rebinding, where a site you visit
        resolves its own domain to 127.0.0.1 so your browser will talk to this
        server on its behalf. The request genuinely originates from this
        machine, so nothing about binding to loopback helps; the Host header is
        what distinguishes "I typed localhost" from "evil.example resolved to
        localhost".
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host in ("localhost", "127.0.0.1", "::1", ""):
            return True
        # With remote access deliberately enabled, this machine's own names and
        # addresses are legitimate too.
        #
        # Note what this does NOT do: resolve the Host header and accept it if
        # it points here. That is precisely the rebinding attack — the
        # attacker's domain really does resolve to this machine. Only names
        # this machine calls itself are accepted, collected once at startup.
        # An explicit allowlist applies whatever the remote-access setting is.
        # It used to be checked only when remote access was on, which meant a
        # reverse proxy on this same machine — a perfectly ordinary setup —
        # got 421 no matter what was configured.
        extra = {str(h).lower() for h in
                 (self.server.prefs.get("extra_hosts") or [])}
        if host.lower() in extra:
            return True

        if self.server.prefs.get("remote_access"):
            return host.lower() in _local_names() or _is_ip_literal(host)
        return False

    def _authed(self) -> bool:
        p = self.server.prefs
        if not prefs.has_password(p):
            # No password set: loopback-only by construction (bind_host refuses
            # otherwise), and anything that can reach loopback can read .env
            # directly. A password would be theatre.
            return True
        return _valid_session(self._cookies().get("athena_session", ""))

    def _session(self) -> dict:
        return _session_of(self._cookies().get("athena_session", ""))

    def _role(self) -> str:
        """"admin" or "user".

        Follows _authed's reasoning exactly: with no password and no accounts
        configured the panel is open and loopback-only, so the caller is the
        operator and is an administrator. Without this a fresh install locks
        ITSELF out — every page 403s before a password can even be set, which
        is the state the panel exists to get you out of.

        Once accounts exist, an unrecognised session is the weaker role.
        """
        p = self.server.prefs
        if not prefs.has_password(p) and not (p.get("users") or {}):
            return "admin"
        return self._session().get("role", "user")

    def _is_admin(self) -> bool:
        return self._role() == "admin"

    def _csrf_ok(self) -> bool:
        return bool(self.headers.get("X-Pantheon-CSRF"))

    # ----------------------------------------------------------------- routing

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        try:
            # Before any gate can refuse the request. See _read_body.
            self._read_body()
            if not self._host_ok():
                self._fail(421, "This server does not answer to that hostname. "
                                "Reach it as http://127.0.0.1 instead.")
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path.startswith("/static/"):
                self._static(path)
                return
            if method == "POST" and path == "/api/login":
                self._login()
                return
            if path == "/login":
                self._html(pages.login_page())
                return

            if not self._authed():
                if self._wants_json():
                    self._json(401, {"ok": False, "error": "not signed in"})
                else:
                    self._html(pages.login_page(), 401)
                return

            # Role gate. After authentication, before anything else: a
            # General User asking for /security gets the same answer whether
            # or not the handler behind it would have worked.
            if not self._is_admin() and path not in USER_PATHS:
                if self._wants_json():
                    self._json(403, {"ok": False,
                                     "error": "Administrators only."})
                else:
                    self._html(pages.forbidden_page(), 403)
                return

            if method == "POST":
                if not self._csrf_ok():
                    self._fail(403, "Missing CSRF header.")
                    return
                self._post(path)
            else:
                self._get(path, query)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._fail(500, "Something went wrong; see the GUI's own output.")
            except Exception:
                pass

    def _get(self, path: str, query: dict):
        if path == "/":
            if not setup_mod.probe()["ready"]:
                self._send(302, b"", "text/plain", {"Location": "/setup"})
                return
            self._html(pages.status_page(self._role()))
        elif path == "/settings":
            self._html(pages.settings_page())
        elif path == "/voice":
            self._html(pages.voice_page(self._role()))
        elif path == "/logs":
            self._html(pages.logs_page())
        elif path == "/security":
            self._html(pages.security_page())
        elif path == "/setup":
            self._html(pages.setup_page())
        elif path == "/api/setup/status":
            self._json(200, setup_mod.probe())
        elif path == "/api/update/status":
            # No fetch: this is polled, and a network round trip per poll
            # would be rude to both the machine and the repository.
            self._json(200, updates_mod.status())
        elif path == "/api/setup/job":
            try:
                since = int((query.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            self._json(200, setup_mod.job((query.get("id") or [""])[0], since))
        elif path == "/api/status":
            self._json(200, {"services": services.status(),
                             "probes": services.probes(),
                             "system": system_mod.snapshot(),
                             "root": str(ROOT)})
        elif path == "/api/settings":
            self._json(200, _settings_payload(self._role()))
        elif path == "/api/logs":
            self._api_logs(query)
        elif path == "/api/logstreams":
            self._json(200, {"streams": logs.available()})
        elif path == "/api/security":
            self._json(200, _security_state(self.server.prefs))
        elif path == "/api/users":
            self._users()
        elif path == "/api/tts/languages":
            self._json(200, _languages())
        elif path == "/api/tts/voices":
            self._json(200, services.voices())
        elif path == "/api/tts/health":
            self._json(200, services.tts_health())
        else:
            self._fail(404, "No such page.")

    def _post(self, path: str):
        body = self._body()
        if path == "/api/service":
            service = str(body.get("service", ""))
            action = str(body.get("action", ""))
            # A General User may bounce the two services their own voice
            # change needs, and nothing else. Without this the feature does
            # not work at all: TTS_VOICE_DESIGN needs the bot restarted and
            # TTS_QWEN_MODEL needs the speech service, so "you may change the
            # voice but not apply it" would be a setting that silently does
            # nothing. Stop is not offered — only restart.
            if not self._is_admin() and (service not in ("bot", "tts")
                                         or action != "restart"):
                self._json(403, {"ok": False,
                                 "error": "Administrators only."})
                return
            result = services.act(service, action)
            self._json(200 if result["ok"] else 500, result)
        elif path == "/api/settings":
            self._json(*_save_settings(body, self._role()))
        elif path == "/api/security":
            code, payload, extra = self._save_security(body)
            self._json(code, payload, extra)
        elif path == "/api/tts/preview":
            self._preview(body)
        elif path == "/api/tts/voice-ref":
            self._voice_ref()
        elif path == "/api/panel/restart":
            self._restart_panel()
        elif path == "/api/users":
            self._save_user(body)
        elif path == "/api/tts/install-language":
            result = services.install_language(str(body.get("lang", "")))
            self._json(200 if result["ok"] else 500, result)
        elif path == "/api/setup/run":
            result = setup_mod.start(str(body.get("action", "")))
            self._json(200 if result["ok"] else 400, result)
        elif path == "/api/update/check":
            self._json(200, updates_mod.plan())
        elif path == "/api/update/apply":
            result = updates_mod.apply()
            self._json(200 if result.get("ok") else 400, result)
        elif path == "/api/logout":
            with _LOCK:
                SESSIONS.pop(self._cookies().get("athena_session", ""), None)
            self._json(200, {"ok": True},
                       {"Set-Cookie": "athena_session=; Path=/; Max-Age=0; HttpOnly"})
        else:
            self._fail(404, "No such endpoint.")

    # ------------------------------------------------------------- individual

    def _static(self, path: str):
        name = os.path.basename(path)
        target = (STATIC / name).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.exists():
            self._fail(404, "No such file.")
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _login(self):
        p = self.server.prefs
        body = self._body()
        name = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not prefs.has_password(p) and not (p.get("users") or {}):
            # Nothing configured yet: first run, before a password is set.
            self._json(200, {"ok": True})
            return

        role = prefs.check_user(p, name, password) if name else ""
        if not role and not name:
            # No username given. Accept the legacy single password so an
            # upgrade does not lock anyone out of their own panel, and sign
            # them in as the administrator it migrated into.
            if prefs.check_password(p, password):
                role = "admin"
                name = "admin"
        if not role:
            # A uniform delay on failure. Not a rate limiter — just enough that
            # a wrong password does not answer measurably faster than a right
            # one. check_user derives even for an unknown name for the same
            # reason, so a wrong USERNAME is not faster either.
            time.sleep(0.5)
            self._json(401, {"ok": False,
                             "error": "Wrong username or password."})
            return
        token = _new_session(name, role)
        self._json(200, {"ok": True, "role": role, "user": name},
                   {"Set-Cookie": self._session_cookie(token)})

    def _users(self):
        """List accounts. Admin-only by the dispatch gate, never hashes."""
        self._json(200, {"users": prefs.list_users(self.server.prefs),
                         "me": self._session().get("user", "")})

    def _save_user(self, body: dict):
        p = self.server.prefs
        action = str(body.get("action", ""))
        name = str(body.get("name", "")).strip()
        me = self._session().get("user", "")

        if action == "delete":
            if name == me:
                self._json(400, {"ok": False,
                                 "error": "That is you. Another admin can remove it."})
                return
            err = prefs.delete_user(p, name)
            if err:
                self._json(400, {"ok": False, "error": err})
                return
            prefs.save(p)
            # Their open tabs stop working now rather than in twelve hours.
            drop_sessions_for(name)
            self._json(200, {"ok": True, "users": prefs.list_users(p)})
            return

        role = str(body.get("role", "user"))
        password = str(body.get("password", ""))
        existing = (p.get("users") or {}).get(name)
        if existing and existing.get("role") == "admin" and role != "admin":
            admins = [n for n, u in (p.get("users") or {}).items()
                      if u.get("role") == "admin"]
            if len(admins) <= 1:
                self._json(400, {"ok": False,
                                 "error": "That is the only administrator."})
                return
        err = prefs.set_user(p, name, password, role)
        if err:
            self._json(400, {"ok": False, "error": err})
            return
        prefs.save(p)
        self._json(200, {"ok": True, "users": prefs.list_users(p)})

    def _preview(self, body: dict):
        """Speak a line with an unsaved voice, so it can be judged before saving."""
        wav, error = services.preview(str(body.get("voice") or ""),
                                      str(body.get("lang") or "auto"),
                                      str(body.get("text") or ""),
                                      str(body.get("instruct") or ""))
        if error is not None:
            self._json(409 if error.get("needs") else 502, {"ok": False, **error})
            return
        self._send(200, wav, "audio/wav", {"Cache-Control": "no-store"})

    def _restart_panel(self):
        """Boot this panel's own launchd job, so it comes back with new code.

        Deliberately Security-only and deliberately restart-only. There is no
        Stop: the panel is the only way in on a headless machine, and a browser
        cannot start it again — a Stop button would be a one-way door.

        The reply must be flushed BEFORE the process dies or the browser sees a
        dropped connection instead of an answer, so the kickstart is scheduled
        on a timer and this returns immediately. launchd's KeepAlive is what
        actually restarts it; kickstart -k just ends the current process.
        """
        import threading

        label = f"gui/{os.getuid()}/com.athena.gui"

        def boot():
            subprocess.run(["launchctl", "kickstart", "-k", label],
                           capture_output=True, timeout=20)

        self._json(200, {"ok": True, "restarting": True})
        try:
            self.wfile.flush()
        except Exception:
            pass
        threading.Timer(0.4, boot).start()

    def _voice_ref(self):
        """Take an uploaded clip and turn it into a usable voice reference.

        The body is the raw file, not multipart: the browser can POST a File
        object directly, and a hand-rolled multipart parser in a stdlib server
        is a much larger thing to get right than this endpoint deserves.

        Returns the settings to apply rather than writing them. The transcript
        comes from Whisper and is occasionally wrong, and the person uploading
        is the one who knows what was said — so it lands in the form for review
        and is saved by the ordinary Save button, like every other setting.
        """
        raw = self._raw_body
        if not raw:
            self._json(400, {"ok": False,
                             "error": "No file received. It may be over the "
                                      f"{VOICE_REF_MAX // 1_000_000}MB limit."})
            return
        name = self.headers.get("X-Voice-Filename") or "upload.wav"
        try:
            start = float(self.headers.get("X-Voice-Start") or 0)
        except ValueError:
            start = 0.0
        result = services.save_voice_ref(raw, name, start)
        self._json(200 if result.get("ok") else 400, result)

    def _api_logs(self, query: dict):
        key = (query.get("stream") or ["athena"])[0]
        path = logs.resolve(key)
        if path is None:
            self._json(200, {"ok": True, "html": "", "offset": 0,
                             "note": "Nothing logged yet."})
            return
        try:
            offset = int((query.get("offset") or ["-1"])[0])
        except ValueError:
            offset = -1
        if offset < 0:
            text, offset = logs.tail(path, 500)
            reset = True
        else:
            text, offset = logs.since(path, offset)
            reset = False
        self._json(200, {"ok": True, "html": logs.as_html(text),
                         "offset": offset, "reset": reset, "name": path.name})

    def _save_security(self, body: dict):
        """Returns (code, payload, extra headers).

        The headers matter: setting a password turns auth on, and the request
        that turned it on came in without a session. Without handing one back,
        the user is signed out by their own action — the very next click 401s,
        which is how "set a password, tick remote access" produced an error
        page rather than a checkbox.
        """
        p = dict(self.server.prefs)
        message, extra = [], {}

        if "password" in body:
            new = str(body.get("password") or "")
            if new and len(new) < 8:
                return 400, {"ok": False, "error": "Use at least 8 characters."}, {}
            if prefs.has_password(p) and not prefs.check_password(
                    p, str(body.get("current") or "")):
                return 403, {"ok": False, "error": "Current password is wrong."}, {}
            p = prefs.set_password(p, new)
            if new:
                # Keep whoever just set it signed in, rather than locking them
                # out of the page they are standing on.
                token = _new_session()
                extra["Set-Cookie"] = self._session_cookie(token)
                message.append("Password set — you are signed in on this browser.")
            else:
                message.append("Password removed, and remote access turned off "
                               "with it.")

        if "tls_cert" in body or "tls_key" in body:
            cert = str(body.get("tls_cert") or "").strip()
            key = str(body.get("tls_key") or "").strip()
            if not cert and not key:
                p["tls_cert"] = p["tls_key"] = None
                message.append("TLS turned off — restart the panel to apply.")
            else:
                trial = dict(p, tls_cert=cert, tls_key=key)
                # Validated before it is saved. Storing a certificate that does
                # not load would leave the panel falling back to HTTP on every
                # restart with the reason only in its log.
                ctx, why = prefs.tls_context(trial)
                if ctx is None:
                    return 400, {"ok": False,
                                 "error": f"That certificate will not load — {why}"}, {}
                p["tls_cert"], p["tls_key"] = cert, key
                message.append("Certificate accepted — restart the panel to "
                               "serve HTTPS.")

        if "extra_hosts" in body:
            raw = body.get("extra_hosts")
            if isinstance(raw, str):
                raw = re.split(r"[,\s]+", raw)
            hosts = []
            for h in (raw or []):
                h = str(h).strip().lower().rstrip(".")
                # A hostname only. Anything with a scheme, path or port is a
                # sign somebody pasted a URL, and silently accepting it would
                # produce an allowlist entry that never matches a Host header.
                if not h:
                    continue
                if "/" in h or ":" in h:
                    return 400, {"ok": False,
                                 "error": f"{h!r} should be just a hostname — "
                                          "no https://, no port, no path."}, {}
                hosts.append(h)
            p["extra_hosts"] = sorted(set(hosts))
            message.append(f"{len(hosts)} extra hostname"
                           f"{'' if len(hosts) == 1 else 's'} allowed."
                           if hosts else "Extra hostnames cleared.")

        if "remote_access" in body:
            want = bool(body["remote_access"])
            if want and not prefs.has_password(p):
                return 400, {"ok": False,
                             "error": "Set a password before enabling remote "
                                      "access."}, {}
            if want != p.get("remote_access"):
                p["remote_access"] = want
                message.append("Remote access " + ("enabled" if want else "disabled")
                               + " — restart the GUI for it to take effect.")

        prefs.save(p)
        self.server.prefs = p
        return 200, {"ok": True, "message": " ".join(message) or "Nothing changed.",
                     "state": _security_state(p)}, extra


_LOCAL_NAMES = None


def _local_names() -> set:
    """Every name this machine answers to, lower-cased. Computed once.

    socket.gethostname() alone is not enough on macOS. It returns the
    DNS-ish name ("something.localdomain") while Bonjour advertises a
    SEPARATE LocalHostName ("Something-MacBook-Pro.local") — and the .local
    name is how anyone actually reaches a Mac on a LAN. Deriving one from the
    other does not work; they are different strings. Accessing the panel by
    its Bonjour name was refused with 421 until this was collected properly.

    Extra names can be added in .athena-gui.json as "extra_hosts", for a
    router-assigned name or a reverse proxy, since no amount of local
    inspection can discover those.
    """
    global _LOCAL_NAMES
    if _LOCAL_NAMES is not None:
        return _LOCAL_NAMES

    names = {"localhost"}
    try:
        hostname = socket.gethostname()
        names |= {hostname, hostname.split(".")[0], hostname + ".local"}
        names.add(socket.getfqdn())
    except OSError:
        pass

    if sys.platform == "darwin":
        for key in ("LocalHostName", "ComputerName", "HostName"):
            try:
                out = subprocess.run(["scutil", "--get", key],
                                     capture_output=True, text=True, timeout=5)
                value = out.stdout.strip()
                if value:
                    names |= {value, value + ".local"}
            except (OSError, subprocess.SubprocessError):
                pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            names.add(info[4][0])
    except (OSError, socket.gaierror):
        pass

    _LOCAL_NAMES = {n.lower().rstrip(".") for n in names if n}
    return _LOCAL_NAMES


def _is_ip_literal(host: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            continue
    return False


def _security_state(p: dict) -> dict:
    return {"has_password": prefs.has_password(p),
            "remote_access": bool(p.get("remote_access")),
            "bind": prefs.bind_host(p), "port": p.get("port"),
            "extra_hosts": list(p.get("extra_hosts") or []),
            "known_hosts": sorted(_local_names()),
            "tls": prefs.describe_cert(p)}


# ---------------------------------------------------------------------- settings

def _settings_payload(role: str = "admin") -> dict:
    """Every setting, its current value, and its description — minus the secrets.

    A secret is reported as whether one is stored and its last four characters,
    which is enough to tell "the token I pasted" from "the token from before I
    rotated it" and not enough to be worth stealing. The value itself never
    leaves this process.
    """
    current = envfile.read(ENV_PATH)
    fields = []
    # A General User sees ONLY the voice fields, and this is where that is
    # enforced for reads. Filtering the payload rather than hiding fields in
    # the browser is the whole point: the values include every token in .env,
    # so a hidden field would still be one devtools panel away.
    allowed = None if role == "admin" else set(prefs.USER_SETTINGS)
    for s in schema.for_platform(sys.platform):
        if allowed is not None and s.name not in allowed:
            continue
        entry = {
            "name": s.name, "kind": s.kind, "help": s.help, "section": s.section,
            "required": s.required, "advanced": s.advanced, "restart": s.restart,
            "choices": list(s.choices), "lo": s.lo, "hi": s.hi,
            "applies": list(s.applies),
            "default": s.default if not isinstance(s.default, bool)
                       else ("true" if s.default else "false"),
        }
        if s.is_secret():
            stored = current.get(s.name, "")
            entry["set"] = bool(stored)
            entry["hint"] = f"…{stored[-4:]}" if len(stored) >= 4 else ""
            entry["value"] = ""
        else:
            # A setting absent from .env is not blank — the bot uses the
            # default, so that is what the page must show. Reporting "" instead
            # made a choice field render its value as unrecognised and offer a
            # phantom "(not a listed choice)" option for the empty string.
            entry["value"] = current.get(s.name, entry["default"])
            entry["explicit"] = s.name in current
        fields.append(entry)
    sections = (list(schema.SECTIONS) if allowed is None
                else sorted({f["section"] for f in fields}))
    # The engine and mode currently in .env, sent regardless of whether the
    # fields themselves were. A General User cannot change TTS_ENGINE and so
    # is not sent it — without this the page would read no engine dropdown,
    # assume the default, and hide exactly the fields they came to use.
    engine = (current.get("TTS_ENGINE") or "kokoro").strip() or "kokoro"
    model = (current.get("TTS_QWEN_MODEL") or "").lower()
    if engine != "qwen":
        context = engine
    elif "customvoice" in model:
        context = "qwen:customvoice"
    elif "voicedesign" in model:
        context = "qwen:voicedesign"
    else:
        context = "qwen:base"
    return {"fields": fields, "sections": sections, "role": role,
            "context": context,
            "env_exists": ENV_PATH.exists(), "env_path": str(ENV_PATH)}


def _languages() -> dict:
    """Which languages the SERVING process can actually phonemise.

    Read from the speech server's /health rather than kept here, because the
    answer depends on what is installed in the TTS virtualenv — which the panel
    cannot import and must not guess at. A second copy of this list would drift
    the moment somebody installed a package.
    """
    health = services.tts_health()
    if health.get("error"):
        return {"ok": False, "error": health["error"],
                "message": "The speech service is not answering, so the panel "
                           "cannot tell which languages are installed.",
                "languages": {}}
    return {"ok": True, "languages": health.get("languages", {}),
            "active": health.get("lang", ""), "setting": health.get("lang_setting", ""),
            "installable": {k: v.get("note", "")
                            for k, v in services.LANGUAGE_PACKAGES.items()}}


def _save_settings(body: dict, role: str = "admin"):
    """Validate, then write, then say what needs restarting.

    Validation is total before anything is written: a form with one bad number
    in it leaves .env exactly as it was, rather than applying the nine good
    values and reporting a failure the user then has to reconstruct.
    """
    values = body.get("values") or {}
    clear = [str(k) for k in (body.get("clear") or [])]
    if not isinstance(values, dict):
        return 400, {"ok": False, "error": "Malformed submission."}

    # The same allowlist as the read path, applied to writes. Refused rather
    # than silently dropped: a General User who somehow submits DISCORD_TOKEN
    # should be told no, not left believing it saved.
    if role != "admin":
        forbidden = sorted((set(values) | set(clear)) - set(prefs.USER_SETTINGS))
        if forbidden:
            return 403, {"ok": False,
                         "error": "Administrators only: " + ", ".join(forbidden)}

    # Settings that become command-line arguments to the speech service, and
    # so cannot contain whitespace. Learned the hard way: a placeholder option
    # in the voice lab was submitted as TTS_VOICE, written here, and passed as
    # --voice "- load this kind first to list its voices -", which argparse
    # rejected. The service then crash-looped at boot and the panel had no way
    # to tell you why. Validating here means the UI cannot write a value that
    # stops the thing booting, whatever the UI does.
    ARGV_SAFE = {"TTS_VOICE": "a voice name like Serena or bf_emma",
                 "TTS_LANG_CODE": "a single language letter"}

    changes, errors = {}, {}
    for name, raw in values.items():
        if name in ARGV_SAFE and raw and str(raw).split() != [str(raw)]:
            errors[name] = (f"must be {ARGV_SAFE[name]} - no spaces. "
                            f"Got {str(raw)[:40]!r}")
            continue
        if name not in schema.BY_NAME:
            errors[name] = "not a setting this version knows about"
            continue
        raw = "" if raw is None else str(raw)
        # An untouched secret arrives empty and means "leave it alone", which
        # is the only reason a blank secret is not treated as a clear. Clearing
        # one is a separate, deliberate action.
        if schema.BY_NAME[name].is_secret() and not raw:
            continue
        problem = schema.validate(name, raw)
        if problem:
            errors[name] = problem
        else:
            changes[name] = raw.strip() if schema.BY_NAME[name].kind != "text" else raw

    for name in clear:
        if name not in schema.BY_NAME:
            errors[name] = "not a setting this version knows about"
        elif schema.BY_NAME[name].required:
            errors[name] = f"{name} is required; the bot will not start without it"
        else:
            changes[name] = ""

    if errors:
        return 400, {"ok": False, "error": "Nothing was saved.", "fields": errors}

    moved = envfile.write(ENV_PATH, changes)
    return 200, {"ok": True, "changed": moved,
                 "restarts": sorted(schema.restarts_for(moved)),
                 "message": _saved_message(moved)}


def _saved_message(moved: list) -> str:
    if not moved:
        return "No changes to save."
    what = sorted(schema.restarts_for(moved))
    names = {"bot": "Athena", "tts": "Speech"}
    if not what:
        return f"Saved {len(moved)} setting{'s' if len(moved) != 1 else ''}."
    # config.py calls load_dotenv() once at import and freezes the result, so a
    # saved setting genuinely does nothing until the process restarts. Saying so
    # every time is far better than a page that appears to apply changes live.
    return (f"Saved {len(moved)} setting{'s' if len(moved) != 1 else ''}. "
            f"Restart {' and '.join(names[w] for w in what)} to apply.")


# -------------------------------------------------------------------- lifecycle

def serve(port: int = None, host: str = None) -> None:
    p = prefs.load()
    # Turn a pre-RBAC single password into an administrator account, once, at
    # startup. Persisted immediately so the accounts page is not empty on a
    # panel that has always had a password.
    before = dict(p.get("users") or {})
    p = prefs.migrate_users(p)
    if (p.get("users") or {}) != before:
        prefs.save(p)
    host = host or prefs.bind_host(p)
    port = port or int(p.get("port") or 8086)

    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.prefs = p

    # TLS, if a usable certificate is configured. A broken one must never stop
    # the panel starting: this is the tool you would use to fix the setting,
    # so it falls back to plain HTTP and says why, loudly.
    scheme = "http"
    ctx, why = prefs.tls_context(p)
    if ctx is not None:
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        httpd.tls = True
        scheme = "https"
    else:
        httpd.tls = False
        if why != "not configured":
            print(f"  TLS is configured but unusable — {why}")
            print("  serving plain HTTP instead; fix it on the Security page")

    where = f"{scheme}://127.0.0.1:{port}"
    print(f"Athena control panel  {where}")
    print(f"  checkout   {ROOT}")
    print(f"  listening  {host}:{port}"
          + ("" if host == "127.0.0.1" else "   (reachable from the network)"))
    if not prefs.has_password(p):
        print("  no password set — fine on loopback, required before remote access")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Athena's control panel.")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host", help="override the bind address (normally decided "
                                   "by the remote-access setting)")
    args = ap.parse_args()
    serve(args.port, args.host)
