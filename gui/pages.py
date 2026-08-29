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

NAV = (("/", "Status"), ("/settings", "Settings"),
       ("/logs", "Logs"), ("/security", "Security"), ("/setup", "Setup"))


# The project is Pantheon; the bot it runs is called whatever its operator
# named it. The panel used to say "Athena" in both places, which is wrong twice
# over on anyone else's install: it is not the project's name, and it is not
# necessarily their bot's either.
PROJECT = "Pantheon"


def _shell(title: str, here: str, body: str, script: str = "") -> str:
    nav = "".join(
        f'<a href="{href}" class="{"on" if href == here else ""}">{html.escape(label)}</a>'
        for href, label in NAV)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — {html.escape(PROJECT)}</title>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<header><h1><span class="mark">◆</span> {html.escape(PROJECT)}</h1><nav>{nav}</nav></header>
<main>{body}</main>
<div id="toast" class="toast" hidden></div>
<script src="/static/app.js"></script>
{f'<script src="/static/{script}"></script>' if script else ''}
</body></html>"""


def status_page() -> str:
    return _shell("Status", "/", """
<section class="services" id="services">
  <p class="loading">Checking…</p>
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
  <h3>Remote access</h3>
  <p class="sub">Off: reachable only from this machine. On: reachable from your
     network, which means this page — and the token fields on it — are exposed
     to everything that can route to this machine. Only on a network you
     trust.</p>
  <label class="inline"><input type="checkbox" id="remote"> Allow access from
     other computers</label>
  <p class="help" id="remote-help"></p>
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
<section class="panel narrow">
  <h2>Sign in</h2>
  <form id="login-form" autocomplete="off">
    <div class="field">
      <label for="password">Password</label>
      <div class="reveal"><input type="password" id="password" autocomplete="current-password" autofocus>
        <button type="button" class="eye" data-for="password" aria-label="Show">show</button></div>
    </div>
    <p class="error" id="login-error" hidden></p>
    <button type="submit" class="primary">Sign in</button>
  </form>
</section>
""", "login.js")


def error_page(code: int, message: str) -> str:
    return _shell(str(code), "", f"""
<section class="panel narrow">
  <h2>{code}</h2>
  <p>{html.escape(message)}</p>
  <p><a href="/">Back to the status page</a></p>
</section>""")
