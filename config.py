"""Configuration, loaded from environment / .env file.

Nothing secret is ever hardcoded here. Copy .env.example to .env and fill it in.
"""

import os
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

# Several defaults below are the name of a thing that only exists on one
# platform — an executable, a process, an audio device. They're branched here
# rather than left Windows-shaped and overridden in every .env, so a fresh
# checkout runs without a settings file full of corrections.
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        print(f"FATAL: {name} is not set. Check your .env file.", file=sys.stderr)
        sys.exit(1)
    return val


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


# --- What she is called ---
#
# One name, feeding everything that displays or matches on it: the mpv window
# title, the device Plex sees, the Firefox profile, and the spoken wake word.
# Those were four separate settings that each happened to default to "Athena",
# so renaming meant finding all four and knowing they were related.
#
# Defined here, above everything that reads it, because Python executes this
# file top to bottom and a default cannot reference a name defined later.
BOT_NAME = os.getenv("BOT_NAME", "").strip() or "Athena"

# --- Discord ---
DISCORD_TOKEN = _req("DISCORD_TOKEN")
ALLOWED_CHANNEL_ID = _int("ALLOWED_CHANNEL_ID")
STATUS_CHANNEL_ID = _int("STATUS_CHANNEL_ID")

# --- Plex ---
PLEX_URL = _req("PLEX_URL")
PLEX_TOKEN = _req("PLEX_TOKEN")

# --- mpv ---
# Leave MPV_PATH empty if mpv is on your PATH — `brew install mpv` puts it
# there, so on macOS this is normally blank.
MPV_PATH = os.getenv("MPV_PATH", "").strip() or None
MPV_FULLSCREEN = _bool("MPV_FULLSCREEN", True)
# macOS only: "no" gives mpv's own fullscreen, which stays on the current Space;
# "yes" is the green-button kind that moves the window to a Space of its own.
# Non-native is better for switching between mpv and Spotify, but appears to
# cost mpv the ability to hide the mouse cursor — see MPV_EXTRA_OPTS notes.
MPV_NATIVE_FS = os.getenv("MPV_NATIVE_FS", "").strip().lower() or "no"
# Extra mpv options, comma separated, e.g. "hwdec=auto,vo=gpu-next"
MPV_EXTRA_OPTS = os.getenv("MPV_EXTRA_OPTS", "").strip()
# Name for mpv's JSON IPC channel. A BARE name on every platform — player.py
# turns it into the real address, which differs: \\.\pipe\<name> on Windows,
# /tmp/<name>.sock here.
#
# This used to default to "/tmp/athena-mpv.sock" off Windows, which looked
# reasonable and was not. player.py builds the socket path as f"/tmp/{name}.sock"
# after appending a pid and counter, so a full path here produced
# /tmp//tmp/athena-mpv.sock-<pid>-1.sock — a directory that does not exist —
# and mpv silently never created a socket. The symptom was
# "mpv did not open its IPC channel within 60s": an mpv window on screen that
# the bot could not talk to, so no idle image and no control of any kind.
MPV_IPC_SOCKET = os.getenv("MPV_IPC_SOCKET", "").strip() or "athena-mpv"

# --- Natural language ---
# One backend: a local model via Ollama. There was a second, Anthropic's API,
# kept as an alternative when this ran on an RTX 2060 Super where an 8GB VRAM
# ceiling made local inference a compromise. It has been removed rather than
# left switched off — the reference machine has 32GB unified and a 25GB Metal working set,
# so local is no longer the fallback option, it is the better one.
#
# The consequence is worth stating plainly: there is no remote fallback. If
# Ollama is not running, the model tier is simply unavailable and the bot
# degrades to fast_match plus offline_match, which still covers `play X`,
# `pause`, `skip` and `back 30s`.
NL_BACKEND = os.getenv("NL_BACKEND", "").strip().lower()  # "ollama", or auto
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M").strip()
# Layers to put on the GPU. Blank lets Ollama decide, and on this machine blank
# is very likely the right answer — which is a reversal of the Windows setup,
# so it is worth saying why rather than leaving the old advice in place.
#
# On the RTX 2060 Super this existed to be set to 0. Inference pinned the card
# at ~100% for seconds at a time and the screen-share encoder stuttered, so
# moving inference to the CPU bought back a smooth stream and freed VRAM out of
# a fixed 8GB budget.
#
# Neither half of that applies to an M1 Max:
#   * Video encode for the screen share runs on the media engine, which is
#     separate silicon from the 32 GPU cores. LLM inference on the GPU does not
#     contend with it the way CUDA work contended with NVENC's neighbours.
#   * There is no separate VRAM to free. 32GB is unified, shared with the CPU,
#     so "freeing VRAM" by moving layers to the CPU frees nothing at all — the
#     weights occupy the same memory either way.
#
# Set it to 0 only if a stutter is actually measured, not on the old reasoning.
_ngl = os.getenv("OLLAMA_NUM_GPU", "").strip()
OLLAMA_NUM_GPU = int(_ngl) if _ngl.lstrip("-").isdigit() else None
# Optional smaller/faster model just for flavour text, which runs far more
# often than tool calling and is only cosmetic. Defaults to OLLAMA_MODEL.
FLAVOR_MODEL = os.getenv("FLAVOR_MODEL", "").strip() or OLLAMA_MODEL
# Device for flavour specifically. Worth separating on the Windows box: tool
# calls carry ~3000 tokens of schema and prefill is what CPUs are slow at
# (measured 10s on GPU versus 29s on CPU for the same request), while a flavour
# prompt is a few hundred tokens and ran fine on CPU. Since flavour fires on
# nearly every action and tool calls are rare, putting flavour on the CPU
# removed most of the GPU contention behind a stuttering screen share.
#
# On this machine that contention is largely not there to remove — see
# OLLAMA_NUM_GPU above — so this is most likely another one to leave blank.
_fngl = os.getenv("FLAVOR_NUM_GPU", "").strip()
FLAVOR_NUM_GPU = int(_fngl) if _fngl.lstrip("-").isdigit() else OLLAMA_NUM_GPU
# Cold prefill of the tool schema can take tens of seconds on slow hardware;
# 60s was low enough that a first request could time out and report a failure.
try:
    OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "").strip() or 180)
except ValueError:
    OLLAMA_TIMEOUT = 180.0

# Reasoning models write their chain of thought out as ordinary reply text
# unless told not to, and this codebase has no idea what to do with that: a
# thinking trace arrives where a one-line answer or a tool call was expected,
# so the intent classifier reads gibberish and the chat path ships paragraphs
# of deliberation in Athena's voice. .env.example has carried a warning about
# exactly this under OLLAMA_MODEL ("brain.py does not send think: false") since
# the qwen3.5:4b evaluation. This is that gap closed.
#
# It matters more here than it did on the 2060 Super. An 8GB VRAM ceiling ruled
# out most reasoning models by size; 32GB unified does not, so the models worth
# bake-off testing on Apple silicon are disproportionately ones that think.
#
#   blank / false  -> send "think": false. The default, and what you want.
#   true           -> let the model think, for comparing behaviour by hand.
#   none / omit    -> don't send the field at all. For an Ollama too old to
#                     know it, or a model that rejects it outright.
# How long Ollama keeps a model resident after a request. Its own default is
# 5 minutes, after which the next request pays a full reload.
#
# That default is wrong for this deployment in a way worth spelling out.
# Measured on this machine: qwen3:8b answers in ~2s warm and ~12.5s cold. The
# bot sits idle in a room for hours at a time, so under the default almost
# every FIRST question of an evening pays the cold path — which is exactly the
# question someone is waiting on, and it made the model look far slower than
# it is. granite4:3b hid this by being small enough (1.3s cold) for nobody to
# notice.
#
# -1 pins the model in memory indefinitely. The cost is real and should be
# understood rather than discovered: ~8GB of the 32GB stays held for as long
# as Ollama runs, alongside mpv, Discord, Whisper and the screen-share
# encoder. There is room, but it is not free.
#
# Accepts anything Ollama does: -1 forever, 0 unload immediately, a number of
# seconds, or a duration string like "30m".
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "").strip() or "-1"
try:
    # Send a bare number as a number; Ollama reads "-1" as a duration string
    # and rejects it, while -1 as an integer means forever.
    OLLAMA_KEEP_ALIVE = int(OLLAMA_KEEP_ALIVE)
except ValueError:
    pass

_think = os.getenv("OLLAMA_THINK", "").strip().lower()
OLLAMA_THINK = (
    None if _think in ("none", "omit")
    else _think in ("1", "true", "yes", "on")
)

if not NL_BACKEND:
    NL_BACKEND = "ollama" if OLLAMA_MODEL else ""
NL_ENABLED = NL_BACKEND == "ollama"

# How long to wait for mpv to come up and open its IPC channel. The bot spawns
# mpv itself rather than letting python-mpv-jsonipc do it, because that library
# gives up after a fixed 10s — too tight on machines where mpv is slow to start.
MPV_START_TIMEOUT = _int("MPV_START_TIMEOUT", 60) or 60

# --- Spotify ---
# Register an app at developer.spotify.com. The redirect URI must be a loopback
# address (127.0.0.1), not localhost — Spotify no longer accepts localhost.
# Playback control requires a Premium account.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
SPOTIFY_REDIRECT_URI = (
    os.getenv("SPOTIFY_REDIRECT_URI", "").strip() or "http://127.0.0.1:8888/callback"
)
SPOTIFY_CACHE = os.getenv("SPOTIFY_CACHE", ".spotify-cache").strip() or ".spotify-cache"
# Optional: which Connect device to target, matched as a substring. Blank picks
# the active device, then any Computer.
SPOTIFY_DEVICE_NAME = os.getenv("SPOTIFY_DEVICE_NAME", "").strip()
# Optional: how to launch Spotify if it isn't already running.
#
# On Windows this is the path to Spotify.exe. On macOS an .app bundle is a
# directory, not something to exec, so the default goes through `open -a`,
# which is also what finds the app wherever it was installed. Set it to a
# full command line if that guess is wrong for this machine.
SPOTIFY_EXE = os.getenv("SPOTIFY_EXE", "").strip() or (
    "open -a Spotify" if IS_MAC else ""
)

# --- Window handling when switching to music ---
# Get mpv out of the way so viewers see Spotify instead of a static image.
MUSIC_MINIMIZE_MPV = _bool("MUSIC_MINIMIZE_MPV", True)
MUSIC_FOCUS_SPOTIFY = _bool("MUSIC_FOCUS_SPOTIFY", True)
# Fill the screen with Spotify so viewers can read the queue and lyrics panel.
MUSIC_MAXIMIZE_SPOTIFY = _bool("MUSIC_MAXIMIZE_SPOTIFY", True)
# Window title mpv is launched with — used to find it again.
MPV_WINDOW_TITLE = os.getenv("MPV_WINDOW_TITLE", "").strip() or BOT_NAME
# The process name to find Spotify's window by. macOS drops the .exe; macctl
# strips a trailing .exe anyway, so an .env carried over from Windows still
# works rather than silently matching nothing.
_default_spotify_process = "Spotify" if IS_MAC else "Spotify.exe"
SPOTIFY_PROCESS = (
    os.getenv("SPOTIFY_PROCESS", "").strip() or _default_spotify_process
)
# Karaoke mode keeps mpv visible and draws synced lyrics over the idle screen.
KARAOKE_DEFAULT = _bool("KARAOKE_DEFAULT", False)

# --- YouTube ---
# mpv resolves YouTube URLs through yt-dlp, but only if it can find the binary.
# A pip install puts it in Python's Scripts directory, which is usually not on
# PATH, so it's located here and handed to mpv explicitly rather than asking
# anyone to edit their PATH.
def _find_ytdlp() -> str:
    import shutil
    import sysconfig

    found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if found:
        return found
    scripts = sysconfig.get_path("scripts") or ""
    for name in ("yt-dlp.exe", "yt-dlp"):
        candidate = os.path.join(scripts, name)
        if os.path.exists(candidate):
            return candidate
    return ""


YTDL_PATH = os.getenv("YTDL_PATH", "").strip() or _find_ytdlp()
# mpv's own default picks a 360p combined stream — measured 640x360 at 228kbps
# on a video that offers 1080p60. Asking for a format explicitly is worth ~9x
# the resolution.
#
# h264 is preferred over "best" deliberately. The Windows reasoning was that
# "best" resolves to AV1, which Maxwell and earlier cannot decode in hardware.
# That conclusion holds on an M1 Max, but only AV1 deserves the fear, and it
# is worth writing down what was actually measured here rather than carrying the
# Windows reasoning over on trust.
#
# Decoding 20s of 1080p30 on this machine, comparing --hwdec=videotoolbox-copy
# against --hwdec=no, by CPU time:
#
#     H.264    2.18s -> 0.56s   hardware decode, ~4x cheaper
#     VP9      1.45s -> 0.59s   VideoToolbox path, ~2.5x cheaper
#     AV1      1.60s -> 1.61s   no change; mpv says "Using software decoding"
#
# So AV1 is the one with no hardware path, confirmed three ways: the CPU cost is
# identical either way, mpv states outright that it is decoding in software, and
# VTIsHardwareDecodeSupported('av01') returns false. AV1 hardware decode arrives
# with M3; this is an M1.
#
# VP9 is more interesting and the reason not to over-tighten this string.
# VTIsHardwareDecodeSupported('vp09') also returns false, yet the VideoToolbox
# path is measurably much cheaper than software and mpv reports it as hardware
# decoding — whether that is an undocumented media-engine capability or simply a
# far better-optimised decoder inside VideoToolbox than libvpx, it is cheap in
# practice and not worth avoiding. Preferring avc1 and letting VP9 be the
# fallback is right; excluding VP9 would be wrong.
#
# One caveat on the numbers above: they come from a synthetic testsrc2 pattern,
# which compresses unusually well. Treat the ratios as real and the absolute
# figures as optimistic. Falls back cleanly when a video has no h264 rendition.
YTDL_FORMAT = os.getenv("YTDL_FORMAT", "").strip() or (
    "bestvideo[vcodec^=avc1][height<=?1080]+bestaudio/"
    "best[vcodec^=avc1][height<=?1080]/best[height<=?1080]/best"
)
# A signed-in YouTube session, for age-gated videos. LEAVE THESE OFF.
#
# Measured 2026-08-13, and the result was the opposite of the intention:
# attaching a Firefox session BROKE ordinary videos. Big Buck Bunny resolved
# 33 formats anonymously and zero with cookies attached. YouTube serves no
# formats at all to an authenticated non-browser client, while anonymous
# requests are served normally — so enabling this does not fix age-gated
# playback, it removes YouTube playback entirely.
#
# The age gate itself WAS passed: the error changed from "Sign in to confirm
# your age" to "no usable formats", so the cookies were read and accepted.
# Every player_client was tried (web, android, ios, tv, web_safari, mweb,
# web_embedded); none returned formats.
#
# Also dead: Chrome and Edge cannot be read at all on current Windows —
# "Failed to decrypt with DPAPI", yt-dlp issue 10927, Chromium app-bound
# encryption. Firefox reads fine; that was never the problem.
#
# Kept because the plumbing is correct and the situation may change with a
# yt-dlp release. Re-measure with an ordinary video before switching it on.
YTDL_COOKIES_FROM_BROWSER = os.getenv("YTDL_COOKIES_FROM_BROWSER", "").strip()
# ...or a cookies.txt exported by a browser extension. Same caveat applies:
# it is the authenticated session that YouTube penalises, not how it arrived.
YTDL_COOKIEFILE = os.getenv("YTDL_COOKIEFILE", "").strip()

# Lookups are a page fetch; give up rather than leave someone waiting.
try:
    YOUTUBE_TIMEOUT = float(os.getenv("YOUTUBE_TIMEOUT", "").strip() or 20)
except ValueError:
    YOUTUBE_TIMEOUT = 20.0

# A link that hits YouTube's age gate opens in a real browser instead of
# failing outright. Unlike YTDL_COOKIES_FROM_BROWSER above, this doesn't ask
# yt-dlp to extract a stream URL at all — it just shows the normal
# youtube.com page to a signed-in browser, so the "authenticated client gets
# zero formats" wall never applies. No playback control once it's open
# (pause/seek/stop don't reach it); see browser.py.
YOUTUBE_BROWSER_FALLBACK = _bool("YOUTUBE_BROWSER_FALLBACK", True)
# Leave blank if firefox.exe is on PATH.
BROWSER_PATH = os.getenv("BROWSER_PATH", "").strip() or None
# A separate Firefox profile, so the bot's YouTube login isn't whoever's
# personal browsing profile happens to be signed in. Create it once with
# `firefox.exe -P` and sign into YouTube inside it.
BROWSER_PROFILE = os.getenv("BROWSER_PROFILE", "").strip() or BOT_NAME
# -kiosk: no toolbar, no tabs, no chrome — the video fills the screen the
# same way mpv does. There's no in-window way out of it; Alt+F4 closes it.
BROWSER_KIOSK = _bool("BROWSER_KIOSK", True)

# --- Voice commands ---
# Spoken commands, captured from the local Discord client's audio output via a
# VB-Audio virtual cable. NOT Discord's voice-receive API, which has been
# unusable since DAVE end-to-end encryption was enforced in March 2026 — see
# voice.py's module docstring. Off by default; needs numpy, sounddevice and
# faster-whisper, none of which production has installed.
VOICE_ENABLED = _bool("VOICE_ENABLED", False)
# Name prefix of the capture device — the far end of the cable Discord plays
# into. Matched case-insensitively against the start of the device name.
# macOS has no VB-Audio Cable. BlackHole is the equivalent and is what the
# setup notes assume: `brew install blackhole-2ch`. Unlike VB-Cable it is a
# single loopback device rather than a named input/output pair, so the same
# name appears on both sides of the cable.
# PortAudio exposes the same physical device through several host APIs, and
# they are not equivalent: on the Windows machine MME reads the VB-Cables as
# silent while WASAPI, DirectSound and WDM-KS all see the same signal. So the
# capture and playback lookups both filter on one, and it has to be named per
# platform — macOS has only Core Audio, and asking there for WASAPI matches
# nothing at all. That was not a cosmetic mismatch: speech.py re-resolves its
# output device whenever the cached index stops looking right, and an
# unsatisfiable host-API test made that true on every single line spoken.
AUDIO_HOST_API = "Core Audio" if IS_MAC else "WASAPI"

_default_voice_device = "BlackHole 2ch" if IS_MAC else "CABLE Output"
VOICE_DEVICE = os.getenv("VOICE_DEVICE", "").strip() or _default_voice_device

# Phase gate. True transcribes, wake-matches and logs, but never acts, so the
# wake list can be tuned against a real room before it can touch playback.
VOICE_DRY_RUN = _bool("VOICE_DRY_RUN", True)

# --- Who was talking ---
# Name the speaker on each line of the tuning log. The cable carries one
# already-mixed stream, so this cannot come from the audio; it comes from
# Discord's opcode 5 SPEAKING frames, which are ordinary signalling and not
# covered by the DAVE encryption that killed voice receive. See speakers.py.
#
# The cost is that the bot must hold a voice connection to receive them, so
# it appears in the voice channel as a participant. It joins muted and
# transmits nothing.
VOICE_TRACK_SPEAKERS = _bool("VOICE_TRACK_SPEAKERS", False)
# The VOICE channel to sit in. Not ALLOWED_CHANNEL_ID, which is the text
# channel she talks in — they are different channels.
VOICE_CHANNEL_ID = _int("VOICE_CHANNEL_ID")
# How far outside an utterance a speaking frame still counts as belonging to
# it. Audio reaches us through Discord, a virtual cable and a segmenter that
# infers the end of speech from a gap of silence; the speaking frame arrives
# over the gateway. The two paths have independent latency and neither is
# exact, so the match is deliberately loose. Too low and short remarks go
# unattributed; too high and everyone in a busy channel matches everything.
VOICE_SPEAKER_TOLERANCE_MS = _int("VOICE_SPEAKER_TOLERANCE_MS", 1500)

# A dedicated file recording every utterance, the verdict it got, and what the
# bot did about it — the raw material for tuning the wake list against a real
# room. Separate from the bot's own log, which records only the SHAPE of an
# utterance ("3.9s: NO-WAKE (13 words)") because everyone's conversation passes
# through the transcriber and most of it is people talking to each other.
#
# This file is the one place those words are written down, which is why turning
# it off is a single switch rather than an audit of every log line.
#
# The comment here used to claim a blank path turned it off. It did not: the
# `or` below meant blank fell back to the default, so there was in fact no way
# to stop recording what the room said. VOICE_TUNING_ENABLED is that way.
VOICE_TUNING_ENABLED = _bool("VOICE_TUNING_ENABLED", True)
VOICE_TUNING_LOG = (
    (os.getenv("VOICE_TUNING_LOG", "").strip() or "logs/voice-tuning.log")
    if VOICE_TUNING_ENABLED else ""
)

# Rotation, because this grows without limit and nobody notices until it is
# hundreds of megabytes of other people's conversation. Size in megabytes, and
# how many old files to keep beside the current one; 0 keeps none, so the log
# never exceeds the size below.
VOICE_TUNING_MAX_MB = _float("VOICE_TUNING_MAX_MB", 5.0)
VOICE_TUNING_KEEP = _int("VOICE_TUNING_KEEP", 3)

# Save each utterance as a WAV — exactly the 16 kHz mono buffer Whisper was
# given — so a mis-heard command can be listened to rather than guessed at.
# Filenames carry the timestamp and transcript so they line up with the tuning
# log. Blank disables. This records everyone in the channel, so it is a tuning
# tool, not something to leave on.
VOICE_SAVE_AUDIO = os.getenv("VOICE_SAVE_AUDIO", "").strip()
VOICE_SAVE_AUDIO_KEEP = _int("VOICE_SAVE_AUDIO_KEEP", 200)

# Spellings Whisper actually produces for "Athena"; it rarely gets it right.
# "next" is included because it is the most common rendering of the name in
# practice — safe only because the prefix below is mandatory. See voice.py.
def _wordlist(name: str, default: str) -> str:
    """A wake list, where an explicitly empty value means empty, not default.

    Blank normally falls back to the built-in list, but these lists sometimes
    need to be switched off outright — the compound list in particular exists
    to patch around a badly-heard name and should be emptied, not tolerated,
    once the name is heard properly.
    """
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


# The spellings below are what the transcriber actually produces for "Athena",
# collected from live use — not variations someone imagined. They are useless
# for any other name, so a renamed bot gets its own name as the only default
# and will want its own list built the same way: turn VOICE_TUNING_ENABLED on,
# talk to it for an evening, and read Logs -> Heard for what came back.
#
# Renaming is not free. The wake word was chosen by measurement (see voice.py):
# "Athena" scored 26/28 where "Jarvis" managed 6/9 and "Lilith" 0/4, because
# Discord's voice gate eats the opening consonant and names starting on an open
# vowel survive that. A name beginning with a plosive will be missed often.
_WAKE_DEFAULT = ("athena athina atena athene"
                 if BOT_NAME.lower() == "athena" else BOT_NAME.lower())
VOICE_WAKE_WORDS = _wordlist("VOICE_WAKE_WORDS", _WAKE_DEFAULT)
# Accepted but not required. A prefix was mandatory under the old one-syllable
# name, which needed the extra structure to avoid false wakes. "Athena" carries
# enough on its own, and demanding "hey" in front reintroduces the clipped-attack
# problem — short unstressed words are the first thing Discord's gate eats.
VOICE_WAKE_PREFIXES = _wordlist("VOICE_WAKE_PREFIXES", "hey hi ok okay hay")
VOICE_REQUIRE_PREFIX = _bool("VOICE_REQUIRE_PREFIX", False)
# Whole-phrase renderings, comma-separated, where recognition fuses the prefix
# into the name. Empty by default: every entry this once held was damage control
# for a name the model could not hear, and each matched real speech somewhere.
VOICE_WAKE_COMPOUNDS = _wordlist("VOICE_WAKE_COMPOUNDS", "")

# Segmentation. The cable carries exact digital silence between utterances, so
# a plain energy gate is enough and no native VAD dependency is needed.
VOICE_THRESHOLD = _float("VOICE_THRESHOLD", 0.003)
VOICE_SILENCE_MS = _int("VOICE_SILENCE_MS", 1000)
VOICE_PREROLL_MS = _int("VOICE_PREROLL_MS", 500)
VOICE_MIN_MS = _int("VOICE_MIN_MS", 400)
VOICE_MAX_S = _float("VOICE_MAX_S", 10.0)
# Anything longer than this is a film or a conversation, not an instruction.
VOICE_MAX_WORDS = _int("VOICE_MAX_WORDS", 14)

VOICE_STT_TIMEOUT = _float("VOICE_STT_TIMEOUT", 30.0)
# Beam search rather than greedy. Utterances are seconds long and the model
# runs far faster than realtime, so the latency is invisible while the accuracy
# is not — a mis-heard wake word is a command that silently does nothing.
VOICE_BEAM_SIZE = _int("VOICE_BEAM_SIZE", 5)
# tiny / base / small / medium / large-v3, or distil-large-v3 for English.
#
# On the Windows box this was sized against free VRAM on the card the LLM also
# lived on (small ~0.5GB, medium ~1.5GB, large-v3 ~3GB at int8), and medium fit.
# On a Mac there is no CUDA, CTranslate2 has no Metal backend, and so the
# constraint is not memory at all — 32GB unified swallows any of them — it is
# how fast eight CPU cores can chew through the model.
#
# Measured on an M1 Max, int8, 6 threads, on a 3.4s utterance:
#
#     tiny     15.9x realtime
#     base     13.4x realtime   - and got the words wrong ("put on" -> "good on")
#     small     4.8x realtime   - correct
#     medium    1.7x realtime   - correct
#
# So the default drops to small here. medium is not wrong, but 1.7x realtime
# against a 10s VOICE_MAX_S ceiling is ~6s of thinking before a spoken command
# does anything, and small does the same job in ~2s. base is the one to avoid
# despite being quick: a mis-heard command silently does nothing, which is a
# worse failure than a slow one.
_default_whisper_model = "small" if IS_MAC else "medium"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "").strip() or _default_whisper_model
# There is no CUDA on a Mac, and CTranslate2 has no Metal backend either — so
# faster-whisper runs on the CPU here regardless of what silicon is available.
# That is less bad than it sounds: CTranslate2's int8 path uses NEON on Apple
# silicon and runs comfortably faster than realtime on short utterances, which
# is all this ever transcribes.
_default_whisper_device = "cpu" if IS_MAC else "cuda"
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "").strip() or _default_whisper_device

# How many CPU threads CTranslate2 may use. Only meaningful when the device is
# the CPU, which on a Mac it always is — so on this machine it is the single
# knob that decides how fast anything gets transcribed.
#
# 0 means "let CTranslate2 choose", and its choice is conservative. This host
# has 8 performance cores and 2 efficiency cores; the efficiency cores are
# actively unhelpful for this, since the whole batch waits on the slowest
# thread. 6 leaves headroom for the bot, mpv and whatever is encoding the
# screen share, all of which are competing for the same 8 good cores.
WHISPER_CPU_THREADS = _int("WHISPER_CPU_THREADS", 6)
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8").strip() or "int8"

# --- Voice output (text to speech) ---
# Athena speaking her replies into the voice channel. Synthesis happens in a
# separate process — tts/tts_server.py, Python 3.10 — because every GPU TTS
# path fails on this interpreter's 3.14 for want of wheels, and because a
# second CUDA stack in the bot's process would put faster-whisper at risk.
# Start it with:  tts\.venv310\Scripts\python tts_server.py --preload
TTS_ENABLED = _bool("TTS_ENABLED", False)
TTS_URL = os.getenv("TTS_URL", "").strip() or "http://127.0.0.1:8085"
TTS_VOICE = os.getenv("TTS_VOICE", "").strip() or "bf_emma"
# Playback goes into Discord's MICROPHONE cable, not its speaker: this is what
# the channel hears. The return cable is the far side of the one the host
# account's Discord reads as its microphone — and that account must be
# UNMUTED for any of it to leave the machine.
# The return cable, feeding Discord's microphone. On Windows that's the second
# VB-Audio cable; on macOS it's a second BlackHole device, which has to be
# installed separately from the 2ch one above (`brew install blackhole-16ch`)
# precisely so the two directions don't share a device and loop back.
_default_tts_device = "BlackHole 16ch" if IS_MAC else "CABLE-B Input"
TTS_OUTPUT_DEVICE = os.getenv("TTS_OUTPUT_DEVICE", "").strip() or _default_tts_device
# Open that device in WASAPI *exclusive* mode rather than shared.
#
# This exists to stop her voice reaching the Discord stream twice. Discord's
# "share stream audio" captures audio per-process, not per-endpoint, so it
# picks up the bot rendering speech regardless of which cable that speech is
# aimed at — once through the host account's mic (correct, everyone hears it) and
# again through the stream capture (wrong, viewers hear it doubled). A real
# microphone never has this problem because a mic signal is never rendered to
# a playback device at all; synthesized speech has to be.
#
# Exclusive mode bypasses the Windows shared mixer and talks to the driver
# directly, so there is typically nothing left in the path for that capture to
# tap. The cost is in the name: while the bot holds the device, nothing else
# on the machine can play to it. That is acceptable here only because
# the return cable exists for exactly one purpose — feeding that mic — and
# nothing else ever renders to it.
#
# MEASURED, AND IT DOES NOT WORK HERE. Kept off, and kept documented so the
# next person does not spend the evening rediscovering it:
#
#   * The driver is not the obstacle. A standalone process opens CABLE-B
#     Input exclusively at 44.1k and 48k, in float32/int16/int32, every time.
#   * Inside the bot the same open is refused with "Invalid device"
#     (PaErrorCode -9996), every time. The unproven suspicion is COM
#     apartment state on the worker thread Speaker.start() runs in; it was
#     not chased to ground.
#   * Attempting it is not free. One refused attempt still leaves shared
#     mode working, but repeated attempts leave the endpoint unusable by
#     anything for the life of the process — including the shared fallback,
#     which turns "heard twice on the stream" into "not heard at all".
#     Hence the single attempt in speech.py, which is not a tunable.
#
# So the doubling this was meant to fix is still there. The alternative, if
# it ever matters enough: point mpv at CABLE-B Input too and turn off Go
# Live's "share stream audio", accepting that Discord's mic processing will
# chew on music and film audio.
#
# Falls back to shared mode if the device refuses, because being audible in
# the channel matters more than being audible only once on the stream.
TTS_EXCLUSIVE = _bool("TTS_EXCLUSIVE", False)
# Exclusive mode is a WASAPI concept and there is no macOS equivalent, so it is
# forced off here rather than left to fail. sounddevice defines WasapiSettings
# on every platform and lets you construct one, so a Mac with TTS_EXCLUSIVE=true
# gets as far as opening the stream before CoreAudio refuses — which lands in
# the shared-mode fallback and logs "exclusive mode refused", implying the
# device said no when in fact the setting was never applicable.
if IS_MAC and TTS_EXCLUSIVE:
    TTS_EXCLUSIVE = False
# ~15 chars a second, so this is roughly 40 seconds of speech. Truncation
# prefers a sentence boundary, so a long reply is cut cleanly rather than
# mid-thought. Lower it if she starts monologuing over the room.
TTS_MAX_CHARS = _int("TTS_MAX_CHARS", 600)
TTS_TIMEOUT = _float("TTS_TIMEOUT", 60.0)
# Capture stays suppressed this long after playback ends. Discord never loops
# a mic back to its own speakers, so she cannot hear herself directly — but her
# voice can return through someone else's open mic, and the tail is still in
# the encoder after the device goes quiet.
TTS_ECHO_GUARD_MS = _int("TTS_ECHO_GUARD_MS", 400)

# A short pre-rendered "heard you" played the moment a wake word lands, before
# the real answer is worked out.
#
# The problem it solves is not the delay, it is the silence. Several seconds
# with no cue is indistinguishable from not having been heard at all, so people
# repeat themselves, which produces a second utterance and makes it worse. A
# cue costs nothing to play — these are rendered once and cached to disk, so
# the ack is a buffer write, not a synthesis — and it turns the wait into
# waiting rather than wondering.
#
# It also changes what a slow TTS engine costs. Chatterbox at ~8s a reply is
# unusable in silence and merely slow behind an ack, which is the difference
# between rejecting an engine and living with it.
TTS_ACK_ENABLED = _bool("TTS_ACK_ENABLED", True)

# Kept short and in character. "On it, one sec" is the obvious wording and the
# wrong one — she is meant to be faintly put upon, not eager. Comma separated;
# one is chosen at random per wake, never the same one twice running.
TTS_ACK_LINES = [
    s.strip() for s in (
        os.getenv("TTS_ACK_LINES", "").strip()
        or "Mm. || Fine. || One moment. || If I must. || Working on it."
    ).split("||") if s.strip()
]

# Where the rendered clips live. Deleting this directory is how you force a
# re-render after changing the voice or the lines.
TTS_ACK_DIR = os.getenv("TTS_ACK_DIR", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tts", "acks")

# --- Idle screen ---
# Image shown whenever nothing is playing, instead of mpv's own logo screen.
# Any image mpv can open. Blank to fall back to mpv's default.
IDLE_IMAGE = os.getenv("IDLE_IMAGE", "idle.png").strip()

# --- Now playing message ---
# A message in the channel that gets edited as playback changes. Updates in
# seconds, unlike the channel topic which Discord rate limits hard.
NOWPLAYING_ENABLED = _bool("NOWPLAYING_ENABLED", True)
NOWPLAYING_CHANNEL_ID = _int("NOWPLAYING_CHANNEL_ID") or ALLOWED_CHANNEL_ID

# How the bot identifies itself to Plex. A stable identifier means the server
# sees one consistent device rather than a new anonymous one each restart.
PLEX_DEVICE_NAME = os.getenv("PLEX_DEVICE_NAME", "").strip() or BOT_NAME
# The "plexbot-" seed below is deliberately NOT renamed to match the rebrand.
# It is the input to a uuid5, so changing it changes the derived client id, and
# Plex would register a second device alongside the existing one. The name above
# is what anyone actually sees; this string only has to stay constant.
PLEX_CLIENT_ID = (
    os.getenv("PLEX_CLIENT_ID", "").strip() or "plexbot-" + str(uuid.uuid5(
        uuid.NAMESPACE_DNS, f"plexbot-{os.getenv('COMPUTERNAME', 'host')}"
    ))[:18]
)

# --- Library filtering ---
# Libraries to ignore entirely. Matched case-insensitively as substrings, so
# A substring match, so "4K" hides any library whose name contains it.
# Comma separated. Blank excludes nothing.
EXCLUDE_LIBRARIES = [
    name.strip().lower()
    for name in os.getenv("EXCLUDE_LIBRARIES", "").split(",")
    if name.strip()
]

# --- Track languages ---
# Default for every library unless overridden below or changed at runtime.
DEFAULT_AUDIO_LANG = os.getenv("DEFAULT_AUDIO_LANG", "eng").strip() or "eng"
DEFAULT_SUBTITLE_LANG = os.getenv("DEFAULT_SUBTITLE_LANG", "off").strip() or "off"
# Libraries that should default to subbed (Japanese audio, English subtitles).
ANIME_LIBRARIES = [
    name.strip()
    for name in os.getenv("ANIME_LIBRARIES", "").split(",")
    if name.strip()
]
ANIME_AUDIO_LANG = os.getenv("ANIME_AUDIO_LANG", "jpn").strip() or "jpn"
ANIME_SUBTITLE_LANG = os.getenv("ANIME_SUBTITLE_LANG", "eng").strip() or "eng"
# Where runtime language choices get remembered.
PREFS_FILE = os.getenv("PREFS_FILE", "prefs.json").strip() or "prefs.json"
# Disk cache of library titles. Rebuilding it from Plex takes tens of seconds,
# so it is written to disk and revalidated in the background — otherwise every
# restart blocks for the length of a full scan. Delete the file to force a
# rebuild; nothing is lost by doing so.
TITLE_CACHE_FILE = os.getenv("TITLE_CACHE_FILE", ".title-cache.json").strip() or ".title-cache.json"

# --- Personality ---
# Optional flavor text folded into the system prompt. Leave blank for a plain
# assistant. This only affects tone/wording — the rules above it (act don't
# ask, never claim library facts without a tool call, etc) still apply.
BOT_PERSONA = os.getenv("BOT_PERSONA", "").strip()
# An optional second flavour of the same character, mixed in only some of the
# time. Small models treat "occasionally reference X" as "always reference X" —
# measured at 5 lines in 8 — so frequency is controlled here instead of asked
# for in the prompt. Put the mood/imagery here and the core voice above.
BOT_PERSONA_FLOURISH = os.getenv("BOT_PERSONA_FLOURISH", "").strip()
try:
    FLAVOR_FLOURISH_CHANCE = float(
        os.getenv("FLAVOR_FLOURISH_CHANCE", "").strip() or 0.35
    )
except ValueError:
    FLAVOR_FLOURISH_CHANCE = 0.35
# One short in-character aside appended to worthwhile results. Costs a model
# call (~1s), but only after the action has already happened, so the player
# responds instantly either way. Needs BOT_PERSONA and an NL backend.
FLAVOR_ENABLED = _bool("FLAVOR_ENABLED", True)
# How the aside is joined to the factual line. "inline" runs it on as part of
# the same sentence; "line" puts it underneath in italics, which is what this
# did originally and reads as a footnote in a second voice rather than the same
# person still talking. See flavor.attach.
FLAVOR_STYLE = os.getenv("FLAVOR_STYLE", "").strip().lower() or "inline"
# Give up on the aside rather than keep anyone waiting for a joke.
try:
    FLAVOR_TIMEOUT = float(os.getenv("FLAVOR_TIMEOUT", "").strip() or 5.0)
except ValueError:
    FLAVOR_TIMEOUT = 5.0

# --- Behaviour ---
# Auto-append the next episode when a show episode finishes and the queue is empty.
AUTOPLAY_NEXT_EPISODE = _bool("AUTOPLAY_NEXT_EPISODE", True)
# Seconds of no playback progress (while unpaused) before we call it frozen.
FREEZE_TIMEOUT = _int("FREEZE_TIMEOUT", 45) or 45
# Resume if the saved position is at least this many seconds in.
RESUME_MIN_SECONDS = 60
# ...and not past this fraction of the runtime.
RESUME_MAX_FRACTION = 0.95
# --- Image generation (ComfyUI, on another machine) ---
# Off until there is a server to talk to, so a fresh checkout does not offer a
# feature that cannot work. See imagegen.py and workflows/README.md.
IMAGE_ENABLED = _bool("IMAGE_ENABLED", False)
# ComfyUI's HTTP address. It must be reachable from this machine, which means
# ComfyUI has to be started with --listen; it binds 127.0.0.1 by default.
IMAGE_URL = os.getenv("IMAGE_URL", "http://127.0.0.1:8188").strip()
# Generous, because it is sized for the hardware rather than for patience: an
# 8GB card doing SDXL at 1024x1024 takes tens of seconds, and a quantised FLUX
# with offloading takes minutes.
IMAGE_TIMEOUT = _float("IMAGE_TIMEOUT", 180.0)
# A ComfyUI graph in API format. Relative paths resolve next to this file.
IMAGE_WORKFLOW = os.getenv("IMAGE_WORKFLOW", "workflows/sdxl.json").strip()
# Blank keeps whatever the workflow was exported with, which is the right
# default: the checkpoint that exists on the server is not knowable from here.
IMAGE_CHECKPOINT = os.getenv("IMAGE_CHECKPOINT", "").strip()
IMAGE_STEPS = _int("IMAGE_STEPS", 25) or 25
IMAGE_CFG = _float("IMAGE_CFG", 7.0)
IMAGE_WIDTH = _int("IMAGE_WIDTH", 1024) or 1024
IMAGE_HEIGHT = _int("IMAGE_HEIGHT", 1024) or 1024
IMAGE_NEGATIVE = os.getenv(
    "IMAGE_NEGATIVE", "blurry, low quality, watermark, text, deformed"
).strip()
# Used instead of IMAGE_WORKFLOW when a picture comes with the request. The
# source image is what sets the size here, so IMAGE_WIDTH/HEIGHT do not apply.
IMAGE_WORKFLOW_IMG2IMG = os.getenv(
    "IMAGE_WORKFLOW_IMG2IMG", "workflows/sdxl-img2img.json").strip()
# How far an edit may travel from the picture it started with. 1.0 discards the
# original entirely; below about 0.4 the prompt has no room to change anything.
# 0.65 restyles while keeping the composition, which is what "make this guy
# into Superman" means. Local edits that must not touch the rest of the frame
# — "give him corn rows" — need inpainting, not a lower number here.
IMAGE_DENOISE = _float("IMAGE_DENOISE", 0.65)
# Level her voice to a normal speaking level before it goes down the cable.
# Kokoro delivers about -23 dBFS RMS; broadcast speech sits near -18, and the
# quiet end of Kokoro's range is where Discord's voice gate starts cutting
# syllables. Never clips: a -1 dBFS peak ceiling always overrides the target.
TTS_NORMALIZE = _bool("TTS_NORMALIZE", True)
TTS_LEVEL_DBFS = _float("TTS_LEVEL_DBFS", -18.0)
# Ceiling on a conversational reply, in tokens. These get SPOKEN, so length is
# a listening cost rather than a scrolling one: 260 is roughly thirty seconds.
# The persona already asks for "a few lines"; this is what enforces it when a
# model at temperature 0.9 ignores that.
CHAT_MAX_TOKENS = _int("CHAT_MAX_TOKENS", 160) or 160
