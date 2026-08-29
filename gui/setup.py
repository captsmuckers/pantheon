"""First-run setup: what is missing, and what can be done about it.

The control panel exists partly for this. It runs on the Python macOS already
ships, with no virtualenv and no dependencies, which means it can be the FIRST
thing someone opens — before anything is installed — and walk them the rest of
the way. A setup guide that needs the environment it is setting up would be
useless exactly when it is wanted.

Two halves, kept apart on purpose:

  PROBE   what is true right now. Read-only, cheap enough to run on every page
          load, and it never asks for permission or installs anything. Every
          check reports not just pass/fail but what to do about it, because
          "Ollama: missing" without the next step is just an insult.

  ACTIONS what the panel can fix by itself. A FIXED table, like the language
          installer: this runs commands, so the set of commands it can possibly
          run is written here and nothing about it comes from a request.

Plenty cannot be automated and is honest about it. Homebrew casks that install
system audio drivers need a password. macOS permission grants are a dialog only
a human can answer. Credentials have to be fetched from three different web
sites. For those the wizard gives the exact command or the exact click path and
a button to re-check — which is more useful than a spinner that cannot finish.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The four settings config.py refuses to start without.
REQUIRED = ("DISCORD_TOKEN", "ALLOWED_CHANNEL_ID", "PLEX_URL", "PLEX_TOKEN")

# Where each required credential comes from, in the order someone would go and
# get them. The wizard can install software; it cannot log in as you.
CREDENTIAL_HELP = {
    "DISCORD_TOKEN": {
        "name": "DISCORD_TOKEN",
        "where": "Discord Developer Portal → your app → Bot → Reset Token",
        "url": "https://discord.com/developers/applications",
        "note": "While you are there, turn on MESSAGE CONTENT INTENT under "
                "Privileged Gateway Intents. Without it the bot connects and "
                "then ignores every message, with no error at all.",
    },
    "ALLOWED_CHANNEL_ID": {
        "name": "ALLOWED_CHANNEL_ID",
        "where": "Discord → Settings → Advanced → Developer Mode, then "
                 "right-click the channel → Copy Channel ID",
        "url": "",
        "note": "The one text channel it listens in.",
    },
    "PLEX_URL": {
        "name": "PLEX_URL",
        "where": "Your Plex server's address, e.g. http://192.168.1.10:32400",
        "url": "",
        "note": "This machine must reach it directly — mpv plays the original "
                "file and there is no transcode fallback.",
    },
    "PLEX_TOKEN": {
        "name": "PLEX_TOKEN",
        "where": "Plex Web → any item → ⋯ → Get Info → View XML, then take "
                 "X-Plex-Token from the address bar",
        "url": "https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/",
        "note": "",
    },
}

BLACKHOLE = {
    "2ch": "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver",
    "16ch": "/Library/Audio/Plug-Ins/HAL/BlackHole16ch.driver",
}


def _run(cmd, timeout=30, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except (FileNotFoundError, OSError) as exc:
        return 127, str(exc)


def _venv_python(*parts) -> Path:
    return ROOT.joinpath(*parts, "bin", "python")


def _imports(python: Path, modules: list) -> tuple:
    """(ok, missing) — can this interpreter import all of these?"""
    if not python.exists():
        return False, modules
    probe = ("import importlib.util,json,sys;"
             f"m={modules!r};"
             "print(json.dumps([x for x in m "
             "if importlib.util.find_spec(x.split('.')[0]) is None]))")
    rc, out = _run([str(python), "-c", probe], timeout=120)
    if rc != 0:
        return False, modules
    try:
        missing = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, modules
    return not missing, missing


def _env_values() -> dict:
    from gui import envfile
    return envfile.read(ROOT / ".env")


# ----------------------------------------------------------------------
# the checks
# ----------------------------------------------------------------------
#
# Each returns a dict the page renders directly. `state` is one of:
#   ok        nothing to do
#   todo      needed, and not done
#   optional  a feature that is simply off
# `fix` names an entry in ACTIONS when the panel can do it itself; `manual`
# carries the command or click-path when it cannot.

def _check_python() -> dict:
    """A Python new enough to build the bot's virtualenv with.

    macOS ships 3.9 and the project is built and tested on 3.13. The control
    panel itself deliberately runs on 3.9 so it can be open before any of this
    is true — which is exactly why this has to be checked rather than assumed.
    """
    found = _pythons()
    have = _bot_python()
    return {"key": "python", "title": "Python 3.13",
            "why": "What the bot runs on. macOS ships 3.9, which is enough for "
                   "this control panel and not for the bot.",
            "state": "ok" if have else "todo",
            "detail": (f"{have}" if have else
                       f"only {sys.version.split()[0]} found"),
            "fix": "brew_python" if not have and shutil.which("brew") else None,
            "manual": "brew install python@3.13" if not have else "",
            "found": found}


def _check_brew() -> dict:
    """Homebrew, which most of the other fixes are.

    Not a dependency of the bot — a dependency of being able to install the
    dependencies. Reported separately because "brew: command not found" three
    steps later is a worse experience than being told up front.
    """
    path = shutil.which("brew")
    return {"key": "brew", "title": "Homebrew",
            "why": "How mpv, Python and Ollama get installed. The panel cannot "
                   "install Homebrew itself — that script wants your password.",
            "state": "ok" if path else "todo",
            "detail": path or "not installed",
            "fix": None,
            "manual": "" if path else
                      '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com'
                      '/Homebrew/install/HEAD/install.sh)"'}


def _check_tools() -> dict:
    missing = [t for t in ("mpv", "yt-dlp") if not shutil.which(t)]
    found = {t: shutil.which(t) for t in ("mpv", "yt-dlp")}
    return {
        "key": "tools", "title": "Media tools",
        "why": "mpv plays everything; yt-dlp resolves YouTube and live streams.",
        "state": "ok" if not missing else "todo",
        "detail": (", ".join(f"{k} at {v}" for k, v in found.items() if v)
                   or "neither found"),
        "fix": "brew_tools" if missing and shutil.which("brew") else None,
        "manual": ("brew install mpv yt-dlp" if missing else ""),
    }


def _check_bot_venv() -> dict:
    python = _venv_python(".venv")
    # python-mpv-jsonipc imports as python_mpv_jsonipc, not "mpv" — guessing
    # the module name from the package name reported a working venv as broken.
    ok, missing = _imports(python, ["discord", "plexapi", "httpx", "dotenv",
                                    "python_mpv_jsonipc"])
    return {
        "key": "venv", "title": "Bot environment",
        "why": "The virtualenv the bot itself runs from.",
        "state": "ok" if ok else "todo",
        "detail": (f"{python} — ready" if ok else
                   (f"missing: {', '.join(missing)}" if python.exists()
                    else "not created yet")),
        "fix": "make_venv",
        "manual": "python3.13 -m venv .venv && "
                  ".venv/bin/python -m pip install -r requirements.txt",
    }


def _check_env() -> dict:
    path = ROOT / ".env"
    if not path.exists():
        return {"key": "env", "title": "Configuration file",
                "why": "Where your settings and tokens live.",
                "state": "todo", "detail": "no .env yet",
                "fix": "make_env", "manual": "cp .env.example .env"}
    values = _env_values()
    blank = [k for k in REQUIRED if not values.get(k)]
    mode = oct(path.stat().st_mode & 0o777)
    return {"key": "env", "title": "Credentials",
            "why": "The four things the bot cannot start without. Each has to "
                   "be fetched from somewhere; none of it can be automated.",
            "state": "ok" if not blank else "todo",
            "detail": (f".env present, mode {mode}" if not blank
                       else f"{len(blank)} still needed"),
            "fix": None,
            "manual": "",
            "settings": blank,
            # Where each one actually comes from. Naming the missing key and
            # stopping there is the difference between a checklist and a help
            # page, and this is the step people get stuck on.
            "credentials": [CREDENTIAL_HELP[k] for k in blank]}


def _check_ollama() -> dict:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    installed = bool(shutil.which("ollama"))
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as r:
            models = [m["name"] for m in json.loads(r.read().decode())["models"]]
    except Exception:
        models = None

    if models is None:
        # `brew services start` rather than `ollama serve`: serve runs in the
        # foreground and would die with the job that started it, leaving a
        # step that reports success and stops working the moment you look away.
        return {"key": "ollama", "title": "Language model",
                "why": "Handles fuzzy requests and conversation. Without it the "
                       "bot still does anything unambiguous.",
                "state": "todo",
                "detail": ("installed but not running" if installed
                           else "not installed"),
                "fix": "start_ollama" if shutil.which("brew") else None,
                "manual": ("brew services start ollama" if installed
                           else "brew install ollama && brew services start ollama")}
    if not models:
        return {"key": "ollama", "title": "Language model",
                "why": "Running, but no model has been pulled.",
                "state": "todo", "detail": "no models",
                "fix": "pull_model", "manual": "ollama pull qwen3:8b"}
    return {"key": "ollama", "title": "Language model",
            "why": "Handles fuzzy requests and conversation.",
            "state": "ok", "detail": f"{len(models)} available: {', '.join(sorted(models)[:4])}",
            "fix": None, "manual": "", "models": sorted(models)}


def _check_permissions() -> dict:
    """Can the bot actually drive windows?

    Deliberately probes rather than assuming. Both grants fail SILENTLY — macOS
    returns an error indistinguishable from "no such window" — which is why the
    commonest support question is a window that is plainly on screen.
    """
    rc, out = _run(["osascript", "-e",
                    'tell application "System Events" to count processes'],
                   timeout=15)
    granted = rc == 0
    return {"key": "permissions", "title": "Screen control",
            "why": "The bot moves mpv and Spotify windows. Without this every "
                   "window call fails and it reports windows it cannot see.",
            "state": "ok" if granted else "todo",
            "detail": ("Accessibility granted" if granted
                       else f"refused — {out.splitlines()[-1][:120] if out else 'no detail'}"),
            "fix": None,
            "manual": "System Settings → Privacy & Security → Accessibility, "
                      "and add whatever runs the bot. Automation is a separate "
                      "grant, requested the first time Spotify is controlled."}


def _check_speech() -> dict:
    python = _venv_python("tts", ".venv")
    ok, missing = _imports(python, ["torch", "kokoro", "soundfile"])
    return {"key": "speech", "title": "Speech (optional)",
            "why": "Lets her talk back. Its own virtualenv, because Kokoro "
                   "pins Python <3.13 and the bot runs 3.13.",
            "state": "ok" if ok else "optional",
            "detail": (f"ready — {python}" if ok else
                       (f"missing: {', '.join(missing)}" if python.exists()
                        else "not created")),
            "fix": "make_tts_venv",
            "manual": "brew install python@3.12 && "
                      "/opt/homebrew/bin/python3.12 -m venv tts/.venv && "
                      "tts/.venv/bin/python -m pip install -r tts/requirements.txt"}


def _check_voice() -> dict:
    python = _venv_python(".venv")
    ok, missing = _imports(python, ["numpy", "sounddevice", "faster_whisper"])
    drivers = {name: Path(p).exists() for name, p in BLACKHOLE.items()}
    absent = [n for n, present in drivers.items() if not present]
    good = ok and not absent
    parts = []
    if not ok:
        parts.append("packages missing: " + (", ".join(missing) or "?"))
    if absent:
        parts.append("no BlackHole " + ", ".join(absent))
    return {"key": "voice", "title": "Voice input (optional)",
            "why": "Spoken commands. Needs two virtual audio devices — two, "
                   "because one shared device would feed her voice back into "
                   "her own ears.",
            "state": "ok" if good else "optional",
            "detail": "ready" if good else "; ".join(parts),
            "fix": "install_voice_deps" if absent == [] and not ok else None,
            "manual": ("brew install --cask blackhole-2ch blackhole-16ch  "
                       "(needs your password, and restart Discord after)"
                       if absent else
                       ".venv/bin/python -m pip install -r requirements-dev.txt")}


def _check_firefox() -> dict:
    """Firefox, for YouTube links that hit the age gate.

    yt-dlp cannot get past an age gate reliably — an authenticated client gets
    zero usable formats — so the fallback does not try. It opens the ordinary
    youtube.com page in a signed-in Firefox and lets YouTube's own player
    handle it, the way it would for a person.

    Not offered as a button: the cask can ask for a password, and a background
    job waiting on a password prompt nobody can see is worse than a command to
    paste. The profile also has to be created and signed into by hand.
    """
    candidates = ["/Applications/Firefox.app/Contents/MacOS/firefox",
                  os.path.expanduser("~/Applications/Firefox.app/Contents/MacOS/firefox")]
    found = shutil.which("firefox") or next(
        (c for c in candidates if Path(c).exists()), None)
    if not found:
        return {"key": "firefox", "title": "Age-restricted YouTube (optional)",
                "why": "Age-gated YouTube links open in a signed-in Firefox, "
                       "because yt-dlp cannot get past the gate. Everything "
                       "else on YouTube works without this.",
                "state": "optional",
                "detail": "Firefox not installed — age-gated links will report "
                          "that they cannot be played",
                "fix": None,
                "manual": "brew install --cask firefox"}

    # Installed is not the same as ready. The profile has to exist, be
    # configured (a fresh one blocks autoplay and shows onboarding over the
    # video), and be signed in to YouTube — which is the only part nobody can
    # automate, and the part most likely to be forgotten.
    import configparser
    prof_dir, prefs, signed_in = None, False, False
    ini = Path.home() / "Library" / "Application Support" / "Firefox" / "profiles.ini"
    if ini.exists():
        try:
            cp = configparser.ConfigParser()
            cp.read(ini)
            wanted = os.environ.get("BROWSER_PROFILE") or \
                _env_values().get("BROWSER_PROFILE") or "Athena"
            for section in cp.sections():
                if cp[section].get("Name") == wanted:
                    path = cp[section].get("Path", "")
                    prof_dir = (Path(path) if os.path.isabs(path)
                                else ini.parent / path)
                    break
        except Exception:
            prof_dir = None
    if prof_dir and prof_dir.exists():
        prefs = (prof_dir / "user.js").exists()
        # A signed-in profile has cookies for google/youtube. Checked by size
        # rather than by reading them: the file existing proves nothing, and
        # reading somebody's cookie jar to render a checkmark would be rude.
        cookies = prof_dir / "cookies.sqlite"
        signed_in = cookies.exists() and cookies.stat().st_size > 200_000

    ready = bool(prof_dir and prefs and signed_in)
    if ready:
        detail = f"{found} — profile configured and signed in"
    elif prof_dir:
        parts = []
        if not prefs:
            parts.append("no user.js (autoplay will be blocked)")
        if not signed_in:
            parts.append("not signed in to YouTube")
        detail = f"profile exists but {', and '.join(parts)}"
    else:
        detail = f"{found} — no '{'Athena'}' profile yet"

    return {"key": "firefox", "title": "Age-restricted YouTube (optional)",
            "why": "Age-gated YouTube links open in a signed-in Firefox, "
                   "because yt-dlp cannot get past the gate. Everything else "
                   "on YouTube works without this.",
            "state": "ok" if ready else "optional",
            "detail": detail,
            "fix": "setup_firefox" if not ready else None,
            "manual": "" if ready else
                      "scripts/setup-firefox-profile.sh --open\n"
                      "# then sign in to YouTube in the window it opens"}


def _check_launchagents() -> dict:
    """Whether the bot starts at login and restarts if it crashes.

    Optional: plenty of people would rather start it by hand. Reported anyway,
    because "why did it not come back after a reboot" is otherwise a mystery.
    """
    agents = Path.home() / "Library" / "LaunchAgents"
    installed = []
    for label in ("com.athena.bot", "com.athena.tts"):
        plist = agents / f"{label}.plist"
        if not plist.exists():
            continue
        try:
            import plistlib
            with open(plist, "rb") as fh:
                data = plistlib.load(fh)
            program = " ".join(data.get("ProgramArguments") or [])
            if str(ROOT) in program:
                installed.append(label)
        except Exception:
            pass
    both = len(installed) == 2
    return {"key": "launchd", "title": "Start at login (optional)",
            "why": "Installs LaunchAgents so both services start when you log "
                   "in and restart if they crash.",
            "state": "ok" if both else "optional",
            "detail": ("both agents installed for this checkout" if both else
                       f"installed: {', '.join(installed) or 'neither'}"),
            "fix": "install_launchagents",
            "manual": "scripts/install-launchagents.sh"}


CHECKS = (_check_brew, _check_python, _check_tools, _check_bot_venv,
          _check_env, _check_ollama, _check_permissions, _check_speech,
          _check_voice, _check_firefox, _check_launchagents)


def probe() -> dict:
    steps = [fn() for fn in CHECKS]
    required = [s for s in steps if s["state"] != "optional"]
    return {"steps": steps,
            "ready": all(s["state"] == "ok" for s in required),
            "remaining": sum(1 for s in required if s["state"] != "ok"),
            "root": str(ROOT)}


# ----------------------------------------------------------------------
# the actions
# ----------------------------------------------------------------------
#
# A FIXED table, for the same reason the language installer is one: these run
# commands, so the set of commands that can possibly run is written here and
# nothing about it comes from a request. The key is looked up; the argv is
# never assembled from anything a caller sends.
#
# Not here, and deliberately: anything needing a password (the BlackHole casks
# install a system audio driver), and anything needing a human to answer a
# dialog (the macOS permission grants). The wizard gives the exact command or
# click path for those and offers a re-check, which is more honest than a
# button that cannot succeed.

def _pythons() -> dict:
    """Interpreters that could build a virtualenv, best first."""
    found = {}
    for label, names in (("3.13", ("python3.13",)), ("3.12", ("python3.12",))):
        for n in names:
            p = shutil.which(n) or f"/opt/homebrew/bin/{n}"
            if Path(p).exists():
                found[label] = p
                break
    return found


def _bot_python() -> str:
    """The interpreter to build the bot's virtualenv with, or "" if there is none.

    Returns empty rather than falling back to sys.executable. That fallback was
    the bug: on a Mac with nothing installed, sys.executable is the stock
    /usr/bin/python3 (3.9), so the wizard would cheerfully build a 3.9
    virtualenv, report success, and hand over an install that cannot run the
    bot. A missing prerequisite has to look missing.
    """
    found = _pythons()
    return found.get("3.13") or found.get("3.12") or ""


ACTIONS = {
    "brew_tools": {
        "title": "Install mpv and yt-dlp",
        "steps": lambda: [["brew", "install", "mpv", "yt-dlp"]],
        "note": "A few minutes. ffmpeg comes along with mpv.",
    },
    "brew_python": {
        "title": "Install Python 3.13",
        "steps": lambda: [["brew", "install", "python@3.13"]],
        "note": "A few minutes.",
    },
    "make_venv": {
        "title": "Create the bot environment",
        "steps": lambda: [
            [_bot_python(), "-m", "venv", str(ROOT / ".venv")],
            [str(_venv_python(".venv")), "-m", "pip", "install", "--upgrade", "pip"],
            [str(_venv_python(".venv")), "-m", "pip", "install",
             "-r", str(ROOT / "requirements.txt")],
        ],
        "note": "A few minutes, and about 300 MB.",
    },
    "make_env": {
        "title": "Create the configuration file",
        "steps": lambda: [],          # handled specially: a file copy, not a command
        "note": "Copies .env.example, then you fill it in on the Settings page.",
    },
    "start_ollama": {
        "title": "Install and start Ollama",
        "steps": lambda: ([["brew", "install", "ollama"]]
                          if not shutil.which("ollama") else []) +
                         [["brew", "services", "start", "ollama"]],
        "note": "Runs it as a background service so it survives a reboot. "
                "Pull a model afterwards — that step appears once it is up.",
    },
    "pull_model": {
        "title": "Download a language model",
        "steps": lambda: [["ollama", "pull", "qwen3:8b"]],
        "note": "About 5 GB. qwen3:8b is the default; others can be compared "
                "later with scripts/bakeoff.py.",
    },
    "make_tts_venv": {
        "title": "Set up speech",
        "steps": lambda: [
            [_pythons().get("3.12", "python3.12"), "-m", "venv", str(ROOT / "tts" / ".venv")],
            [str(_venv_python("tts", ".venv")), "-m", "pip", "install", "--upgrade", "pip"],
            [str(_venv_python("tts", ".venv")), "-m", "pip", "install",
             "-r", str(ROOT / "tts" / "requirements.txt")],
        ],
        "note": "Several minutes, and about 2 GB — it pulls PyTorch. Needs "
                "Python 3.12: Kokoro declares Requires-Python <3.13.",
    },
    "setup_firefox": {
        "title": "Prepare the Firefox profile",
        "steps": lambda: [["/bin/bash",
                           str(ROOT / "scripts" / "setup-firefox-profile.sh"),
                           "--open"]],
        "note": "Creates the profile and configures it, then opens it at "
                "YouTube. Sign in there — that part is yours, and it is the "
                "only step that cannot be automated.",
    },
    "install_launchagents": {
        "title": "Install the start-at-login agents",
        "steps": lambda: [["/bin/bash",
                           str(ROOT / "scripts" / "install-launchagents.sh")]],
        "note": "Both services restart while this runs, so give it a few "
                "seconds. Remove them later with --uninstall.",
    },
    "install_voice_deps": {
        "title": "Install voice input packages",
        "steps": lambda: [
            [str(_venv_python(".venv")), "-m", "pip", "install",
             "-r", str(ROOT / "requirements-dev.txt")],
        ],
        "note": "The audio devices are separate — they need a password and "
                "cannot be installed from here.",
    },
}

_JOBS: dict = {}
_JOB_LOCK = threading.Lock()
MAX_LINES = 400


def start_steps(title: str, steps: list, cwd=None) -> dict:
    """Run a prepared list of commands as a background job.

    Exposed so the updater can reuse this rather than growing a second job
    runner. `steps` is built by the caller from its own fixed table — nothing
    here makes it safe, and nothing here should be handed a command that came
    from a request.
    """
    job_id = uuid.uuid4().hex
    with _JOB_LOCK:
        _JOBS[job_id] = {"action": "steps", "title": title, "lines": [],
                         "done": False, "rc": None, "started": time.time()}
    threading.Thread(target=_run_job, args=(job_id, steps, cwd),
                     daemon=True).start()
    return {"ok": True, "job": job_id, "title": title}


def start(action: str) -> dict:
    """Begin a whitelisted action in the background. Returns a job id."""
    spec = ACTIONS.get(action)
    if spec is None:
        return {"ok": False, "message": f"No such setup action: {action!r}"}

    if action == "make_env":
        src, dst = ROOT / ".env.example", ROOT / ".env"
        if dst.exists():
            return {"ok": False, "message": ".env already exists; not overwriting it."}
        if not src.exists():
            return {"ok": False, "message": "No .env.example to copy."}
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        return {"ok": True, "done": True,
                "message": "Created .env (mode 0600). Fill it in on Settings."}

    steps = spec["steps"]()
    if not steps:
        return {"ok": False, "message": "Nothing to do."}

    # Only the FIRST step's executable is checked. Later steps deliberately use
    # interpreters that earlier steps create — make_venv builds .venv and then
    # installs into it — so checking them all upfront made the single most
    # important action permanently unrunnable on exactly the fresh machine it
    # exists for.
    first = steps[0][0]
    # The empty check is load-bearing: Path("") is PosixPath("."), which exists,
    # so an unresolved interpreter sailed through this guard and the wizard
    # started a job to run "" as a command.
    if not first:
        return {"ok": False,
                "message": "Cannot run this yet — no suitable Python was found. "
                           "Install one first (brew install python@3.13)."}
    if not (shutil.which(first) or Path(first).is_file()):
        return {"ok": False,
                "message": f"Cannot run this yet — {first} is not installed."}

    job_id = uuid.uuid4().hex
    with _JOB_LOCK:
        _JOBS[job_id] = {"action": action, "title": spec["title"], "lines": [],
                         "done": False, "rc": None, "started": time.time()}
    threading.Thread(target=_run_job, args=(job_id, steps), daemon=True).start()
    return {"ok": True, "job": job_id, "title": spec["title"]}


def _run_job(job_id: str, steps: list, cwd=None) -> None:
    """Run each step, streaming output into the job so the page can watch.

    Streamed rather than captured at the end because these take minutes — a pip
    install of PyTorch with no output for four minutes is indistinguishable
    from a hang, and the natural response to that is to press the button again.
    """
    rc = 0
    for step in steps:
        _emit(job_id, f"$ {' '.join(step)}")
        try:
            proc = subprocess.Popen(step, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1, cwd=str(cwd or ROOT))
            for line in proc.stdout:
                _emit(job_id, line.rstrip())
            rc = proc.wait()
        except Exception as exc:
            _emit(job_id, f"failed: {type(exc).__name__}: {exc}")
            rc = 1
        if rc != 0:
            _emit(job_id, f"— step failed with exit code {rc}")
            break
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["done"] = True
            job["rc"] = rc


def _emit(job_id: str, line: str) -> None:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job["lines"].append(line)
        # Bounded: `pip install` is thousands of lines and nobody reads the
        # middle of it. The tail is where the error is.
        if len(job["lines"]) > MAX_LINES:
            del job["lines"][:-MAX_LINES]


def job(job_id: str, since: int = 0) -> dict:
    """Progress for a running action. `since` is a line index, so the page
    asks for what is new rather than re-fetching the whole log."""
    with _JOB_LOCK:
        j = _JOBS.get(job_id)
        if j is None:
            return {"ok": False, "message": "No such job — the panel may have restarted."}
        lines = j["lines"][since:]
        return {"ok": True, "title": j["title"], "lines": lines,
                "next": since + len(lines), "done": j["done"], "rc": j["rc"],
                "elapsed": round(time.time() - j["started"])}
