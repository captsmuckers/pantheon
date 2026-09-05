"""What is running, and starting or stopping it.

The whole difficulty here is launchd, and it is worth stating plainly because
getting it wrong produces a button that appears to work and does not.

Both services are LaunchAgents with KeepAlive set to true. That is what makes
the bot survive a crash, and it also means launchd will restart anything you
kill. `launchctl stop` sends a signal and KeepAlive immediately undoes it;
running scripts/stop-athena.sh has exactly the same problem. A Stop button
built on either would report success, and the bot would be back inside ten
seconds — ThrottleInterval, not a fix.

So when a service is supervised, control goes through launchd itself:

    start     launchctl bootstrap gui/$UID <plist>   (or kickstart if loaded)
    stop      launchctl bootout    gui/$UID/<label>
    restart   launchctl kickstart -k gui/$UID/<label>

and when it is not supervised — someone started it from a terminal, or never
installed the agents — control goes through the same scripts/ launchers a
person would run by hand. Those two paths are genuinely different and the
status has to say which one applies, because "Stop" meaning "stop until I say
otherwise" and "Stop" meaning "stop until launchd notices" are not the same
promise.

ONE UGLY CONSEQUENCE, recorded rather than hidden. launchd terminates a job
with SIGTERM. Python's default SIGTERM handling exits without unwinding, so
bot.py's `finally: await player.shutdown()` does not run and mpv is left
holding a fullscreen window — `LastExitStatus = 15` in launchctl's output is
this having already happened. scripts/stop-athena.sh uses SIGINT precisely to
avoid it. A supervised stop therefore sweeps for orphaned mpv afterwards, the
same way that script does, and the sweep matches the athena-mpv- socket name so
an mpv the user opened themselves is left alone.

TWO CHECKOUTS ON ONE MACHINE is assumed, not treated as an error: this fork
exists alongside the private one it came from, and `pgrep -f 'python.*bot.py'`
matches both. Every process this module reports is checked against the root of
the checkout it is running from, so the GUI in one never offers to stop the
other.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UID = os.getuid()
AGENTS = Path.home() / "Library" / "LaunchAgents"

# The health endpoint the TTS server serves, and which launchd-athena.sh waits
# on before starting the bot.
TTS_HEALTH = "http://127.0.0.1:8085/health"


@dataclass
class Service:
    key: str
    label: str                 # launchd label
    title: str                 # what a person calls it
    pattern: str               # pgrep -f, matching scripts/_common.sh
    start_script: str
    stop_script: str


def _service_title() -> str:
    from gui import envfile
    return envfile.bot_name(ROOT)


# The launchd labels stay com.athena.* whatever the bot is called: they are
# identifiers, and renaming one would orphan an installed agent rather than
# rename it. Only the title a person reads follows BOT_NAME.
SERVICES = {
    "bot": Service("bot", "com.athena.bot", _service_title(),
                   r"python.*bot\.py", "start-athena.sh", "stop-athena.sh"),
    "tts": Service("tts", "com.athena.tts", "Speech",
                   r"python.*tts_server\.py", "start-tts.sh", "stop-tts.sh"),
    # The second speech server, for /tts and the voice lab. Listed so it can
    # be started and stopped from the Services page: it is ~6GB resident and
    # only earns that while somebody is actually using saved voices.
    "voices": Service("voices", "com.athena.voices", "Saved voices",
                      r"python.*tts_server\.py.*--port 8087", "", ""),
}


# ----------------------------------------------------------------------
# looking
# ----------------------------------------------------------------------

def _run(cmd: list, timeout: float = 15) -> tuple:
    """(returncode, stdout+stderr). Never raises for an ordinary failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"


# Processes that match a service pattern by carrying it on their command line
# without being the service. caffeinate is the live one: every launcher wraps
# python in `caffeinate -dis`, so `pgrep -f 'python.*bot\.py'` returns the
# wrapper too and the bot appears to be running twice.
_WRAPPERS = {"caffeinate", "nohup", "bash", "sh", "zsh", "env", "sudo"}


def _cwds(pids: list) -> dict:
    """{pid: working directory}, in one call rather than one call per pid.

    Ownership is decided on the working directory and NOT on the command line,
    which was the first attempt and was wrong. A venv on macOS re-execs into
    the framework interpreter, so the running bot's argv[0] is
    /opt/homebrew/Cellar/python@3.13/.../Python.app/Contents/MacOS/Python and
    carries no trace of the checkout it started from. Matching on it made the
    GUI report its own bot as somebody else's.

    Every launcher does `cd "$ROOT"` first and launchd sets WorkingDirectory,
    so cwd identifies the checkout on both paths.
    """
    if not pids:
        return {}
    code, out = _run(["lsof", "-a", "-p", ",".join(map(str, pids)),
                      "-d", "cwd", "-Fpn"], timeout=10)
    found, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].strip().isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            found[pid] = line[1:].strip()
    return found


def _comms(pids: list) -> dict:
    """{pid: executable name}, used only to drop wrapper processes."""
    if not pids:
        return {}
    _, out = _run(["ps", "-o", "pid=,comm=", "-p", ",".join(map(str, pids))], timeout=5)
    found = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            found[int(parts[0])] = os.path.basename(parts[1])
    return found


def _split_pids(pattern: str) -> tuple:
    """(ours, foreign) PIDs matching `pattern`.

    The foreign half is reported rather than discarded, and that is the whole
    reason this returns a pair. With two checkouts on one machine the honest
    status is not "stopped" — a bot is audibly running — it is "not this one".
    A page showing a bare Stopped while the room can hear Athena talking is
    worse than useless: the obvious response is to press Start, and end up with
    two bots answering the same channel.
    """
    code, out = _run(["pgrep", "-f", pattern], timeout=5)
    if code != 0 or not out:
        return [], []
    pids = [int(p) for p in out.split() if p.isdigit()]
    cwds, comms = _cwds(pids), _comms(pids)

    ours, foreign = [], []
    for pid in pids:
        if comms.get(pid, "") in _WRAPPERS:
            continue
        cwd = cwds.get(pid, "")
        # The TTS launcher runs from $ROOT/tts, so a subdirectory of the
        # checkout counts as the checkout.
        if cwd and (cwd == str(ROOT) or cwd.startswith(str(ROOT) + os.sep)):
            ours.append(pid)
        else:
            foreign.append({"pid": pid, "where": cwd or "an unreadable location"})
    return ours, foreign


def _pids(pattern: str) -> list:
    """Just the PIDs belonging to this checkout."""
    return _split_pids(pattern)[0]


def _launchd(label: str) -> dict:
    """What launchd knows about `label`, and whether it is even ours.

    A plist pointing at a different checkout is reported as foreign rather than
    controlled: the private repo's agents and this fork's would otherwise be
    indistinguishable by label alone, and stopping the wrong one from a page
    titled with the right one is precisely the sort of thing that destroys
    trust in a control panel.
    """
    info = {"label": label, "installed": False, "loaded": False, "ours": False,
            "pid": None, "last_exit": None, "plist": None}
    plist = AGENTS / f"{label}.plist"
    if plist.exists():
        info["installed"] = True
        info["plist"] = str(plist)
        try:
            with open(plist, "rb") as fh:
                data = plistlib.load(fh)
            program = " ".join(data.get("ProgramArguments") or [data.get("Program", "")])
            info["ours"] = str(ROOT) in program
        except Exception:
            info["ours"] = False

    code, out = _run(["launchctl", "list", label], timeout=5)
    if code == 0:
        info["loaded"] = True
        pid = re.search(r'"PID"\s*=\s*(\d+)', out)
        exit_ = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
        info["pid"] = int(pid.group(1)) if pid else None
        info["last_exit"] = int(exit_.group(1)) if exit_ else None
    return info


def _uptime(pid: int) -> str:
    """How long a pid has been up, phrased for a person."""
    _, out = _run(["ps", "-o", "etime=", "-p", str(pid)], timeout=5)
    return out.strip() or "?"


def tts_health(timeout: float = 2.0) -> dict:
    """Ask the speech server whether its model is actually loaded.

    Running and ready are different states and the difference is visible to the
    user: a reply spoken while Kokoro is still loading is dropped silently.
    """
    try:
        with urllib.request.urlopen(TTS_HEALTH, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {"error": type(exc).__name__}


def status() -> dict:
    """Everything the status page needs, in one call."""
    out = {}
    for key, svc in SERVICES.items():
        pids, foreign = _split_pids(svc.pattern)
        ld = _launchd(svc.label)
        supervised = ld["loaded"] and ld["ours"]
        out[key] = {
            # Re-resolved per call, so a rename shows up without a restart.
            "title": _service_title() if key == "bot" else svc.title,
            "running": bool(pids),
            "pids": pids,
            "foreign": foreign,
            "uptime": _uptime(pids[0]) if pids else "",
            "supervised": supervised,
            "launchd": ld,
            # A supervised service that is not running is either mid-restart or
            # crash-looping, and the last exit code is the only clue on the page.
            # Meaningless when the agent belongs to another checkout, so it is
            # only carried through when this GUI is the one in charge.
            "last_exit": ld["last_exit"] if ld["ours"] else None,
        }
    out["tts"]["health"] = tts_health()
    mpv_ours, mpv_foreign = _split_pids(r"mpv.*(athena|nyx)-mpv-")
    out["mpv"] = {"pids": mpv_ours, "foreign": mpv_foreign}
    return out


def probes() -> list:
    """External things the bot needs, checked cheaply.

    Not service state — these are the "why won't it start" answers, and every
    one of them has actually been the cause once. mpv in particular is the
    reason the bot crash-looped every eleven seconds under launchd.
    """
    found = []
    for name, why in (("mpv", "plays video; the bot exits at startup without it"),
                      ("yt-dlp", "resolves YouTube and live streams")):
        path = shutil.which(name)
        found.append({"name": name, "ok": bool(path),
                      "detail": path or "not on PATH", "why": why})

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=2) as r:
            models = [m["name"] for m in json.loads(r.read().decode())["models"]]
        found.append({"name": "ollama", "ok": True,
                      "detail": f"{len(models)} models", "models": sorted(models),
                      "why": "runs the model that answers and routes"})
    except Exception as exc:
        found.append({"name": "ollama", "ok": False,
                      "detail": f"unreachable at {host} ({type(exc).__name__})",
                      "why": "runs the model that answers and routes"})
    return found


# ----------------------------------------------------------------------
# touching
# ----------------------------------------------------------------------

def _script(name: str, *args: str) -> tuple:
    path = ROOT / "scripts" / name
    if not path.exists():
        return 127, f"missing {path}"
    return _run(["/bin/bash", str(path), *args], timeout=120)


def _sweep_mpv() -> str:
    """Kill mpv instances this checkout's bot left behind.

    Only ours: the pattern matches the IPC socket name the bot sets, so an mpv
    the user opened themselves is untouched. Same reasoning, and the same
    pattern, as scripts/stop-athena.sh.
    """
    pids = _pids(r"mpv.*(athena|nyx)-mpv-")
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    return f"swept {len(pids)} orphaned mpv" if pids else ""


def act(key: str, action: str) -> dict:
    """start | stop | restart a service. Returns what happened, in words.

    The message matters as much as the result: half of these operations behave
    differently depending on whether launchd is supervising, and a person
    watching a spinner deserves to know which path ran.
    """
    svc = SERVICES.get(key)
    if svc is None:
        return {"ok": False, "message": f"unknown service {key!r}"}
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "message": f"unknown action {action!r}"}

    ld = _launchd(svc.label)
    supervised = ld["loaded"] and ld["ours"]
    target = f"gui/{UID}/{svc.label}"
    notes = []

    if supervised:
        if action == "restart":
            code, out = _run(["launchctl", "kickstart", "-k", target], timeout=60)
            verb = "restarted via launchd"
        elif action == "stop":
            code, out = _run(["launchctl", "bootout", target], timeout=60)
            verb = "stopped and unloaded from launchd"
            if key == "bot":
                # launchd's SIGTERM skips bot.py's cleanup, so mpv survives.
                swept = _sweep_mpv()
                if swept:
                    notes.append(swept)
                notes.append("It will not come back until you press Start "
                             "or log in again.")
        else:
            code, out = _run(["launchctl", "kickstart", target], timeout=60)
            verb = "started via launchd"
    elif ld["installed"] and ld["ours"] and action in ("start", "restart"):
        code, out = _run(["launchctl", "bootstrap", f"gui/{UID}", ld["plist"]], timeout=60)
        verb = "loaded into launchd"
    else:
        script = svc.stop_script if action == "stop" else svc.start_script
        if action == "restart":
            _script(svc.stop_script)
            time.sleep(1.0)
        code, out = _script(script)
        verb = f"ran scripts/{script}"
        if ld["installed"] and not ld["ours"]:
            notes.append(f"A launchd agent named {svc.label} exists but points "
                         "at a different checkout, so it was left alone.")

    return {"ok": code == 0, "message": verb,
            "detail": out[-2000:], "notes": notes}


# ----------------------------------------------------------------------
# speech: previewing a voice, and the two languages that need extra packages
# ----------------------------------------------------------------------
#
# Previewing goes through the running speech server rather than loading Kokoro
# inside the panel. Three reasons, in order of importance: the panel would have
# to live in the TTS virtualenv and it deliberately needs no virtualenv at all;
# a second copy of the model would be 700 MB for no gain; and what you want to
# hear is what the SERVING process will produce, not what a different process
# with different packages thinks it would.
#
# /synthesize returns WAV bytes and does not play anything — the bot is what
# plays audio into BlackHole — so a preview cannot be heard in the Discord
# channel. That is the property that makes this safe to expose.

# What installing each language actually takes. Deliberately a fixed table and
# NOT anything derived from the request: this endpoint runs pip, so the set of
# things it can possibly install is written here and nowhere else.
#
# `after` matters and was nearly missed. `pip install misaki[ja]` succeeds and
# Japanese still does not work, because unidic ships the CODE for its
# dictionary and not the dictionary — MeCab then fails on a missing mecabrc at
# pipeline construction, which would take the speech service down on restart.
# The install is therefore two steps, and the button is not allowed to report
# success after only the first.
LANGUAGE_PACKAGES = {
    "j": {"pip": "misaki[ja]",
          "after": [["-m", "unidic", "download"]],
          "note": "Japanese also needs a dictionary of about 250 MB, "
                  "so this one takes a few minutes."},
    "z": {"pip": "misaki[zh]", "after": [], "note": ""},
}

PREVIEW_TEXT = ("Playing Blade Runner. It is two and a half hours long, and you "
                "have started it twice this month without finishing it.")


# Where an uploaded reference lands, and what it is allowed to be called.
# Written into the repo's own voices directory rather than anywhere the user
# names: this endpoint takes a filename from a browser, and the only safe
# reading of that is "a label", never "a destination".
VOICES_DIR = ROOT / "tts" / "voices"
# How much of an upload to keep. Not a model limit — Qwen has none — but long
# references embed slowly and stop adding anything, so this is a practical
# ceiling with room to spare over the ~48s that measured well.
REF_MAX_SECONDS = 90

# An upload is a CANDIDATE until somebody saves it. Prefixed rather than kept
# in a subdirectory so the voices server's _safe_ref check — which only accepts
# files sitting directly in this directory — keeps working unchanged, and a
# pending clip can still be auditioned before anyone commits to it.
#
# This exists because uploading used to save immediately, so every bad take and
# failed clone became a permanent library entry that had to be deleted by hand.
PENDING = "pending--"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _voice_ref_name(raw_name: str) -> str:
    """A filename we are willing to create, from one we were handed."""
    stem = Path(raw_name or "").name           # drop any directory part
    stem = _SAFE_NAME.sub("_", stem).lstrip(".")
    stem = stem.rsplit(".", 1)[0][:48] or "reference"
    return f"{stem}.wav"


def transcribe(path: Path) -> str:
    """What is said in a clip, for Qwen Base's ref_text.

    Run in the BOT's virtualenv, which is the one with faster-whisper — the
    panel deliberately has no virtualenv of its own, and the speech venvs do
    not carry Whisper. Returns "" on any failure: a missing transcript makes
    the clone worse, not broken, and is not worth failing an upload over.
    """
    code, out = _run([str(ROOT / ".venv" / "bin" / "python"), "-c", (
        "import sys,warnings;warnings.filterwarnings('ignore');"
        "from faster_whisper import WhisperModel;"
        "m=WhisperModel('small',device='cpu',compute_type='int8',cpu_threads=8);"
        "segs,_=m.transcribe(sys.argv[1],beam_size=5);"
        "print(''.join(s.text for s in segs).strip())"), str(path)], timeout=180)
    return out.strip() if code == 0 else ""


def _sidecar(wav: Path) -> Path:
    """Where a clip's transcript and label live: beside it, same stem.

    Beside the file rather than in .env because there is only ONE
    TTS_VOICE_REF_TEXT and a library needs one transcript per clip. Keeping
    them apart meant switching back to an older clip silently carried the
    NEWER clip's transcript, which does not error — it just clones badly, the
    same shape of fault as having no reference at all.
    """
    return wav.with_suffix(".json")


def voice_refs() -> dict:
    """Every saved clip, with what is said in it. The library.

    Backfills a sidecar for any clip that predates them, so voices uploaded
    before this existed are usable rather than invisible. Transcribing is
    slow, so a missing one is recorded as empty and filled in on demand
    rather than blocking the listing.
    """
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for wav in sorted(VOICES_DIR.glob("*.wav")):
        if wav.name.startswith(PENDING):
            continue
        meta = {}
        side = _sidecar(wav)
        if side.exists():
            try:
                meta = json.loads(side.read_text("utf-8"))
            except (ValueError, OSError):
                meta = {}
        try:
            import wave as _w
            with _w.open(str(wav)) as w:
                seconds = round(w.getnframes() / w.getframerate(), 1)
        except Exception:
            seconds = 0.0
        out.append({
            "name": wav.stem,
            "path": str(wav.relative_to(ROOT)),
            "label": meta.get("label") or wav.stem.replace("_", " "),
            "transcript": meta.get("transcript", ""),
            "added_by": meta.get("added_by", ""),
            "added": meta.get("added", ""),
            "seconds": seconds,
            "needs_transcript": not meta.get("transcript"),
        })
    return {"voices": out, "count": len(out)}


def _sweep_pending(max_age_hours: float = 6.0) -> None:
    """Delete candidates nobody saved. They are auditions, not files."""
    import time as _t
    cutoff = _t.time() - max_age_hours * 3600
    for f in VOICES_DIR.glob(f"{PENDING}*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def commit_voice_ref(pending: str, label: str, added_by: str = "",
                     transcript: str = "") -> dict:
    """Promote a candidate into the library under a name of its own.

    The name a person typed becomes both the label and the filename, so the
    library reads as a list of voices rather than a list of uploads.
    """
    import datetime
    import re as _re

    src = VOICES_DIR / Path(pending).name
    if not src.exists() or not src.name.startswith(PENDING):
        return {"ok": False, "error": "That upload is no longer around. "
                                      "Choose the file again."}
    stem = _SAFE_NAME.sub("_", (label or "").strip()).strip("._-")[:48]
    if not stem:
        stem = src.name[len(PENDING):-4] or "voice"
    dest = VOICES_DIR / f"{stem}.wav"
    n = 2
    while dest.exists():
        dest = VOICES_DIR / f"{stem}-{n}.wav"
        n += 1
    try:
        src.replace(dest)
    except OSError as exc:
        return {"ok": False, "error": f"Could not save it: {exc}"}
    _sidecar(dest).write_text(json.dumps({
        "transcript": transcript,
        "label": (label or dest.stem).strip(),
        "added_by": added_by,
        "added": datetime.date.today().isoformat(),
    }, indent=2), "utf-8")
    return {"ok": True, "name": dest.stem,
            "path": str(dest.relative_to(ROOT)), **voice_refs()}


def discard_voice_ref(pending: str) -> dict:
    """Throw a candidate away without saving it."""
    f = VOICES_DIR / Path(pending).name
    if f.name.startswith(PENDING) and f.exists():
        try:
            f.unlink()
        except OSError:
            pass
    return {"ok": True, **voice_refs()}


def describe_voice_ref(name: str) -> dict:
    """Transcribe a saved clip that has no transcript yet, and remember it."""
    wav = VOICES_DIR / f"{_voice_ref_name(name)}"
    if not wav.exists():
        return {"ok": False, "error": "No such saved voice."}
    text = transcribe(wav)
    meta = {}
    if _sidecar(wav).exists():
        try:
            meta = json.loads(_sidecar(wav).read_text("utf-8"))
        except (ValueError, OSError):
            meta = {}
    meta["transcript"] = text
    _sidecar(wav).write_text(json.dumps(meta, indent=2), "utf-8")
    return {"ok": True, "transcript": text,
            "note": "" if text else "Could not transcribe it — type what is said."}


def delete_voice_ref(name: str) -> dict:
    """Remove a saved clip and its transcript."""
    wav = VOICES_DIR / f"{_voice_ref_name(name)}"
    if not wav.exists():
        return {"ok": False, "error": "No such saved voice."}
    try:
        wav.unlink()
        _sidecar(wav).unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"Could not remove it: {exc}"}
    return {"ok": True, **voice_refs()}


def save_voice_ref(data: bytes, filename: str, start: float = 0.0,
                   label: str = "", added_by: str = "") -> dict:
    """Store an uploaded clip as a voice reference, and describe what we got.

    Mono, 24kHz, loudness-normalised, from `start` — and NOT trimmed to ten
    seconds any more.

    That cap was Chatterbox's. Its ENC_COND_LEN/DEC_COND_LEN really do stop at
    6 and 10 seconds, and make-voice-ref.sh documents it for that reason. Qwen
    has no such limit: extract_speaker_embedding takes the whole waveform and
    the encoder pools across all of it. Applying Chatterbox's numbers here
    silently threw away most of every upload. Measured on a 48s reference
    against a 10s one, the longer clip cloned closer.

    The ceiling that remains is practical rather than modelled: a very long
    reference is slow to embed and dilutes rather than sharpens, so this takes
    a generous slice instead of everything.
    """
    import shutil
    import tempfile
    import wave

    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        return {"ok": False, "error": "ffmpeg not found; brew install ffmpeg"}

    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    _sweep_pending()
    out_name = PENDING + _voice_ref_name(filename)
    out_path = VOICES_DIR / out_name
    # Adding, not replacing. Several people uploading "voice.wav" would
    # otherwise silently overwrite each other, and the loser would have no way
    # to tell - their clip would simply become someone else's.
    if out_path.exists():
        stem, n = out_path.stem, 2
        while out_path.exists():
            out_path = VOICES_DIR / f"{stem}-{n}.wav"
            n += 1
        out_name = out_path.name

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix or ".bin") as tmp:
        tmp.write(data)
        src = Path(tmp.name)
    try:
        code, err = _run([ffmpeg, "-v", "error", "-y",
                          "-ss", str(max(start, 0.0)), "-t", str(REF_MAX_SECONDS),
                          "-i", str(src),
                          "-af", "loudnorm=I=-18:TP=-2:LRA=11",
                          "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
                          str(out_path)], timeout=120)
        if code != 0 or not out_path.exists():
            return {"ok": False,
                    "error": f"Could not decode that file: {err.strip()[:200] or 'unknown format'}"}
        with wave.open(str(out_path)) as w:
            seconds = w.getnframes() / w.getframerate()
        if seconds < 1.0:
            out_path.unlink(missing_ok=True)
            return {"ok": False,
                    "error": f"Only {seconds:.1f}s of audio at that start point. "
                             "Try a smaller start offset."}
        # Transcribed, because the transcript is what unlocks Qwen's
        # in-context path — use_icl = ref_audio and ref_text, both or neither.
        #
        # I had this off for a while, on the strength of cloning af_bella: a
        # synthetic voice with no rasp, accent or breathiness, where both
        # mechanisms measured the same and sounded identical. That was the one
        # case that cannot distinguish them. On a real voice it is stark — a
        # Gilbert Gottfried clip cloned embedding-only lost 5.3dB of energy
        # above 4kHz against its source, which is exactly where rasp lives,
        # and the user's report was that it "removed all the raspiness".
        # In-context lost only 2dB.
        text = transcribe(out_path)
        note = ""
        if seconds < 5:
            note = ("Under 5s. Short references clone poorly — 20 to 60 seconds "
                    "of clear speech works much better.")
        elif seconds < 15:
            note = "Usable, but 20 to 60 seconds gives the model more to work from."
        # Nothing is written beside a candidate: a sidecar would make it look
        # like a library entry to anything that scans the directory.
        return {"ok": True,
                "pending": out_path.name,
                "path": str(out_path.relative_to(ROOT)),
                "label": label,
                "seconds": round(seconds, 1),
                "note": note,
                **voice_refs()}
    finally:
        src.unlink(missing_ok=True)


def tts_python() -> Path:
    """The interpreter serving TTS, mirroring scripts/_common.sh tts_python()."""
    env = envfile_values()
    engine = env.get("TTS_ENGINE", "kokoro")
    # Must stay in step with scripts/_common.sh tts_python(). Three engines,
    # three virtualenvs: mlx-audio pulls no torch at all and Chatterbox pins
    # torch 2.6 against Kokoro's 2.14, so none of them can share one.
    if engine == "qwen":
        mlx = ROOT / "tts" / ".venv-mlx" / "bin" / "python"
        if mlx.exists():
            return mlx
    if engine == "chatterbox":
        cb = ROOT / "tts" / ".venv-chatterbox" / "bin" / "python"
        if cb.exists():
            return cb
    venv = ROOT / "tts" / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def envfile_values() -> dict:
    from gui import envfile
    return envfile.read(ROOT / ".env")


def preview(voice: str, lang: str, text: str = "", instruct: str = "",
            ref_audio: str = "", ref_text: str = "", voices: bool = False) -> tuple:
    """(wav bytes, error dict). Asks the running server to speak, not to play.

    `instruct` is Qwen VoiceDesign's voice-in-words. It is forwarded unsaved so
    a description can be auditioned before committing it — which is the whole
    point of the mode, since the wording changes the timbre substantially and
    is not something you can predict from reading it.
    """
    # Fall back to the CONFIGURED voice, not a Kokoro name. Hardcoding
    # "bf_emma" meant Test failed outright under Qwen CustomVoice - "Speaker
    # 'bf_emma' not supported" - because the two engines' voice names share no
    # namespace. The engine that is running is the only thing that knows what
    # a valid voice looks like, so ask .env rather than guessing.
    env = envfile_values()
    fallback = (env.get("TTS_VOICE") or "").strip() or "bf_emma"
    payload = json.dumps({"text": (text or PREVIEW_TEXT)[:600],
                          "voice": voice or fallback,
                          "instruct": instruct or (env.get("TTS_VOICE_DESIGN") or ""),
                          "ref_audio": ref_audio,
                          "ref_text": ref_text,
                          "lang": lang or "auto"}).encode()
    # The lab previews against the VOICES service, never the live one. That is
    # the whole point of a second server: trying a voice, or loading a
    # different checkpoint to try one, must not interrupt her mid-sentence.
    target = ((env.get("VOICES_URL") or "http://127.0.0.1:8087").rstrip("/")
              if voices else "http://127.0.0.1:8085")
    req = urllib.request.Request(f"{target}/synthesize", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read(), None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"error": f"HTTP {e.code}"}
        return None, body
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, {"error": "The speech service is not answering. Start it "
                               "from the Status page.",
                      "detail": type(exc).__name__}


def install_language(code: str) -> dict:
    """Install one language's phonemiser into the TTS virtualenv.

    `code` is only ever used to look up LANGUAGE_PACKAGES; neither the package
    name nor any argument comes from the request. An endpoint that runs pip has
    to be a fixed menu, not a text field.

    Reports success only when the language can actually be constructed
    afterwards, verified by importing it in the TTS interpreter — not by pip's
    exit code, which is happy to install a package whose data is missing.
    """
    spec = LANGUAGE_PACKAGES.get(code)
    if spec is None:
        return {"ok": False, "message": f"No installable package for {code!r}."}
    python = tts_python()
    if not python.exists():
        return {"ok": False,
                "message": f"No TTS interpreter at {python} to install into."}

    steps = [["-m", "pip", "install", spec["pip"]]] + list(spec["after"])
    log = []
    for step in steps:
        rc, out = _run([str(python), *step], timeout=1800)
        log.append(f"$ {' '.join(step)}\n{out[-1500:]}")
        if rc != 0:
            return {"ok": False, "package": spec["pip"],
                    "message": f"Installing {spec['pip']} failed at "
                               f"`{' '.join(step)}`.",
                    "detail": "\n\n".join(log)[-4000:]}

    ok, why = _language_works(python, code)
    return {"ok": ok, "package": spec["pip"],
            "message": (f"Installed {spec['pip']}. Restart Speech to use it."
                        if ok else
                        f"{spec['pip']} installed, but the language still will "
                        f"not load: {why}"),
            "detail": "\n\n".join(log)[-4000:]}


def _language_works(python: Path, code: str) -> tuple:
    """Can the TTS interpreter actually build this language's phonemiser?

    Runs in the TTS virtualenv, because that is the one that matters and the
    panel cannot import kokoro itself. model=False keeps it to the phonemiser:
    there is no reason to load 700 MB of weights to answer this.
    """
    probe = ("from kokoro import KPipeline\n"
             f"KPipeline(lang_code={code!r}, model=False)\n"
             "print('OK')\n")
    rc, out = _run([str(python), "-c", probe], timeout=300)
    if rc == 0 and "OK" in out:
        return True, ""
    tail = [ln for ln in out.splitlines() if ln.strip()]
    return False, (tail[-1][:200] if tail else "unknown error")


def voices() -> dict:
    """The published voice list, from the speech server.

    Proxied rather than fetched here: the panel has no huggingface_hub, and the
    serving process is the one that knows which voices are already on disk.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:8085/voices", timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"error": type(exc).__name__, "voices": [], "count": 0,
                "message": "The speech service is not answering, so the voice "
                           "list is unavailable. Start it from the Status page.",
                "doc": "https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md"}


def voices_health() -> dict:
    """What the voices service reports, or why it is not answering."""
    env = envfile_values()
    url = (env.get("VOICES_URL") or "http://127.0.0.1:8087").rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=5) as r:
            return {"url": url, **json.loads(r.read().decode("utf-8"))}
    except Exception as exc:
        return {"url": url, "engine": "", "ready": False,
                "error": type(exc).__name__}


def tts_health() -> dict:
    """What the running speech service says it is doing, verbatim.

    The lab needs the engine, the mode and the saved description to tell you
    what is loaded versus what you are proposing — and the only honest source
    for that is the process actually serving, not .env, which may have been
    edited since it started.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:8085/health", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"engine": "", "ready": False, "error": type(exc).__name__}
