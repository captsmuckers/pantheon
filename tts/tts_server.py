"""Text to speech for Athena, as a standalone localhost service.

Still a separate process with its own interpreter on macOS, but only one of the
three reasons that forced it on Windows survives intact — and the one that
still pins the Python version is a different problem wearing the same clothes:

  * **Python version — still applies, for a new reason.** On Windows this had
    to be 3.10 because every GPU TTS path was blocked on the bot's 3.14 for
    want of wheels. That was a CUDA-and-Windows problem and it is gone. What
    replaces it: kokoro declares Requires-Python <3.13 on every version it has
    ever shipped, as does misaki beneath it, and the bot runs 3.13. So this
    needs its own 3.12 environment regardless of anything to do with GPUs.
    Worth knowing when diagnosing it, because pip does not say this plainly —
    it silently filters the incompatible versions out and then reports what is
    left, which reads as "that version was never published".
  * **CUDA isolation — no longer applies.** There is no CUDA here, and
    CTranslate2 has no Metal backend, so faster-whisper runs on the CPU and
    cannot be disturbed by whatever torch does with the GPU.
  * **Restarts — still applies.** Model load takes seconds and the bot's
    gateway connection must not be dropped to reload a voice.

The contract is deliberately tiny, so the engine can be swapped without the
bot noticing:

    GET  /health      -> {"status": "ok", "ready": true, "voice": "bf_emma"}
    POST /synthesize  -> {"text": "...", "voice": "bf_emma"}  =>  audio/wav

Binds 127.0.0.1 only. There is no authentication and it must not be reachable
from the network.

    tts/.venv/bin/python tts_server.py
"""

import argparse
import glob
import html
import io
import json
import logging
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tts")

# Kokoro emits 24kHz mono. Discord wants 48kHz; the bot's player resamples, so
# this stays at the model's native rate rather than guessing at the far end.
SAMPLE_RATE = 24000

# British female. Chosen by measurement rather than taste: Athena scored 26/28
# for wake-word recognition against every alternative tried, and bf_emma is the
# British voice whose register suits a terse, unimpressed character.
DEFAULT_VOICE = "bf_emma"
# Which engine this process is serving, and — for Chatterbox — the reference
# clip its voice is cloned from. Set from the command line in main().
ENGINE = "kokoro"
VOICE_REF = ""
# Qwen3-TTS only. The transcript of VOICE_REF: the Base model is documented to
# take the reference clip AND what is said in it, and omitting this clones
# audibly worse while still producing perfectly plausible audio — so nothing
# fails, it just quietly sounds less like the reference.
VOICE_REF_TEXT = ""
# Qwen3-TTS VoiceDesign only: the voice described in words, e.g. "a low, dry,
# aristocratic English woman, bored and faintly contemptuous".
VOICE_DESIGN = ""
# Which Qwen checkpoint to serve. The repo id is also the mode switch — Qwen
# publishes Base (clone from a clip), CustomVoice (9 fixed timbres) and
# VoiceDesign (describe it in words) as SEPARATE checkpoints, so the mode is
# not a parameter you can flip at runtime; it decides which weights load.
QWEN_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"

# The 9 timbres CustomVoice ships. Hard-coded because they are baked into the
# weights rather than listed by any endpoint, and the settings page needs them
# to draw a picker. Only Ryan and Aiden are natively English and both are male;
# the female voices are Chinese-native reading English, which is usable but is
# the reason VoiceDesign exists as an alternative.
QWEN_SPEAKERS = {
    "Ryan":      ("English",  False, "Dynamic male, strong rhythmic drive"),
    "Aiden":     ("English",  False, "Sunny American male, clear midrange"),
    "Vivian":    ("Chinese",  True,  "Bright, slightly edgy young female"),
    "Serena":    ("Chinese",  True,  "Warm, gentle young female"),
    "Uncle_Fu":  ("Chinese",  False, "Seasoned male, low mellow timbre"),
    "Dylan":     ("Chinese",  False, "Youthful Beijing male, clear and natural"),
    "Eric":      ("Chinese",  False, "Lively Chengdu male, slightly husky"),
    "Ono_Anna":  ("Japanese", True,  "Playful female, light nimble timbre"),
    "Sohee":     ("Korean",   True,  "Warm female, rich emotion"),
}
# Which phonemiser turns spelling into sound. This is NOT the voice: the voice
# file decides who it sounds like, this decides how words are pronounced. They
# are chosen separately and Kokoro's convention is that the voice name's first
# letter IS its language code — af_heart is American, bf_emma is British.
#
# It used to be hardcoded to "b", which meant every voice was read with British
# rules no matter which was picked. Measured rather than assumed: the same
# voice and text under 'a' and 'b' produce completely different audio
# (correlation +0.007), differing on schedule, either, tomato, herb, and every
# word ending in -er, since British English drops the trailing r. So 46 of the
# 54 published voices were being mispronounced.
#
# "auto" derives the code from the voice, which is right by default. An
# explicit code overrides it, because deliberately reading an American voice
# with British rules is a legitimate thing to want and used to be the only
# option available.
LANG_CODE = "auto"

# Every language Kokoro publishes voices for. Five work out of the box; two
# need extra packages, and are listed here rather than hidden so the settings
# page can say WHY they are unavailable and offer to fix it. Hiding them just
# turns "needs one package" into "seems unsupported".
LANGUAGES = {
    "a": "American English", "b": "British English", "e": "Spanish",
    "f": "French", "h": "Hindi", "i": "Italian", "j": "Japanese",
    "p": "Brazilian Portuguese", "z": "Mandarin Chinese",
}

# The two whose phonemiser is not installed by default. `probe` is a module
# whose presence means the phonemiser will import; `pip` is what installs it.
# Checked by find_spec rather than by importing, because importing pyopenjtalk
# costs real time and this runs on every /health.
EXTRAS = {
    "j": {"pip": "misaki[ja]", "probe": "pyopenjtalk"},
    "z": {"pip": "misaki[zh]", "probe": "ordered_set"},
}


def language_available(code: str) -> bool:
    """Whether this code's phonemiser can be constructed right now."""
    extra = EXTRAS.get(code)
    if extra is None:
        return True
    import importlib.util
    try:
        return importlib.util.find_spec(extra["probe"]) is not None
    except (ImportError, ValueError):
        return False


# Where the voices are published, and the two pages worth linking a person to.
# Kokoro's own README does not list them; VOICES.md does, with quality grades,
# and SAMPLES.md has audio. Neither is easy to find from the model page.
VOICES_REPO = "hexgrad/Kokoro-82M"
VOICES_DOC = f"https://huggingface.co/{VOICES_REPO}/blob/main/VOICES.md"
SAMPLES_DOC = f"https://huggingface.co/{VOICES_REPO}/blob/main/SAMPLES.md"

_voices_cache = None


def qwen_voice_list() -> dict:
    """Qwen's timbres in the SAME shape Kokoro's list_voices() returns.

    Matching the existing payload exactly is the point: the settings page
    already groups by lang_name, draws a chip per id and marks downloaded ones,
    and reusing that costs nothing. Returning a different shape here made the
    picker report "Voice list unavailable", because paintVoices tests d.count
    and I had not sent one.

    Only CustomVoice has timbres. The other two modes return count 0 with a
    message, which the picker already renders instead of an empty grid.
    """
    mode = qwen_mode()
    if mode != "customvoice":
        why = ("This checkpoint designs a voice from your description - "
               "type one in Voice design, then press Test."
               if mode == "voicedesign" else
               "This checkpoint clones the clip in Voice reference.")
        return {"voices": [], "count": 0, "source": "built-in",
                "message": why, "mode": mode, "model": QWEN_MODEL,
                "doc": "https://huggingface.co/" + QWEN_MODEL}
    out = [{"id": name, "lang": lang[:1].lower(), "lang_name": lang,
            "female": female, "downloaded": True, "note": note}
           for name, (lang, female, note) in QWEN_SPEAKERS.items()]
    return {"voices": out, "count": len(out), "source": "built-in",
            "mode": mode, "model": QWEN_MODEL,
            "doc": "https://huggingface.co/" + QWEN_MODEL,
            "samples": "https://huggingface.co/" + QWEN_MODEL}


def list_voices() -> dict:
    """Every published voice, with whether it is already downloaded.

    Served from here rather than looked up by the settings page, because this
    is the process that knows which ones are on disk — and the panel has no
    huggingface_hub to ask with.

    Falls back to whatever is already cached locally when the Hub cannot be
    reached, so the picker still works offline rather than showing nothing.
    """
    global _voices_cache
    if _voices_cache is not None:
        return _voices_cache

    names, source = [], "hub"
    try:
        from huggingface_hub import HfApi
        names = sorted(f.split("/")[-1][:-3]
                       for f in HfApi().list_repo_files(VOICES_REPO)
                       if f.startswith("voices/") and f.endswith(".pt"))
    except Exception as exc:
        log.warning("Could not list voices from the Hub (%s); using local cache",
                    type(exc).__name__)
        source = "local"

    local = _downloaded_voices()
    if not names:
        names = sorted(local)

    out = []
    for name in names:
        code = name[:1]
        out.append({"id": name,
                    "lang": code,
                    "lang_name": LANGUAGES.get(code, "Unknown"),
                    "female": name[1:2] == "f",
                    "downloaded": name in local})
    _voices_cache = {"voices": out, "source": source, "count": len(out),
                     "doc": VOICES_DOC, "samples": SAMPLES_DOC,
                     "repo": f"https://huggingface.co/{VOICES_REPO}"}
    return _voices_cache


def _downloaded_voices() -> set:
    """Voice files already in the local HuggingFace cache."""
    root = os.path.expanduser("~/.cache/huggingface/hub")
    found = set()
    for base, _dirs, files in os.walk(root):
        if os.path.basename(base) != "voices":
            continue
        found |= {f[:-3] for f in files if f.endswith(".pt")}
    return found


def language_report() -> dict:
    """What the settings page needs to render the language picker."""
    return {code: {"name": name,
                   "available": language_available(code),
                   "needs": EXTRAS.get(code, {}).get("pip", "")}
            for code, name in LANGUAGES.items()}

MAX_CHARS = 600

# One model, many phonemisers. KPipeline accepts a shared KModel, so the 82M
# weights are loaded once (~700 MB) and each additional language costs about a
# second and 50 MB. That is what makes previewing a voice in another accent
# affordable at request time rather than a restart.
_model = None
_pipelines = {}
_lock = threading.Lock()


def lang_for(voice: str) -> str:
    """The language code to phonemise `voice` with."""
    if LANG_CODE != "auto":
        return LANG_CODE
    first = (voice or DEFAULT_VOICE)[:1]
    return first if first in LANGUAGES else "b"


# ----------------------------------------------------------------------
# log viewing
# ----------------------------------------------------------------------

# This process already runs an HTTP server and sits next to the bot's logs, so
# it doubles as somewhere to read them from a browser rather than tailing files
# over a remote desktop session. Read-only, and bound to loopback like the rest.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BOT_LOGS = os.path.join(os.path.dirname(_HERE), "logs")

LOGS = {
    "voice": ("Voice tuning — what she heard and what she decided",
              os.path.join(_BOT_LOGS, "voice-tuning.log")),
    "bot": ("Bot — newest run", None),          # resolved at request time
    "tts": ("This service", os.path.join(_HERE, "tts.log")),
}


def resolve_log(name: str) -> str | None:
    """Path for a log key. The bot's newest run is found fresh each time.

    Each bot start writes its own file, so a fixed path would go stale the
    moment it restarts — which it does often during a tuning session.
    """
    if name not in LOGS:
        return None
    if name == "bot":
        matches = sorted(glob.glob(os.path.join(_BOT_LOGS, "athena-*.log")),
                         key=os.path.getmtime, reverse=True)
        return matches[0] if matches else None
    return LOGS[name][1]


def tail(path: str, lines: int) -> str:
    """Last N lines, tolerating a file being written to as we read it."""
    if not path or not os.path.exists(path):
        return "(no such log yet)"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            # 400 bytes a line is generous for these logs; cap the read so a
            # multi-megabyte file does not get slurped whole.
            block = min(size, max(8192, lines * 400))
            fh.seek(size - block)
            data = fh.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError as exc:
        return f"(could not read: {exc})"


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Athena — {title}</title>
<meta http-equiv="refresh" content="{refresh}">
<style>
 body {{ background:#14161f; color:#e8eaf0; font:13px/1.55 Cascadia Mono,Consolas,monospace;
        margin:0; padding:16px 20px; }}
 nav {{ margin-bottom:14px; display:flex; gap:14px; align-items:baseline; flex-wrap:wrap; }}
 a {{ color:#74a6c9; text-decoration:none; }}
 a.on {{ color:#e8952f; font-weight:600; }}
 a:hover {{ text-decoration:underline; }}
 h1 {{ font-size:14px; margin:0 14px 0 0; color:#a9afc0; font-weight:600; }}
 .meta {{ color:#787e92; margin-left:auto; font-size:12px; }}
 pre {{ white-space:pre-wrap; word-break:break-word; margin:0;
        border-top:1px solid #2e3244; padding-top:12px; }}
 .wake {{ color:#56a88a; }}
 .none {{ color:#787e92; }}
 .err  {{ color:#d4697f; }}
</style>
<nav>
 <h1>Athena logs</h1>
 {links}
 <span class="meta">{lines} lines · refreshing every {refresh}s · {path}</span>
</nav>
<pre>{body}</pre>
"""


def colourise(text: str) -> str:
    """Mark up the tuning log's verdicts so a wake stands out from the noise."""
    out = []
    for line in html.escape(text).splitlines():
        cls = ""
        if "| WAKE" in line:
            cls = "wake"
        elif "NO-WAKE" in line or "NOISE" in line or "hallucination" in line:
            cls = "none"
        elif "ERROR" in line or "Traceback" in line or "FAILED" in line:
            cls = "err"
        out.append(f'<span class="{cls}">{line}</span>' if cls else line)
    return "\n".join(out)


def get_pipeline(device: str, lang: str = None):
    """Load once per language, on first use. Guarded: handlers are threaded."""
    global _model
    lang = lang or lang_for(DEFAULT_VOICE)
    if lang not in _pipelines:
        with _lock:
            if lang not in _pipelines:
                import torch
                from kokoro import KPipeline

                if device == "cuda":
                    if not torch.cuda.is_available():
                        raise RuntimeError(
                            "cuda requested but torch reports it unavailable")
                    log.info("Loading Kokoro on %s", torch.cuda.get_device_name(0))
                elif device == "mps":
                    # Apple silicon's GPU. Checked rather than assumed: an
                    # Intel Mac, or a torch installed from the wrong index,
                    # reports it unavailable and would otherwise fail deep
                    # inside the first inference instead of here at load.
                    if not torch.backends.mps.is_available():
                        raise RuntimeError(
                            "mps requested but torch reports it unavailable — "
                            "this needs Apple silicon and a torch built with "
                            "MPS support")
                    log.info("Loading Kokoro on the Apple silicon GPU (MPS)")
                else:
                    log.info("Loading Kokoro on CPU")
                if _model is None:
                    from kokoro import KModel
                    _model = KModel().to(device).eval()
                _pipelines[lang] = KPipeline(lang_code=lang, model=_model,
                                             device=device)
                log.info("Ready (%s)", LANGUAGES.get(lang, lang))
    return _pipelines[lang]


# ----------------------------------------------------------------------
# Chatterbox, as an alternative engine behind the same contract
# ----------------------------------------------------------------------
#
# Kokoro is 82M parameters and phonemises through misaki, a rule-based
# grapheme-to-phoneme layer. Rule-based G2P is exactly what mangles proper
# nouns, and this bot says band names and film titles constantly — "Deftones",
# "Fetty Wap", "Evangelion". Chatterbox reads the text through a language-model
# backbone instead and gets them noticeably right.
#
# It costs about 35x the latency (measured: 7.0-7.9s per line against Kokoro's
# ~0.2s), which is why this is a switch rather than a replacement. Whether that
# is acceptable is a judgement about the room, not about the code: the text
# reply posts immediately either way, so the delay is only before she speaks.
#
# The two cannot share a virtualenv — Chatterbox pins torch 2.6 and Kokoro runs
# on 2.13 — so this file is run from tts/.venv-chatterbox when --engine
# chatterbox is used, and from tts/.venv otherwise. The bot never learns which:
# it only knows /health and /synthesize, which is what that contract was for.
_cb_model = None


def _chatterbox(device: str):
    global _cb_model
    if _cb_model is None:
        with _lock:
            if _cb_model is None:
                import torch
                from chatterbox.tts import ChatterboxTTS

                if device == "mps" and not torch.backends.mps.is_available():
                    raise RuntimeError("mps requested but torch reports it unavailable")
                log.info("Loading Chatterbox on %s", device)
                _cb_model = ChatterboxTTS.from_pretrained(device=device)
                log.info("Ready")
    return _cb_model


def _synth_chatterbox(text: str, device: str) -> bytes:
    """Chatterbox's voice comes from a reference clip, not a voice name.

    VOICE_REF is a few seconds of whoever she should sound like. Without one
    Chatterbox uses its own default, which is not the character wanted here.
    """
    import numpy as np

    model = _chatterbox(device)
    with _lock:
        kwargs = {"audio_prompt_path": VOICE_REF} if VOICE_REF else {}
        wav = model.generate(text, **kwargs)
    audio = wav.detach().cpu().numpy().squeeze()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        # Chatterbox is 24kHz like Kokoro, but ask the model rather than assume:
        # the bot resamples from whatever this says, so a wrong number here is a
        # chipmunk rather than an error.
        w.setframerate(getattr(model, "sr", SAMPLE_RATE))
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _release_device_memory(device: str) -> None:
    """Return the accelerator's cached blocks. Never raises into a request.

    Two allocators, because the engines do not share a runtime. MLX keeps its
    own Metal buffer cache which — unlike torch's — does NOT show up in the
    process's RSS, so it is invisible to `ps` and to the control panel's
    process view. Measured on an M2 Max: a Qwen server sat at 3.0GB RSS while
    actually holding ~6.3GB of unified memory, the difference being this cache.
    MEASURED, and it is NOT a leak: the cache plateaus by itself after a few
    calls (11.18GB free after 5 syntheses, 11.30GB after 15). Clearing it made
    no difference to steady-state footprint under load either — 11.30GB vs
    11.52GB with and without, which is noise. It is kept because it costs
    nothing, mirrors what the Kokoro path already does, and does hand memory
    back during idle gaps between auditions; do NOT expect it to reduce the
    ~6.3GB a running Qwen server occupies.
    """
    try:
        if ENGINE == "qwen":
            import mlx.core as mx

            mx.clear_cache()
            return
        import torch

        if device == "mps" and hasattr(torch, "mps"):
            torch.mps.empty_cache()
        elif device == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # pragma: no cover - a cleanup step must not fail a reply
        log.debug("Could not release device memory", exc_info=True)


_qwen_model = None


def _qwen(device: str):
    """Load the Qwen3-TTS checkpoint. Device is ignored, deliberately.

    mlx-audio runs on MLX, which is always the Apple-silicon GPU — there is no
    cpu/mps choice to make. Measured on an M2 Max: MLX is ~2.2-2.6x faster than
    the same model under PyTorch MPS, which is why this engine exists at all.
    """
    global _qwen_model
    if _qwen_model is None:
        with _lock:
            if _qwen_model is None:
                from mlx_audio.tts.utils import load_model

                log.info("Loading Qwen3-TTS %s", QWEN_MODEL)
                _qwen_model = load_model(QWEN_MODEL)
                log.info("Ready")
    return _qwen_model


def qwen_mode(model_id: str = "") -> str:
    """base | customvoice | voicedesign, read off the checkpoint name.

    Inferred rather than configured separately so the two can never disagree.
    A mode setting that says "customvoice" against a Base checkpoint would be
    accepted by every layer here and then silently ignore the speaker name.
    """
    name = (model_id or QWEN_MODEL).lower()
    if "customvoice" in name:
        return "customvoice"
    if "voicedesign" in name:
        return "voicedesign"
    return "base"


def _synth_qwen(text: str, voice: str, instruct: str) -> bytes:
    """One WAV from Qwen3-TTS, in whichever mode the checkpoint implies.

    generate() yields CHUNKS, not one waveform — returning the first one only
    gives a clipped first sentence, which reads as the model truncating rather
    than as a bug here.
    """
    model = _qwen(None)
    mode = qwen_mode()
    kwargs = {}
    if mode == "customvoice":
        kwargs["voice"] = voice or DEFAULT_VOICE
    elif mode == "voicedesign":
        kwargs["instruct"] = instruct or VOICE_DESIGN
    elif VOICE_REF:
        kwargs["ref_audio"] = VOICE_REF
        if VOICE_REF_TEXT:
            kwargs["ref_text"] = VOICE_REF_TEXT

    with _lock:
        chunks, sr = [], None
        for result in model.generate(text=text, **kwargs):
            chunks.append(np.asarray(result.audio, dtype=np.float32).squeeze())
            sr = getattr(result, "sample_rate", None) or sr
    if not chunks:
        return b""
    audio = np.concatenate(chunks)
    # Same reasoning as the Kokoro path below, different allocator. Done after
    # the copy into numpy so nothing still needed is thrown away.
    del chunks
    _release_device_memory(None)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr or SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def synthesize(text: str, voice: str, device: str, lang: str = None,
               instruct: str = "") -> bytes:
    """Return a WAV. One inference at a time — the model is not thread-safe.

    On the Windows machine serialising also kept this off a GPU shared with
    Whisper and the language model. That second reason is gone on macOS, where
    Whisper is on the CPU, but thread-safety alone was always sufficient.
    """
    if ENGINE == "chatterbox":
        return _synth_chatterbox(text, device)
    if ENGINE == "qwen":
        return _synth_qwen(text, voice, instruct)
    pipeline = get_pipeline(device, lang or lang_for(voice))
    with _lock:
        chunks = [audio for _, _, audio in pipeline(text, voice=voice, speed=1.0)]
        if not chunks:
            return b""
        audio = np.concatenate([c.detach().cpu().numpy() for c in chunks])
        # Hand the intermediate tensors back before releasing the lock.
        # Measured on this machine: without it the server grew 748MB over 20
        # requests — about 37MB a line, never returned — because torch's MPS
        # allocator keeps freed blocks in its own cache, and on Apple silicon
        # that cache is unified memory, so it shows up as the process simply
        # getting bigger until the machine is rebooted.
        del chunks
        _release_device_memory(device)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    device = "mps" if sys.platform == "darwin" else "cuda"

    def log_message(self, fmt, *args):  # quieter than the default
        log.debug(fmt, *args)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_logs(self, query: dict) -> None:
        name = (query.get("log") or ["voice"])[0]
        if name not in LOGS:
            name = "voice"
        try:
            lines = max(20, min(2000, int((query.get("lines") or ["200"])[0])))
        except ValueError:
            lines = 200
        try:
            refresh = max(0, min(60, int((query.get("refresh") or ["5"])[0])))
        except ValueError:
            refresh = 5

        path = resolve_log(name)
        links = " ".join(
            f'<a class="{"on" if key == name else ""}" '
            f'href="/logs?log={key}&lines={lines}&refresh={refresh}">{label.split(" — ")[0]}</a>'
            for key, (label, _) in LOGS.items()
        )
        self._html(PAGE.format(
            title=LOGS[name][0],
            links=links,
            lines=lines,
            refresh=refresh or 3600,
            path=html.escape(os.path.basename(path) if path else "—"),
            body=colourise(tail(path, lines)),
        ))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/voices":
            # Listed here because this process is the one that knows which are
            # on disk, and the settings page has no huggingface_hub to ask.
            if ENGINE == "qwen":
                self._json(200, qwen_voice_list())
                return
            self._json(200, list_voices())
            return
        if parsed.path in ("/", "/logs"):
            self._serve_logs(parse_qs(parsed.query))
            return
        if parsed.path != "/health":
            self._json(404, {"error": "not found"})
            return
        # Reports whether the model is already resident, without loading it —
        # the bot health-checks this to decide whether to say "TTS is warming
        # up" rather than hanging on a cold start.
        # Ready means "the engine actually serving is loaded". This tested
        # _pipeline alone, which is Kokoro's global and stays None forever under
        # --engine chatterbox — so /health reported ready:false no matter how
        # loaded the model was, and launchd-athena.sh, which waits on exactly
        # this before starting the bot, would have burned its full 90s timeout
        # on every single boot.
        # Each engine keeps its loaded model in its own global, so this has to
        # branch per engine. Getting it wrong is not cosmetic: launchd-athena.sh
        # blocks on ready:true before starting the bot, so an engine missing
        # from here burns that full 90s timeout on every boot.
        if ENGINE == "chatterbox":
            loaded = _cb_model is not None
        elif ENGINE == "qwen":
            loaded = _qwen_model is not None
        else:
            loaded = bool(_pipelines)
        lang = lang_for(DEFAULT_VOICE)
        self._json(200, {"status": "ok", "ready": loaded,
                         "voice": (VOICE_REF or DEFAULT_VOICE) if ENGINE == "chatterbox"
                                  else DEFAULT_VOICE,
                         "engine": ENGINE, "device": self.device,
                         **({"qwen_model": QWEN_MODEL, "qwen_mode": qwen_mode(),
                             "voice_design": VOICE_DESIGN} if ENGINE == "qwen" else {}),
                         "lang": lang, "lang_setting": LANG_CODE,
                         "lang_name": LANGUAGES.get(lang, lang),
                         "languages": language_report()})

    def do_POST(self):
        if self.path != "/synthesize":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._json(400, {"error": f"bad request: {exc}"})
            return

        text = (payload.get("text") or "").strip()[:MAX_CHARS]
        voice = payload.get("voice") or DEFAULT_VOICE
        lang = payload.get("lang") or None
        # Per-request so a voice can be auditioned without restarting the
        # service. Loading a Qwen checkpoint takes ~20-33s, and having to pay
        # that to hear one adjective changed makes the feature unusable.
        instruct = (payload.get("instruct") or "").strip()
        if not text:
            self._json(400, {"error": "no text"})
            return
        # Kokoro's phonemiser codes. Qwen and Chatterbox have no equivalent, so
        # a lang on those requests is meaningless rather than invalid — reject
        # it only for the engine it actually means something to.
        if ENGINE == "kokoro" and lang and lang != "auto" and lang not in LANGUAGES:
            self._json(400, {"error": f"unknown language {lang!r}"})
            return
        if ENGINE == "kokoro" and lang and lang != "auto" and not language_available(lang):
            self._json(409, {"error": f"{LANGUAGES[lang]} needs an extra package",
                             "lang": lang, "needs": EXTRAS[lang]["pip"]})
            return
        if lang == "auto":
            lang = None

        try:
            wav = synthesize(text, voice, self.device, lang, instruct)
        except Exception as exc:
            log.exception("Synthesis failed")
            # A voice name that does not exist surfaces as a 404 from the
            # HuggingFace download, several frames down. Reported plainly,
            # because "synthesis failed" for a typo sends you looking at the
            # model, the device and the audio path before the spelling.
            blob = f"{type(exc).__name__}: {exc}"
            if "404" in blob or "EntryNotFound" in blob:
                self._json(404, {"error": f"No such voice: {voice!r}. Voice "
                                          "names look like bf_emma or af_heart.",
                                 "voice": voice})
            else:
                self._json(500, {"error": "synthesis failed", "detail": blob[:200]})
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)


def main():
    # Declared up front: argparse reads DEFAULT_VOICE below as a default value,
    # and Python rejects a global statement that comes after the name is used.
    global ENGINE, VOICE_REF, LANG_CODE, DEFAULT_VOICE
    global VOICE_REF_TEXT, VOICE_DESIGN, QWEN_MODEL
    ap = argparse.ArgumentParser()
    # 127.0.0.1 by default. Pass 0.0.0.0 to reach the log page from another
    # machine — a firewall rule alone cannot do it, because loopback means
    # nothing is listening on the network interface to allow traffic to.
    #
    # Understand what that opens: /synthesize takes text and speaks it into the
    # Discord channel, with no authentication. On a trusted LAN that is a
    # nuisance at worst; do not put it anywhere reachable from outside.
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8085)
    # mps is Apple silicon's GPU, cuda is NVIDIA's; the default follows the
    # platform so neither machine needs the flag.
    ap.add_argument("--device", default=Handler.device,
                    choices=("cuda", "mps", "cpu"))
    ap.add_argument("--engine", default="kokoro",
                    choices=("kokoro", "chatterbox", "qwen"),
                    help="kokoro is fast and mispronounces proper nouns; "
                         "chatterbox is ~35x slower and does not")
    ap.add_argument("--qwen-model", default=QWEN_MODEL,
                    help="qwen only: which checkpoint to serve. The repo name "
                         "also selects the mode - Base clones from --voice-ref, "
                         "CustomVoice uses --voice, VoiceDesign uses "
                         "--voice-design.")
    # Defaulted from the environment, not just the flag. scripts/_common.sh
    # exports these rather than passing them as arguments, because the
    # launchers word-split tts_args' output and a voice description is a whole
    # sentence. Reading them here is the other half of that mechanism — without
    # it the export went nowhere and any /synthesize call that did not carry
    # its own instruct (the panel's Test button, the test suite) failed with
    # "VoiceDesign model requires...", while the bot worked, because the bot
    # sends the description on every request.
    ap.add_argument("--ref-text", default=os.environ.get("ATHENA_TTS_REF_TEXT", ""),
                    help="qwen Base only: what is said in --voice-ref. The "
                         "model takes both; without it the clone is worse but "
                         "nothing errors.")
    ap.add_argument("--voice-design",
                    default=os.environ.get("ATHENA_TTS_VOICE_DESIGN", ""),
                    help="qwen VoiceDesign only: the voice described in words.")
    ap.add_argument("--voice-ref", default="",
                    help="chatterbox only: a few seconds of the voice to clone")
    ap.add_argument("--voice", default=DEFAULT_VOICE,
                    help="the voice the bot is configured to use. Only affects "
                         "which phonemiser is preloaded and what /health "
                         "reports; the bot names a voice on every request.")
    ap.add_argument("--lang", default="auto",
                    help="phonemiser language: auto (derive from the voice "
                         "name's first letter) or one of "
                         + ", ".join(sorted(LANGUAGES)))
    ap.add_argument("--preload", action="store_true",
                    help="load the model at startup instead of on first request")
    args = ap.parse_args()

    if args.engine == "kokoro" and args.lang != "auto" and args.lang not in LANGUAGES:
        ap.error(f"--lang {args.lang!r} is not a language Kokoro publishes "
                 "voices for; choose auto or one of "
                 + ", ".join(f"{k} ({v})" for k, v in sorted(LANGUAGES.items())))
    if args.engine == "kokoro" and args.lang != "auto" and not language_available(args.lang):
        # Refused at startup rather than on the first spoken line: a speech
        # service that starts happily and then throws the first time she tries
        # to talk is far worse than one that will not start.
        ap.error(f"--lang {args.lang} ({LANGUAGES[args.lang]}) needs a package "
                 f"that is not installed. Install it with:\n"
                 f"  tts/.venv/bin/python -m pip install '{EXTRAS[args.lang]['pip']}'")
    LANG_CODE = args.lang
    DEFAULT_VOICE = args.voice
    ENGINE = args.engine
    VOICE_REF = args.voice_ref
    VOICE_REF_TEXT = args.ref_text
    VOICE_DESIGN = args.voice_design
    QWEN_MODEL = args.qwen_model
    Handler.device = args.device
    if ENGINE == "qwen":
        log.info("Engine: qwen %s (mode: %s)", QWEN_MODEL, qwen_mode())
    else:
        log.info("Engine: %s%s", ENGINE,
                 f" (voice ref: {VOICE_REF})" if VOICE_REF else "")
    if args.preload:
        # Preload whichever engine is actually serving. This called
        # get_pipeline unconditionally, which imports kokoro — and kokoro is
        # not installed in the Chatterbox venv, by design, since the two pin
        # incompatible torch versions. So --engine chatterbox --preload died on
        # ModuleNotFoundError before it ever bound a port.
        if ENGINE == "chatterbox":
            _chatterbox(args.device)
        elif ENGINE == "qwen":
            _qwen(args.device)
        else:
            get_pipeline(args.device, lang_for(DEFAULT_VOICE))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("Listening on http://%s:%d  (device=%s)", args.host, args.port, args.device)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping")


if __name__ == "__main__":
    main()
