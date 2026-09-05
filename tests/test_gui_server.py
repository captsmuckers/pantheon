"""The control panel serves a page with a Discord token field on it.

So this suite is mostly about what the server REFUSES. Every check here
corresponds to a way the panel could leak a credential or be driven by
somebody else's web page, and each is cheap to break by accident later: a
convenience route added without the auth gate, a template that renders a value
it should redact, a CORS header added to make a fetch work.

The server is started on an ephemeral port against a temporary checkout, so
nothing here touches the real .env or the real services.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


SECRET = "a-token-that-must-never-be-served-back-9f3c"

ENV = f"""\
# Discord
DISCORD_TOKEN={SECRET}
ALLOWED_CHANNEL_ID=000000000000000001
PLEX_URL=http://127.0.0.1:32400
PLEX_TOKEN=another-secret-value-4b7d
OLLAMA_MODEL=qwen3:8b
FLAVOR_MAX_LENGTH=120
SOME_SETTING_FROM_THE_FUTURE=keep-me
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, path, method="GET", body=None, headers=None, host=None, raw=False):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if host:
        req.add_header("Host", host)
    # raw for endpoints that answer with audio: decoding a WAV as UTF-8 throws.
    decode = (lambda b: b) if raw else (lambda b: b.decode("utf-8", "replace"))
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, decode(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, decode(e.read()), dict(e.headers)


class Panel:
    """A control panel serving a throwaway checkout."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="athena-gui-"))
        for item in ("gui", "schema.py"):
            src = ROOT / item
            dst = self.dir / item
            shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
        (self.dir / ".env").write_text(ENV, encoding="utf-8")
        os.chmod(self.dir / ".env", 0o600)
        (self.dir / "logs").mkdir(exist_ok=True)
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "gui.server", "--port", str(self.port),
             "--host", "127.0.0.1"],
            cwd=str(self.dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                request(self.port, "/api/status")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("panel did not start")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)

    def env(self) -> str:
        return (self.dir / ".env").read_text("utf-8")


def test_every_page_renders(p):
    """The check that was missing when /security shipped raising a TypeError.

    The suite tested the JSON routes thoroughly and never simply asked for the
    pages, so a page handler called with the wrong arguments passed every test
    and 500'd the moment it was opened in a browser.
    """
    print("every page in the nav actually renders:")
    for path in ("/", "/settings", "/logs", "/security", "/login"):
        code, text, headers = request(p.port, path)
        ok = code == 200 and "text/html" in headers.get("Content-Type", "")
        check(f"{path} renders", ok and "Something went wrong" not in text, str(code))
    for path in ("/static/app.css", "/static/app.js", "/static/settings.js",
                 "/static/status.js", "/static/logs.js", "/static/security.js",
                 "/static/login.js"):
        code, _, _ = request(p.port, path)
        check(f"{path} is served", code == 200, str(code))
    code, _, _ = request(p.port, "/no-such-page")
    check("an unknown page is a 404, not a 500", code == 404, str(code))


def test_secrets_never_leave_the_process(p):
    print("no response ever contains a stored secret:")
    seen = []
    for path in ("/", "/settings", "/logs", "/security", "/api/status",
                 "/api/settings", "/api/security", "/api/logstreams"):
        _, text, _ = request(p.port, path)
        if SECRET in text:
            seen.append(path)
    check("the token appears in no response", not seen, str(seen))

    _, text, _ = request(p.port, "/api/settings")
    field = next(f for f in json.loads(text)["fields"] if f["name"] == "DISCORD_TOKEN")
    check("a set token reports only that it is set", field["value"] == "" and field["set"])
    check("the hint is the last four characters", field["hint"] == "…9f3c", field["hint"])


def test_dns_rebinding(p):
    print("\na page on the internet cannot drive this server:")
    code, _, _ = request(p.port, "/api/status", host="evil.example")
    check("a foreign Host is refused", code == 421, str(code))
    code, _, _ = request(p.port, "/api/status", host="localhost")
    check("localhost is allowed", code == 200, str(code))
    code, _, _ = request(p.port, "/api/status", host=socket.gethostname())
    check("this machine's own name is refused while remote access is off",
          code == 421, str(code))


def test_csrf(p):
    print("\nmutating requests need a header a cross-origin form cannot set:")
    code, _, _ = request(p.port, "/api/settings", "POST", {"values": {}})
    check("POST without the header is refused", code == 403, str(code))
    code, _, _ = request(p.port, "/api/settings", "POST", {"values": {}},
                         {"X-Pantheon-CSRF": "1"})
    check("POST with the header is accepted", code == 200, str(code))


def test_headers(p):
    print("\nthe page cannot be framed, sniffed, or made to load anything remote:")
    _, _, headers = request(p.port, "/")
    csp = headers.get("Content-Security-Policy", "")
    check("a content security policy is set", "default-src 'self'" in csp, csp)
    check("framing is refused", "frame-ancestors 'none'" in csp, csp)
    check("no MIME sniffing", headers.get("X-Content-Type-Options") == "nosniff")
    check("no CORS header is offered", "Access-Control-Allow-Origin" not in headers)


def test_traversal(p):
    print("\nnothing outside the served directories can be read:")
    for path in ("/static/../.env", "/static/..%2f.env", "/static/../../etc/passwd"):
        code, text, _ = request(p.port, path)
        check(f"{path} is refused", code == 404 and SECRET not in text, str(code))
    _, text, _ = request(p.port, "/api/logs?stream=../../.env")
    check("a log stream cannot name an arbitrary file", SECRET not in text)


def test_saving(p):
    print("\nsaving settings is all-or-nothing, and leaves the rest of .env alone:")
    hdr = {"X-Pantheon-CSRF": "1"}
    before = p.env()

    code, text, _ = request(p.port, "/api/settings", "POST",
                            {"values": {"OLLAMA_MODEL": "granite4:3b",
                                        "FLAVOR_MAX_LENGTH": "not-a-number"}}, hdr)
    check("one bad value rejects the whole submission", code == 400, str(code))
    check("nothing was written", p.env() == before)
    check("the bad field is named", "FLAVOR_MAX_LENGTH" in json.loads(text)["fields"])

    code, text, _ = request(p.port, "/api/settings", "POST",
                            {"values": {"OLLAMA_MODEL": "granite4:3b"}}, hdr)
    body = json.loads(text)
    check("a valid save succeeds", code == 200 and body["changed"] == ["OLLAMA_MODEL"],
          str(body))
    check("it says what to restart", body["restarts"] == ["bot"], str(body["restarts"]))
    check("the new value is on disk", "OLLAMA_MODEL=granite4:3b" in p.env())
    check("comments survive", "# Discord" in p.env())
    check("a setting this build does not know survives",
          "SOME_SETTING_FROM_THE_FUTURE=keep-me" in p.env())
    check("the token is untouched", f"DISCORD_TOKEN={SECRET}" in p.env())

    # Opening the page and pressing Save must not wipe every credential: a
    # secret arrives blank because the server never sent its value.
    code, text, _ = request(p.port, "/api/settings", "POST",
                            {"values": {"DISCORD_TOKEN": ""}}, hdr)
    check("a blank secret means leave it alone, not clear it",
          json.loads(text)["changed"] == [] and f"DISCORD_TOKEN={SECRET}" in p.env())

    code, text, _ = request(p.port, "/api/settings", "POST",
                            {"values": {}, "clear": ["DISCORD_TOKEN"]}, hdr)
    check("a required secret cannot be cleared", code == 400 and SECRET in p.env())

    code, text, _ = request(p.port, "/api/settings", "POST",
                            {"values": {"NOT_A_REAL_SETTING": "x"}}, hdr)
    check("an unknown setting is refused", code == 400, str(code))

    check("the file is still mode 0600",
          (Path(p.dir) / ".env").stat().st_mode & 0o777 == 0o600)


def test_remote_access_needs_a_password(p):
    print("\nremote access cannot be enabled without a password:")
    hdr = {"X-Pantheon-CSRF": "1"}
    code, text, _ = request(p.port, "/api/security", "POST",
                            {"remote_access": True}, hdr)
    check("refused with no password set", code == 400, str(code))
    _, text, _ = request(p.port, "/api/security")
    check("still listening on loopback only",
          json.loads(text)["bind"] == "127.0.0.1", text)

    code, text, _ = request(p.port, "/api/security", "POST", {"password": "short"}, hdr)
    check("a short password is refused", code == 400, str(code))

    code, _, _ = request(p.port, "/api/security", "POST",
                         {"password": "a-long-enough-password"}, hdr)
    check("a real password is accepted", code == 200, str(code))

    # And now that a password exists, everything needs a session.
    code, _, _ = request(p.port, "/api/settings")
    check("the settings API now requires signing in", code == 401, str(code))
    code, text, _ = request(p.port, "/api/login", "POST", {"password": "wrong"}, hdr)
    check("a wrong password is rejected", code == 401, str(code))


def test_language_install_is_a_fixed_menu(p):
    """The install endpoint runs pip. What it can install must not be an input.

    This is the one route in the panel that executes a package manager, so the
    test that matters is not that it installs correctly — it is that nothing
    reaching it from a request can change WHAT gets installed.
    """
    print("\nthe language installer cannot be pointed at arbitrary packages:")
    hdr = {"X-Pantheon-CSRF": "1"}
    for evil in ("a", "", "requests", "misaki[ja]", "../../evil",
                 "j; rm -rf /", "-e /tmp/x", "j z"):
        code, text, _ = request(p.port, "/api/tts/install-language", "POST",
                                {"lang": evil}, hdr)
        body = json.loads(text)
        refused = not body.get("ok") and "No installable package" in body.get("message", "")
        check(f"{evil!r} refused", refused, body.get("message", "")[:60])

    from gui import services
    check("only two packages are installable ever",
          set(services.LANGUAGE_PACKAGES) == {"j", "z"},
          str(sorted(services.LANGUAGE_PACKAGES)))
    check("every entry names a literal package",
          all(isinstance(v["pip"], str) and v["pip"].startswith("misaki[")
              for v in services.LANGUAGE_PACKAGES.values()))


def test_preview_needs_the_speech_service(p):
    """A preview with nothing listening must explain itself, not just fail.

    The panel under test talks to 127.0.0.1:8085 like the real one does. On a
    machine where that is not running, this asserts the error is legible; where
    it IS running, a preview is real audio.
    """
    print("\npreviewing a voice:")
    hdr = {"X-Pantheon-CSRF": "1"}
    # No voice named, deliberately. Asking for "bf_emma" hardcoded a KOKORO
    # name into a test that runs against whatever engine is configured, and it
    # broke the moment this machine moved to Qwen: the service was up and
    # correct, and refused a voice belonging to another engine. Omitting it
    # exercises what the Test button actually does — preview the CONFIGURED
    # voice — and works whichever engine is serving.
    code, data, headers = request(p.port, "/api/tts/preview", "POST",
                                  {"lang": "auto"}, hdr, raw=True)
    if code == 200:
        check("returns audio", "audio/wav" in headers.get("Content-Type", ""),
              headers.get("Content-Type", ""))
        check("it is a real RIFF/WAVE file", data[:4] == b"RIFF" and data[8:12] == b"WAVE",
              repr(data[:12]))
        check("the audio is not empty", len(data) > 1000, f"{len(data)} bytes")
    else:
        body = json.loads(data.decode("utf-8", "replace"))
        # Legible means it names what went wrong, not that it names one
        # specific failure: the service can be down, or up and refusing the
        # voice. Both are fine to report; a bare "synthesis failed" is not.
        err = body.get("error", "").lower()
        check("the failure is legible",
              bool(err) and ("speech service" in err or "no such voice" in err
                             or "not answering" in err),
              str(body)[:90])


def test_the_machines_own_names_are_accepted(p):
    """With remote access on, this machine's own names must work.

    socket.gethostname() alone is not enough on macOS: it returns the DNS-ish
    name ("x.localdomain") while Bonjour advertises a SEPARATE LocalHostName
    ("X-MacBook-Pro.local") — and the .local name is how anyone actually
    reaches a Mac on a LAN. Reaching the panel by its Bonjour name was refused
    with 421 while every other name worked.
    """
    print("\nthe host allowlist covers what this machine is really called:")
    from gui import server as srv
    names = srv._local_names()
    check("localhost is in it", "localhost" in names)
    check("the hostname is in it",
          socket.gethostname().lower() in names, socket.gethostname())

    if sys.platform == "darwin":
        import subprocess as sp
        try:
            local = sp.run(["scutil", "--get", "LocalHostName"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            local = ""
        if local:
            check("the Bonjour .local name is in it",
                  f"{local.lower()}.local" in names, f"{local}.local")

    # And the whole point of the check still holds.
    check("an attacker's domain is not",
          "evil.example" not in names and "attacker.test" not in names)
    check("names are resolved once and cached, not per request",
          srv._local_names() is names)


def test_a_refused_post_does_not_break_the_connection(p):
    """A POST refused before its body is read desynchronises keep-alive.

    HTTP/1.1 reuses the connection. A POST rejected for a missing CSRF header,
    a wrong host, or no session leaves its body in the socket, and the next
    request is parsed starting from those bytes:

        Bad request syntax ('{"remote_access": true}GET /api/status HTTP/1.1')

    which surfaces as a 400 or, when the leftovers do not parse as a method at
    all, a bare 501 page with nothing in the log. Reported from the settings
    page as "I saved a password and ticking remote access gives a 501".
    """
    print("\na refused POST leaves the connection usable:")
    import http.client
    for label, headers in (
            ("no CSRF header", {"Content-Type": "application/json"}),
            ("foreign Host", {"Content-Type": "application/json",
                              "X-Pantheon-CSRF": "1", "Host": "evil.example"})):
        c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=20)
        try:
            c.request("POST", "/api/security",
                      json.dumps({"remote_access": True}).encode(), headers)
            first = c.getresponse()
            first.read()
            c.request("GET", "/api/setup/status",
                      headers={"Accept": "application/json"})
            second = c.getresponse()
            body = second.read()
            ok = second.status < 500 and b"Bad request syntax" not in body
            check(f"after {label}", ok, f"{second.status} {body[:70]}")
        except Exception as exc:
            check(f"after {label}", False, f"{type(exc).__name__}: {exc}")
        finally:
            c.close()


def test_setting_a_password_keeps_you_signed_in(p):
    """Turning auth on must not sign out the person turning it on.

    The request that sets a password arrives without a session, because there
    was no auth a moment ago. Without handing one back, the very next click
    401s — so "set a password, then tick remote access" failed on the second
    step every time.
    """
    print("\nsetting a password signs you in rather than out:")
    import http.client
    # Its own panel: this has to run against one with NO password yet, and the
    # remote-access test above deliberately leaves one set. Borrowing that
    # state made this fail with "current password is wrong", which looks like
    # the bug it is testing for and is not.
    fresh = Panel()
    c = http.client.HTTPConnection("127.0.0.1", fresh.port, timeout=30)
    try:
        c.request("POST", "/api/security",
                  json.dumps({"password": "another-long-password"}).encode(),
                  {"Content-Type": "application/json", "X-Pantheon-CSRF": "1"})
        r = c.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie") or ""
        check("a session cookie comes back", "athena_session=" in cookie, cookie[:50])
        check("it is HttpOnly and SameSite=Strict",
              "HttpOnly" in cookie and "SameSite=Strict" in cookie, cookie[:80])

        session = cookie.split(";")[0]
        c.request("POST", "/api/security",
                  json.dumps({"remote_access": True}).encode(),
                  {"Content-Type": "application/json", "X-Pantheon-CSRF": "1",
                   "Cookie": session})
        r2 = c.getresponse()
        body = json.loads(r2.read().decode() or "{}")
        check("remote access can then be enabled", r2.status == 200,
              f"{r2.status} {body.get('error', '')}")
        check("and it really did bind wider",
              body.get("state", {}).get("bind") == "0.0.0.0",
              str(body.get("state")))
    finally:
        c.close()
        fresh.close()


def test_reverse_proxy_and_tls(p):
    """Behind a proxy, and serving TLS directly.

    The Host allowlist is what stops a page on the internet pointing its own
    domain at this machine. A reverse proxy forwards its OWN hostname, so that
    name has to be allowed explicitly — the allowlist cannot infer it, and
    inferring it would defeat the check.
    """
    print("\na reverse proxy's hostname has to be allowed explicitly:")
    hdr = {"X-Pantheon-CSRF": "1"}
    # Its own panel: this has to run against one with no password, and the
    # remote-access test deliberately leaves one set. Borrowing that state made
    # every call here 401 — which looks like the bug under test and is not.
    p = Panel()
    code, _, _ = request(p.port, "/", host="pantheon.example.com")
    check("an unknown proxy domain is refused", code == 421, str(code))

    code, text, _ = request(p.port, "/api/security", "POST",
                            {"extra_hosts": "pantheon.example.com"}, hdr)
    check("it can be allowed", code == 200, text[:70])

    code, _, _ = request(p.port, "/", host="pantheon.example.com")
    check("and is then accepted", code != 421, str(code))

    # The allowlist used to be consulted only when remote access was on, so a
    # proxy on this same machine got 421 no matter what was configured.
    state = json.loads(request(p.port, "/api/security")[1])
    check("it applies regardless of the remote-access setting",
          not state["remote_access"] and code != 421,
          f"remote_access={state['remote_access']} code={code}")

    code, _, _ = request(p.port, "/", host="evil.example")
    check("an attacker's domain is still refused", code == 421, str(code))

    print("\na pasted URL is refused rather than silently stored:")
    for bad in ("https://pantheon.example.com", "pantheon.example.com:8086",
                "pantheon.example.com/panel"):
        code, text, _ = request(p.port, "/api/security", "POST",
                                {"extra_hosts": bad}, hdr)
        check(f"{bad!r} refused", code == 400, str(code))

    print("\na certificate is validated before it is saved:")
    code, text, _ = request(p.port, "/api/security", "POST",
                            {"tls_cert": "/nope/cert.pem",
                             "tls_key": "/nope/key.pem"}, hdr)
    body = json.loads(text)
    check("a missing certificate is refused", code == 400, str(code))
    check("and says why", "will not load" in body.get("error", ""),
          body.get("error", "")[:60])
    state = json.loads(request(p.port, "/api/security")[1])
    check("nothing was stored", not state["tls"]["configured"], str(state["tls"]))

    print("\nthe session cookie is Secure when the browser used HTTPS:")
    code, _, headers = request(p.port, "/api/security", "POST",
                               {"password": "a-long-enough-password"},
                               {**hdr, "X-Forwarded-Proto": "https"})
    cookie = headers.get("Set-Cookie", "")
    check("Secure is set behind a TLS proxy", "Secure" in cookie, cookie[:70])
    # And must NOT be, over plain HTTP — the browser would refuse to send it
    # back, which looks like a login that silently never works.
    # And must NOT be set over plain HTTP: a Secure cookie is never sent back
    # over a plaintext connection, so marking it unconditionally would produce
    # a login that appears to succeed and then silently never holds.
    plain = Panel()
    try:
        _, _, h2 = request(plain.port, "/api/security", "POST",
                           {"password": "a-long-enough-password"}, hdr)
        cookie2 = h2.get("Set-Cookie", "")
        check("a cookie is issued over plain HTTP too", "athena_session=" in cookie2,
              cookie2[:50])
        check("but without Secure", "Secure" not in cookie2, cookie2[:70])
    finally:
        plain.close()
    p.close()


def test_prefs_file_permissions(p):
    print("\nthe file holding the password hash is not world-readable:")
    prefs = Path(p.dir) / ".athena-gui.json"
    check("it exists after setting a password", prefs.exists())
    if prefs.exists():
        check("mode is 0600", prefs.stat().st_mode & 0o777 == 0o600,
              oct(prefs.stat().st_mode & 0o777))
        body = prefs.read_text("utf-8")
        check("the password itself is not in it", "a-long-enough-password" not in body)


def main():
    p = Panel()
    try:
        test_every_page_renders(p)
        test_secrets_never_leave_the_process(p)
        test_dns_rebinding(p)
        test_csrf(p)
        test_headers(p)
        test_traversal(p)
        test_saving(p)
        test_language_install_is_a_fixed_menu(p)
        test_preview_needs_the_speech_service(p)
        # Sets a password, after which everything needs a session — so it and
        # the prefs check it produces run last, on purpose.
        test_the_machines_own_names_are_accepted(p)
        test_a_refused_post_does_not_break_the_connection(p)
        test_remote_access_needs_a_password(p)
        test_setting_a_password_keeps_you_signed_in(p)
        test_reverse_proxy_and_tls(p)
        test_prefs_file_permissions(p)
    finally:
        p.close()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
