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
    return {"key": "env", "title": "Configuration file",
            "why": "Where your settings and tokens live.",
            "state": "ok" if not blank else "todo",
            "detail": (f".env present, mode {mode}" if not blank
                       else f"still blank: {', '.join(blank)}"),
            "fix": None,
            "manual": "" if not blank else "Fill these in on the Settings page.",
            "settings": blank}


def _check_ollama() -> dict:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    installed = bool(shutil.which("ollama"))
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as r:
            models = [m["name"] for m in json.loads(r.read().decode())["models"]]
    except Exception:
        models = None

    if models is None:
        return {"key": "ollama", "title": "Language model",
                "why": "Handles fuzzy requests and conversation. Without it the "
                       "bot still does anything unambiguous.",
                "state": "todo",
                "detail": ("installed but not running" if installed
                           else "not installed"),
                "fix": None,
                "manual": ("ollama serve" if installed
                           else "brew install ollama && ollama serve")}
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


CHECKS = (_check_tools, _check_bot_venv, _check_env, _check_ollama,
          _check_permissions, _check_speech, _check_voice)


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
    return _pythons().get("3.13") or sys.executable


ACTIONS = {
    "brew_tools": {
        "title": "Install mpv and yt-dlp",
        "steps": lambda: [["brew", "install", "mpv", "yt-dlp"]],
        "note": "A few minutes. ffmpeg comes along with mpv.",
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
    if not (shutil.which(first) or Path(first).exists()):
        return {"ok": False,
                "message": f"Cannot run this yet — {first} is not installed."}

    job_id = uuid.uuid4().hex
    with _JOB_LOCK:
        _JOBS[job_id] = {"action": action, "title": spec["title"], "lines": [],
                         "done": False, "rc": None, "started": time.time()}
    threading.Thread(target=_run_job, args=(job_id, steps), daemon=True).start()
    return {"ok": True, "job": job_id, "title": spec["title"]}


def _run_job(job_id: str, steps: list) -> None:
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
                                    bufsize=1, cwd=str(ROOT))
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
