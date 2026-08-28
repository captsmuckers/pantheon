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
# 'b' is British English. Using 'a' with a bf_* voice produces an American
# reading of a British-trained voice, which sounds subtly wrong rather than
# obviously broken.
LANG_CODE = "b"

MAX_CHARS = 600

_pipeline = None
_lock = threading.Lock()


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


def get_pipeline(device: str):
    """Load once, on first use. Guarded because HTTP handlers are threaded."""
    global _pipeline
    if _pipeline is None:
        with _lock:
            if _pipeline is None:
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
                _pipeline = KPipeline(lang_code=LANG_CODE, device=device)
                log.info("Ready")
    return _pipeline


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


def synthesize(text: str, voice: str, device: str) -> bytes:
    """Return a WAV. One inference at a time — the model is not thread-safe.

    On the Windows machine serialising also kept this off a GPU shared with
    Whisper and the language model. That second reason is gone on macOS, where
    Whisper is on the CPU, but thread-safety alone was always sufficient.
    """
    if ENGINE == "chatterbox":
        return _synth_chatterbox(text, device)
    pipeline = get_pipeline(device)
    with _lock:
        chunks = [audio for _, _, audio in pipeline(text, voice=voice, speed=1.0)]
    if not chunks:
        return b""
    audio = np.concatenate([c.detach().cpu().numpy() for c in chunks])

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
        loaded = _cb_model is not None if ENGINE == "chatterbox" else _pipeline is not None
        self._json(200, {"status": "ok", "ready": loaded,
                         "voice": (VOICE_REF or DEFAULT_VOICE) if ENGINE == "chatterbox"
                                  else DEFAULT_VOICE,
                         "engine": ENGINE, "device": self.device})

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
        if not text:
            self._json(400, {"error": "no text"})
            return

        try:
            wav = synthesize(text, voice, self.device)
        except Exception:
            log.exception("Synthesis failed")
            self._json(500, {"error": "synthesis failed"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)


def main():
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
    ap.add_argument("--engine", default="kokoro", choices=("kokoro", "chatterbox"),
                    help="kokoro is fast and mispronounces proper nouns; "
                         "chatterbox is ~35x slower and does not")
    ap.add_argument("--voice-ref", default="",
                    help="chatterbox only: a few seconds of the voice to clone")
    ap.add_argument("--preload", action="store_true",
                    help="load the model at startup instead of on first request")
    args = ap.parse_args()

    global ENGINE, VOICE_REF
    ENGINE = args.engine
    VOICE_REF = args.voice_ref
    Handler.device = args.device
    log.info("Engine: %s%s", ENGINE, f" (voice ref: {VOICE_REF})" if VOICE_REF else "")
    if args.preload:
        # Preload whichever engine is actually serving. This called
        # get_pipeline unconditionally, which imports kokoro — and kokoro is
        # not installed in the Chatterbox venv, by design, since the two pin
        # incompatible torch versions. So --engine chatterbox --preload died on
        # ModuleNotFoundError before it ever bound a port.
        if ENGINE == "chatterbox":
            _chatterbox(args.device)
        else:
            get_pipeline(args.device)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("Listening on http://%s:%d  (device=%s)", args.host, args.port, args.device)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping")


if __name__ == "__main__":
    main()
