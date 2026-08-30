"""Driving the Discord desktop app: join a voice channel, leave, and see state.

WHAT IS AND IS NOT POSSIBLE HERE, established by testing rather than assumed,
because the obvious approaches all fail:

  THE BOT API CANNOT DO THIS. Bots can join voice channels, but Go Live is not
  in the bot API at all. The account that streams is a person's account signed
  into the desktop app, and it has to stay that way.

  ACCESSIBILITY IS A DEAD END. Discord is Electron, and its accessibility tree
  is six nested groups deep and then nothing — no buttons, no channel list, no
  text. Forcing it with AXManualAccessibility returns success and changes
  nothing. There is no element to find and press.

  AUTOMATING THE USER TOKEN IS OUT. That is self-botting, against Discord's
  terms, and the realistic cost is the account being banned — which loses the
  stream, the server, and the account in one go. Not worth it for convenience.

WHAT WORKS: Discord's own global keybinds. "Switch To Voice Channel" and
"Disconnect From Voice Channel" are assignable in Discord's settings, and a
keystroke is a supported, documented feature rather than a circumvention. The
QuickSwitcher (Cmd-K) navigates to a named channel first, so the join lands
somewhere specific.

WHAT STILL CANNOT BE AUTOMATED: starting the stream. There is no Go Live
keybind — confirmed against the actual keybind list. So this module joins and
leaves, and REPORTS whether a stream is live, which is most of the value: the
panel can say "the stream is down" without anyone opening Discord.
"""

from __future__ import annotations

import re
import subprocess
import time

# Discord's voice and media traffic. A client sitting idle in the app holds no
# such connection; one in a voice channel does. This is the signal that lets
# every action below verify itself instead of reporting success blindly.
_MEDIA_PORTS = (443, 50000)


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def _pids():
    """Every Discord process, not just the main one.

    Matching only Discord.app/Contents/MacOS/Discord finds the top-level
    process, which holds no media connections — those live in the helper
    processes ("Discord Helper", the Electron Framework children). Checking
    only the parent reported "not connected to voice" while a stream was
    visibly live.
    """
    rc, out = _run(["pgrep", "-f", "/Applications/Discord.app/"])
    if rc != 0:
        return []
    return [int(p) for p in out.split() if p.strip().isdigit()]


def state() -> dict:
    """Is Discord running, connected to voice, and streaming?

    Connection is inferred from Discord holding a UDP association with a remote
    media server. Streaming is inferred from that plus a renderer working hard
    enough to be encoding video — measured at ~48% CPU on a live desktop share
    here, against ~0% for a client merely sitting in a channel.
    """
    pids = _pids()
    if not pids:
        return {"running": False, "in_voice": False, "streaming": False,
                "detail": "Discord is not running"}

    # Discord's voice/media transport is UDP, and those sockets exist only
    # while connected to a voice channel. They are UNBOUND — lsof shows
    # "UDP *:62996" with no peer — so looking for an "->address" the way a TCP
    # check would find nothing even mid-stream. The TCP connections to
    # Cloudflare are the gateway websocket and are present regardless, so they
    # say nothing about voice.
    udp_sockets = 0
    busiest = 0.0
    for pid in pids:
        rc, out = _run(["lsof", "-nP", "-iUDP", "-a", "-p", str(pid)])
        udp_sockets += sum(1 for ln in out.splitlines()
                           if ln.startswith("Discord") and "UDP" in ln)
        rc, out = _run(["ps", "-o", "%cpu=", "-p", str(pid)])
        try:
            busiest = max(busiest, float(out.strip() or 0))
        except ValueError:
            pass

    in_voice = udp_sockets > 0
    # Encoding a desktop share costs real CPU in the renderer; sitting in a
    # voice channel does not. Deliberately generous: a false "not streaming"
    # prompts someone to check, while a false "streaming" leaves nobody
    # watching anything and nobody told.
    streaming = in_voice and busiest >= 20.0
    return {
        "running": True,
        "in_voice": in_voice,
        "streaming": streaming,
        "peak_cpu": round(busiest, 1),
        "udp_sockets": udp_sockets,
        "detail": ("streaming" if streaming else
                   "in a voice channel, not streaming" if in_voice else
                   "running, not connected to voice"),
    }


# --- sending keystrokes -------------------------------------------------
#
# Written as a parser over a "cmd+shift+j" style string rather than fixed
# constants, because the keybind is whatever the operator assigned in Discord
# and this module has no way to discover it.

_MODIFIERS = {
    "cmd": "command down", "command": "command down",
    "ctrl": "control down", "control": "control down",
    "alt": "option down", "opt": "option down", "option": "option down",
    "shift": "shift down",
}

_SPECIAL = {
    "enter": 36, "return": 36, "escape": 53, "esc": 53, "tab": 48,
    "space": 49, "up": 126, "down": 125, "left": 123, "right": 124,
}


def send_combo(combo: str) -> tuple:
    """Send a keystroke like "cmd+shift+j". Returns (ok, message).

    Global, not targeted: macOS has no per-window key delivery, so this reaches
    whatever holds focus. Discord's keybinds are registered globally, which is
    what makes that acceptable — but focus still matters for the QuickSwitcher,
    so callers focus Discord first.
    """
    combo = (combo or "").strip().lower()
    if not combo:
        return False, "no keybind configured"

    parts = [p.strip() for p in re.split(r"[+\-\s]+", combo) if p.strip()]
    mods = [_MODIFIERS[p] for p in parts if p in _MODIFIERS]
    keys = [p for p in parts if p not in _MODIFIERS]
    if len(keys) != 1:
        return False, f"could not read {combo!r} as one key plus modifiers"

    key = keys[0]
    using = f" using {{{', '.join(mods)}}}" if mods else ""
    if key in _SPECIAL:
        script = f'tell application "System Events" to key code {_SPECIAL[key]}{using}'
    elif len(key) == 1:
        script = f'tell application "System Events" to keystroke "{key}"{using}'
    else:
        return False, f"unknown key {key!r}"

    rc, out = _run(["osascript", "-e", script])
    if rc != 0:
        return False, out.strip()[:200] or "osascript failed"
    return True, "sent"


def focus_discord() -> bool:
    rc, _ = _run(["osascript", "-e",
                  'tell application "Discord" to activate'])
    if rc == 0:
        time.sleep(0.6)
    return rc == 0


# --- actions ------------------------------------------------------------

def _settings():
    from pathlib import Path
    from gui import envfile
    env = envfile.read(Path(__file__).resolve().parent.parent / ".env")
    return {
        "channel": env.get("DISCORD_VOICE_CHANNEL", "").strip(),
        "join": env.get("DISCORD_KEYBIND_JOIN", "").strip(),
        "leave": env.get("DISCORD_KEYBIND_LEAVE", "").strip(),
    }


def _type(text: str):
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    return _run(["osascript", "-e",
                 f'tell application "System Events" to keystroke "{safe}"'])


def join() -> dict:
    """Navigate to the configured channel, then join it.

    Two steps, because they are two different mechanisms. The QuickSwitcher
    (Cmd-K) is how you reach a NAMED channel — keybinds toggle things, they
    cannot navigate. "Switch To Voice Channel" then joins whatever is on
    screen. Doing only the second would join whatever happened to be open.
    """
    cfg = _settings()
    if not cfg["channel"]:
        return {"ok": False, "message": "Set DISCORD_VOICE_CHANNEL first."}
    if not cfg["join"]:
        return {"ok": False,
                "message": "Set DISCORD_KEYBIND_JOIN to the keybind you "
                           "assigned to 'Switch To Voice Channel' in Discord."}
    if not _pids():
        return {"ok": False, "message": "Discord is not running."}

    before = state()
    focus_discord()
    ok, msg = send_combo("cmd+k")
    if not ok:
        return {"ok": False, "message": f"Could not open the quick switcher: {msg}"}
    time.sleep(0.8)
    _type(cfg["channel"])
    time.sleep(1.2)                 # let the search results settle
    send_combo("enter")
    time.sleep(1.5)                 # let the channel view load

    ok, msg = send_combo(cfg["join"])
    if not ok:
        return {"ok": False, "message": f"Join keybind failed: {msg}"}

    # Verified, not assumed. A keystroke that goes nowhere looks identical to
    # one that worked, and the whole point of this is not having to go and look.
    for _ in range(10):
        time.sleep(1)
        now = state()
        if now["in_voice"]:
            return {"ok": True, "message": f"Joined {cfg['channel']}.",
                    "state": now}
    return {"ok": False,
            "message": f"Sent the keystrokes but Discord is still not in a "
                       f"voice channel. Check the channel name and that "
                       f"{cfg['join']!r} is assigned to 'Switch To Voice "
                       f"Channel'.",
            "state": state(), "was": before}


def leave() -> dict:
    cfg = _settings()
    if not cfg["leave"]:
        return {"ok": False,
                "message": "Set DISCORD_KEYBIND_LEAVE to the keybind you "
                           "assigned to 'Disconnect From Voice Channel'."}
    if not _pids():
        return {"ok": False, "message": "Discord is not running."}

    focus_discord()
    ok, msg = send_combo(cfg["leave"])
    if not ok:
        return {"ok": False, "message": f"Leave keybind failed: {msg}"}
    for _ in range(8):
        time.sleep(1)
        now = state()
        if not now["in_voice"]:
            return {"ok": True, "message": "Left the voice channel.", "state": now}
    return {"ok": False,
            "message": "Sent the keystroke but Discord still looks connected.",
            "state": state()}
