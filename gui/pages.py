"""The HTML. Structure only — every value on these pages arrives from the API.

Rendering the settings server-side was the obvious first approach and is a bad
one here: it would mean the token values pass through a template on their way
to the browser, and the only reliable way to never leak a secret is for it to
never be in the document at all. So these pages ship empty and fill themselves
from /api/settings, which redacts.

No template engine, because there is no dependency to spend on one and the
structure is fixed. The only interpolation is the page title and the nav.
"""

from __future__ import annotations

import html

# The nav, and who sees each entry. A General User gets Status and Voice; the
# rest are administrator-only. This only draws the links — the server refuses
# the routes regardless, so a friend typing /security by hand still gets a 403
# rather than a page. Hiding them is courtesy, not the control.
NAV = (("/", "Status", "user"), ("/voice", "Voice lab", "user"),
       ("/library", "Voice library", "user"),
       ("/account", "Account", "user"),
       ("/settings", "Settings", "admin"), ("/logs", "Logs", "admin"),
       ("/security", "Security", "admin"), ("/setup", "Setup", "admin"))


# The project is Pantheon; the bot it runs is called whatever its operator
# named it. The panel used to say "Athena" in both places, which is wrong twice
# over on anyone else's install: it is not the project's name, and it is not
# necessarily their bot's either.
PROJECT = "Pantheon"


def _shell(title: str, here: str, body: str, script: str = "",
           role: str = "admin", chrome: bool = True) -> str:
    """chrome=False drops the nav and centres the body.

    For pages reached BEFORE signing in. A nav offering Settings and Security
    to somebody who cannot open either is noise at best, and at worst reads as
    a list of things that are broken.
    """
    nav = "" if not chrome else "".join(
        f'<a href="{href}" class="{"on" if href == here else ""}">{html.escape(label)}</a>'
        for href, label, need in NAV
        if role == "admin" or need == "user")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — {html.escape(PROJECT)}</title>
<link rel="stylesheet" href="/static/app.css">
</head><body data-role="{html.escape(role)}">
<header class="{'' if chrome else 'bare'}"><h1><span class="mark">◆</span> {html.escape(PROJECT)}</h1>{f'<nav>{nav}</nav>' if chrome else ''}{'<div class="who"><span id="whoami"></span><button type="button" id="signout" class="ghost">Sign out</button></div>' if chrome else ''}</header>
{'<div id="labbar" class="labbar" hidden></div>' if chrome else ''}
<main class="{'' if chrome else 'centred'}">{body}</main>
<div id="toast" class="toast" hidden></div>
<script src="/static/app.js"></script>
{f'<script src="/static/{script}"></script>' if script else ''}
</body></html>"""


def status_page(role: str = "admin") -> str:
    return _shell("Status", "/", """
<section class="services" id="services">
  <p class="loading">Checking…</p>
</section>


<section class="panel" id="update-panel">
  <h2>Updates</h2>
  <div id="update-body"><p class="loading">Checking…</p></div>
  <pre id="update-output" class="log short" hidden></pre>
</section>

<section class="panel">
  <h2>System</h2>
  <div id="sysmon" class="sysmon"><p class="loading">Reading…</p></div>
</section>

<section class="panel">
  <h2>What it needs</h2>
  <p class="sub">External things the bot depends on. Each of these has stopped
     it from starting at least once.</p>
  <div id="probes" class="probes"><p class="loading">Checking…</p></div>
</section>
""", "status.js")


def settings_page() -> str:
    return _shell("Settings", "/settings", """
<section class="panel" id="settings-head">
  <h2>Settings</h2>
  <p class="sub">Written to <code id="env-path">.env</code>. Nothing takes
     effect until the service that reads it restarts — the bot loads its
     configuration once, at startup.</p>
  <label class="inline"><input type="checkbox" id="show-advanced"> Show advanced
     settings</label>
  <input type="search" id="setting-filter" placeholder="Filter settings…"
         autocomplete="off" spellcheck="false">
</section>

<form id="settings-form" autocomplete="off">
  <div class="settings-body">
    <nav class="secnav" id="secnav"></nav>
    <div id="sections"><p class="loading">Loading…</p></div>
  </div>
  <div class="savebar" id="savebar" hidden>
    <span id="save-summary"></span>
    <button type="button" class="ghost" id="revert">Revert</button>
    <button type="submit" class="primary">Save</button>
  </div>
</form>
""", "settings.js")


def logs_page() -> str:
    return _shell("Logs", "/logs", """
<section class="panel logs-head">
  <h2>Logs</h2>
  <div class="row">
    <div class="tabs" id="log-tabs"></div>
    <label class="inline"><input type="checkbox" id="follow" checked> Follow</label>
  </div>
  <p class="sub" id="log-note"></p>
</section>
<pre id="log" class="log"></pre>
""", "logs.js")


def security_page() -> str:
    return _shell("Security", "/security", """
<section class="panel">
  <h2>Access</h2>
  <p class="sub">The panel listens on this machine only unless you say
     otherwise, and it will not listen anywhere else without a password.</p>
  <div id="security-state" class="state"><p class="loading">Loading…</p></div>
</section>

<section class="panel">
  <h3>Password</h3>
  <p class="sub">Optional while the panel is loopback-only: anything that can
     reach it can already read <code>.env</code>. Required before remote access
     can be turned on.</p>
  <form id="password-form" autocomplete="off">
    <div class="field" id="current-wrap" hidden>
      <label for="current">Current password</label>
      <div class="reveal"><input type="password" id="current" autocomplete="current-password">
        <button type="button" class="eye" data-for="current" aria-label="Show">show</button></div>
    </div>
    <div class="field">
      <label for="password">New password</label>
      <div class="reveal"><input type="password" id="password" autocomplete="new-password">
        <button type="button" class="eye" data-for="password" aria-label="Show">show</button></div>
      <p class="help">At least 8 characters. Leave blank and save to remove the
         password — which also turns remote access off.</p>
    </div>
    <button type="submit" class="primary">Save password</button>
  </form>
</section>

<section class="panel">
  <h3>HTTPS</h3>
  <p class="sub">Serve the panel over TLS directly, without a reverse proxy in
     front. Point at a certificate and key already on this machine — paths, not
     uploads: a private key posted through a web form crosses the network,
     possibly over plain HTTP since TLS is the thing being set up, and is then
     written by the web server itself.</p>
  <div id="tls-state" class="state"></div>
  <form id="tls-form" autocomplete="off">
    <div class="field">
      <label for="tls-cert">Certificate (fullchain.pem)</label>
      <input type="text" id="tls-cert" spellcheck="false"
             placeholder="/etc/letsencrypt/live/example.com/fullchain.pem">
    </div>
    <div class="field">
      <label for="tls-key">Private key (privkey.pem)</label>
      <input type="text" id="tls-key" spellcheck="false"
             placeholder="/etc/letsencrypt/live/example.com/privkey.pem">
      <p class="help">The panel must be able to read both. Leave both blank to
         turn TLS off. A certificate that will not load is refused here rather
         than saved — otherwise the panel would fall back to plain HTTP on
         every restart with the reason buried in a log.</p>
    </div>
    <button type="submit" class="primary">Save certificate</button>
  </form>
</section>

<section class="panel">
  <h3>Behind a reverse proxy</h3>
  <p class="sub">The panel refuses requests carrying a hostname it does not
     answer to — that is what stops a page on the internet pointing its own
     domain at this machine and driving the panel through your browser. A
     reverse proxy forwards its own hostname, so that name has to be allowed
     here or every request comes back <b>421</b>.</p>
  <form id="hosts-form" autocomplete="off">
    <div class="field">
      <label for="extra-hosts">Extra hostnames</label>
      <input type="text" id="extra-hosts" spellcheck="false"
             placeholder="pantheon.example.com">
      <p class="help">Hostnames only — no <code>https://</code>, no port, no
         path. Separate several with commas.</p>
    </div>
    <button type="submit" class="primary">Save hostnames</button>
  </form>
  <div id="known-hosts" class="note-sm"></div>
</section>

<section class="panel">
  <h3>Remote access</h3>
  <p class="sub">Off: reachable only from this machine. On: reachable from your
     network, which means this page — and the token fields on it — are exposed
     to everything that can route to this machine. Only on a network you
     trust.</p>
  <label class="inline"><input type="checkbox" id="remote"> Allow access from
     other computers</label>
  <p class="help" id="remote-help"></p>
</section>

<section class="panel">
  <h3>Restart the panel</h3>
  <p class="sub">Only needed after the code changes — new settings or new
     dropdown choices are read from disk once, at startup, so a running panel
     keeps serving the old ones and shows anything it does not recognise as
     "not a listed choice". Changing a setting never needs this; the Speech and
     Services pages restart the bot and the speech service themselves.</p>
  <p class="sub">This page goes away for a few seconds and comes back on its
     own. There is deliberately no Stop: nothing in a browser could start it
     again.</p>
  <button type="button" id="restart-panel" class="ghost">Restart panel</button>
  <span class="try-note" id="restart-note"></span>
</section>

<section class="panel">
  <h3>Accounts</h3>
  <p class="sub">Administrators see and change everything. General Users get
     the status page and the voice controls — they cannot reach settings,
     security, setup, updates, or the logs, and the restriction is enforced by
     the server, not by hiding buttons.</p>
  <div id="users-state" class="state"></div>
  <table class="users" id="users-table"></table>
  <form id="user-form" autocomplete="off">
    <div class="field">
      <label for="u-name">Name</label>
      <input type="text" id="u-name" spellcheck="false" placeholder="a friend">
    </div>
    <div class="field">
      <label for="u-pass">Password</label>
      <input type="password" id="u-pass" autocomplete="new-password"
             placeholder="leave blank to keep the current one">
    </div>
    <div class="field">
      <label for="u-role">Role</label>
      <select id="u-role">
        <option value="user">General User</option>
        <option value="admin">Administrator</option>
      </select>
    </div>
    <button type="submit" class="ghost">Save account</button>
    <span class="try-note" id="user-note"></span>
  </form>
</section>
""", "security.js")


def setup_page() -> str:
    return _shell("Setup", "/setup", """
<section class="panel">
  <h2>Setting up</h2>
  <p class="sub">What is done, what is left, and what this page can do for you.
     Nothing here is destructive; re-checking is free.</p>
  <div id="setup-summary" class="summary"><p class="loading">Checking…</p></div>
</section>

<div id="setup-steps"></div>

<section class="panel" id="job-panel" hidden>
  <h3 id="job-title">Working…</h3>
  <pre id="job-output" class="log short"></pre>
  <p class="sub" id="job-status"></p>
</section>
""", "setup.js")


def login_page() -> str:
    return _shell("Sign in", "", """
<section class="panel narrow signin">
  <h2>Sign in</h2>
  <form id="login-form" autocomplete="off">
    <div class="field">
      <label for="username">Name</label>
      <input type="text" id="username" autocomplete="username" autofocus
             placeholder="admin">
    </div>
    <div class="field">
      <label for="password">Password</label>
      <div class="reveal"><input type="password" id="password" autocomplete="current-password">
        <button type="button" class="eye" data-for="password" aria-label="Show">show</button></div>
    </div>
    <p class="error" id="login-error" hidden></p>
    <button type="submit" class="primary">Sign in</button>
  </form>
</section>
""", "login.js", chrome=False)


def error_page(code: int, message: str) -> str:
    return _shell(str(code), "", f"""
<section class="panel narrow">
  <h2>{code}</h2>
  <p>{html.escape(message)}</p>
  <p><a href="/">Back to the status page</a></p>
</section>""", chrome=False)


def forbidden_page() -> str:
    """Shown when a General User asks for an administrator-only page.

    A page rather than a bare 403 because this is reachable by typing a URL or
    following an old bookmark, and "you are signed in, this is simply not for
    you" is a different message from "you are signed out".
    """
    return _shell("Not for you", "/", """
<section class="panel">
  <h2>Administrators only</h2>
  <p class="sub">You are signed in, but this section needs an administrator
     account. The status page and the voice controls are yours.</p>
  <p><a href="/">Back to the status page</a></p>
</section>
""", "")


def library_page(role: str = "admin") -> str:
    """The stored clips, as a place rather than a panel at the bottom of a bench.

    The lab is where you try things; this is what you keep. They were one page
    and it read as one activity, so managing the library meant scrolling past
    a mode picker and an upload form that had nothing to do with it.
    """
    return _shell("Voice library", "/library", """
<section class="panel">
  <h2>Voice library</h2>
  <p class="sub">Every clip stored on this machine. These are what
     <code>/tts</code> and <code>/voices</code> offer in Discord, and what the
     lab clones from.</p>
  <p class="sub"><strong>The transcript is what decides clone quality.</strong>
     When it is present and accurate, the model conditions on the recording
     itself and keeps rasp, accent and grain. When it is blank, the clip is
     reduced to a 1024-number speaker average that keeps pitch and little else
     — with no error shown anywhere. Correct any that are wrong.</p>
  <div id="lab-library-body"><p class="loading">Reading…</p></div>
</section>
""", "voicelab.js", role)


def account_page(role: str = "admin") -> str:
    """Your own account. The only page a General User owns.

    Everything else about accounts lives under Security, which is
    administrators-only — so the password an admin typed in for somebody was
    the password they kept, known to whoever set it up.
    """
    return _shell("Account", "/account", """
<section class="panel">
  <h2>Your account</h2>
  <p class="sub">Signed in as <strong id="acct-who">…</strong>.</p>
</section>

<section class="panel">
  <h3>Change your password</h3>
  <div class="field">
    <label for="pw-current">Current password</label>
    <input type="password" id="pw-current" autocomplete="current-password">
  </div>
  <div class="field">
    <label for="pw-new">New password</label>
    <input type="password" id="pw-new" autocomplete="new-password">
    <p class="help">At least 8 characters.</p>
  </div>
  <div class="field">
    <label for="pw-again">New password again</label>
    <input type="password" id="pw-again" autocomplete="new-password">
  </div>
  <div class="try">
    <button type="button" id="pw-save" class="primary">Change password</button>
    <span class="try-note" id="pw-note"></span>
  </div>
</section>
""", "account.js", role)


def voice_page(role: str = "admin") -> str:
    """A bench for trying voices, deliberately NOT the settings form.

    The two were the same page and it was the wrong shape: you cannot
    experiment inside the live configuration without either saving things you
    are only trying, or bouncing between pages to hear them. So nothing here
    is bound to .env. You change what you like, press Test as often as you
    like, and only Apply writes anything.

    What CAN be tried without a restart is bounded by physics rather than
    choice: voice and description ride on each synthesize call, so they are
    instant, but the mode IS the checkpoint and trying a different one means
    loading different weights. That is stated on the page rather than hidden,
    because a 30 second pause with no explanation reads as a hang.
    """
    return _shell("Voice lab", "/voice", """
<section class="panel">
  <h2>Voice lab</h2>
  <p class="sub">Try voices here. Nothing on this page affects Athena until you
     press <strong>Apply to Athena</strong> at the bottom — until then she keeps
     using whatever is saved, and Test only plays in this browser.</p>
  <div id="lab-live" class="state">Checking what is loaded…</div>
</section>

<section class="panel" id="lab-controls">
  <p class="loading">Loading…</p>
</section>

<section class="panel">
  <h3>Try it</h3>
  <p class="sub">Plays in this browser only. Athena is not affected and nothing
     is saved.</p>
  <div class="field">
    <label for="lab-text">4. What the voice should say</label>
    <input type="text" id="lab-text"
           value="Playing The Emperor's New Groove. Do try to keep up.">
    <p class="help">Anything you want to hear it say — this is the output, not
       the recording. The line below is only an example; replace it or leave
       it. It is <em>not</em> the same as “what it says”, which is the
       transcript of the clip you uploaded.</p>
  </div>
  <div class="try">
    <button type="button" id="lab-test">▶ Test</button>
    <span class="try-note" id="lab-note"></span>
  </div>
  <audio id="lab-audio" controls hidden></audio>
</section>

""" + ('''
<section class="panel" id="lab-apply-panel">
  <h3>Apply to Athena</h3>
  <p class="sub" id="lab-apply-help">This writes what is above to the saved
     settings and restarts what needs restarting. She will speak in this voice
     in Discord from then on.</p>
  <button type="button" id="lab-apply" class="primary">Apply to Athena</button>
  <span class="try-note" id="lab-apply-note"></span>
</section>''' if role == "admin" else '''
<section class="panel">
  <h3>Changing Athena\u2019s own voice</h3>
  <p class="sub">Everything on this page is yours to use: load any kind of
     voice, clone recordings, test them, and save them to the library for
     <code>/tts</code> in Discord. Changing the voice <em>Athena herself</em>
     speaks with is reserved for administrators \u2014 ask one if you want a
     voice you have made to become hers.</p>
</section>''') + """
""", "voicelab.js", role)
