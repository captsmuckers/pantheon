# Working on Pantheon

A Discord media bot: Plex and mpv, Spotify, YouTube and live streams, voice
input through Whisper, speech out through Kokoro, image generation on a remote
GPU, and a web control panel. It runs on macOS, on a machine that stays on.

Machine-specific values — addresses, GPU identifiers, host names — are in
`CLAUDE.local.md`, which is gitignored. This file is public; keep it that way.

## Getting oriented

```
bot.py        Discord client, slash commands, message handling
brain.py      intent routing, tool calling, the conversation path
player.py     mpv, via its JSON IPC socket
speech.py     TTS client, playback, the output stream
voice.py      Whisper capture and wake-word detection
imagegen.py   ComfyUI client — a client, not an engine
config.py     every setting, read once at import
schema.py     the same settings described for the panel
gui/          the control panel (stdlib http.server, no framework)
tts/          the speech server, its own venv and Python version
```

Two interpreters on purpose: the bot runs 3.13, the speech server 3.12, because
kokoro caps below 3.13. `tts/.venv` is separate and not interchangeable.

## Before you commit

```bash
./.venv/bin/python tests/run_all.py
```

All suites must pass. The suite is fast and there is no CI, so it is the only
thing standing between a change and a machine somebody talks to.

## How to change things here

**Comments say why, not what.** Nearly every non-obvious line in this codebase
is non-obvious because something failed. Preserve that reasoning when you edit
around it — several comments exist specifically to stop a fix being reverted by
someone who thinks they see a tidier way.

**A test asserts the property that actually broke.** Not that the code runs.
When a bug is fixed, the test should fail if the bug returns, and its docstring
should say what the failure looked like in the wild.

**Measure before asserting.** Repeatedly in this project the obvious diagnosis
was wrong and a measurement settled it in minutes. Prefer a number.

## Things that will bite you

**mpv is the bot's child, and is reaped by pid on startup.** Restarting the bot
kills whatever is playing. Check `ps -o %cpu` on mpv before bouncing the bot: a
low figure is the idle image, a high one is somebody's film.

**Settings are read once, at import.** Changing `.env` does nothing until the
process that reads it restarts. `schema.py` records which service each setting
belongs to, and a test enforces that anything the bot reads restarts the bot —
`TTS_VOICE` said "tts" for a while and silently applied nothing.

**The audio devices have fixed roles.** Three virtual devices, and mixing them
up produces symptoms that look like anything but routing:

| device | role |
|---|---|
| BlackHole 2ch | Discord's **output** — she hears the room |
| BlackHole 16ch | Discord's **input** — the room hears her |
| BlackHole 64ch | inside a Multi-Output aggregate, for the desktop stream |

The aggregate device exists **in memory only** and does not survive a reboot;
a LaunchAgent rebuilds it at login. CoreAudio also reassigns device ids after
creating one, so always re-look-up by name before setting a default — the call
returns success and does nothing otherwise.

**Her voice and the desktop stream take different paths into Discord.** Music
goes through the screen share, which applies no processing. Her voice arrives
as *microphone* input and goes through noise suppression, AGC and echo
cancellation. "The music is fine but she sounds bad" is that asymmetry, not a
fault in the audio pipeline.

**The intent classifier sends image requests to CHAT**, and CHAT attaches no
tools, so she answers in character with no way to act — which reads as refusing
something she can do. Fixed with regexes ahead of the classifier
(`_DRAW_REQUEST`, `_EDIT_REQUEST`). If a new phrasing fails to draw, add it to
the regex and its test. Do **not** reword `INTENT_PROMPT`: its wording was
measured and two rewrites made it worse.

**A nudge to the model must be a user turn, not an addition to the system
prompt.** Measured against a genuinely stuck reply: in the system prompt it
changed nothing 4 times out of 4; the same words as the last thing the model
reads broke it 4 times out of 4.

## Settled — do not re-investigate

- **Chatterbox TTS** was benchmarked and rejected. Kokoro 0.19s per line
  against Chatterbox 7.13s, MPS working correctly in both. Keep Kokoro.
- **Going Live cannot be automated.** No Discord keybind, absent from the bot
  API, empty accessibility tree. The only route left is self-botting.
- **Temperatures need root** on Apple silicon. The panel is a network-listening
  server that also runs pip and launchctl; it does not get root to draw a gauge.
- **The orange microphone dot** cannot be disabled.
- **mpv registers no windows** with the macOS accessibility API. Window control
  goes through its own IPC; raising it uses AX at the process level.

## Image generation

`imagegen.py` is a **client**. Generation happens on another machine running
ComfyUI, reached over HTTP, the same shape as the speech server. Nothing heavy
runs locally — diffusion alongside Whisper and Kokoro starves the CoreAudio
callbacks her voice depends on.

Workflows are ComfyUI graphs in **API format** and are patched **by structure,
not by node id**: `_patch` follows KSampler's own `positive` and `negative`
links, because node ids change on re-export and a graph with the prompt in the
wrong place still runs perfectly, just ignoring what was asked.

Two things the server does that look like success and are not: a **rejected
workflow returns HTTP 200 with a prompt_id** and `node_errors` as the only
signal, and **uploads are renamed on collision**, so the name to reference is
the one `/upload/image` answers with, never the one you sent.

The checkpoint matters more than any setting. `sd_xl_base_1.0` is the research
baseline and the weakest common option for anatomy and object coherence.

## The control panel

Stdlib `http.server`, no framework, served on a port from `gui/prefs.json`. It
runs pip, git and launchctl, so it deliberately has no privileges beyond the
user's. Sessions live in memory; restarting it signs everyone out.

It can update the repository, but only ever **fast-forward** — it refuses
rather than merging or discarding anything.
