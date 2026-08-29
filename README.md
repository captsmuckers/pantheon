# Pantheon

A Discord bot that runs the media in a room. Ask it in plain English — by
typing, or out loud — and it plays Plex films and shows through mpv, music
through Spotify, YouTube and live streams on request, karaoke lyrics over the
idle screen. It talks back, in a voice best described as unimpressed.

The bot is called **Athena** by default. You can rename her; Pantheon is the
project, she is the one who shows up.

```
put on the office s3e5
what's playing?
skip ahead 5 minutes
queue up dune part two after this
what did he say          → jumps back 15 seconds
turn the subs off
it's frozen              → relaunches the player
```

Plex is only a metadata and file source — mpv does the playing, which is why
subtitle changes are instant and a crashed player resumes where it stopped.

Requests are handled in tiers, so the language model sees as little as possible:

1. **`fast_match`** — deterministic regex routing for anything unambiguous
2. **`try_direct_play`** — library-first scored resolution, then Spotify
3. **the model** — only genuinely fuzzy requests ("something with dragons in it")

Most of what you say never reaches a model at all. That is deliberate: it is
faster, and it cannot hallucinate an action it never saw.

---

## What you need

**A Mac with Apple silicon.** Developed and run on an M1 Max. An M-series with
16GB will work; the defaults assume unified memory and a Metal-capable GPU, and
`config.py` explains each choice where it is made. Intel Macs are not tested.

**It must run in a logged-in GUI session.** The bot moves mpv and Spotify
windows around, and window control needs a real login session — a LaunchAgent
is fine, a LaunchDaemon is not. Close the lid with no external display and
everything stops.

**Accounts and services:**

| | for | required |
|---|---|---|
| Discord bot token | the bot itself | yes |
| Plex server + token | films and shows | yes |
| Ollama, running locally | fuzzy requests and conversation | recommended |
| Spotify developer app | music | optional |

Without Ollama the bot still handles anything unambiguous — `play X`, `pause`,
`back 30s` — and only genuinely fuzzy requests are lost. There is no cloud
fallback and no API key to buy; the model runs on your machine.

**Disk:** about 2GB for the bot, plus ~2GB more if you want voice input and
speech.

---

## Quick start

```bash
git clone https://github.com/captsmuckers/pantheon.git
cd pantheon
brew install python@3.13 mpv yt-dlp
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
scripts/start-gui.sh
```

Then open **<http://127.0.0.1:8086>**. It will land you on a setup page that
checks what is present, does what it can for you — creating the virtualenv,
copying the config, pulling a model — and gives you the exact command for the
few things it cannot, like the audio driver that needs your password.

In fact only the first two lines above are needed. The control panel requires
no virtualenv and runs on the Python macOS already ships, so you can clone,
start it, and let it do the rest:

```bash
git clone https://github.com/captsmuckers/pantheon.git
cd pantheon && scripts/start-gui.sh
```

The one thing no wizard can do for you is fetch your credentials. It will tell
you which are missing and link you to the page to enter them.

`.env.example` documents every setting if you would rather edit a file.

### Permissions macOS will ask for

Two **separate** grants in System Settings → Privacy & Security. Both are asked
for once and refused silently forever if the prompt is dismissed, so they are
worth granting deliberately:

- **Accessibility** — for whatever runs the bot (Terminal, iTerm, or the Python
  binary). Without it every window call fails and the bot reports "Couldn't
  find the Spotify window" for windows that are plainly there.
- **Automation** — for controlling Spotify. This is the one that gets missed,
  because the first prompt looks like it covered everything.

Screen Recording is a third grant, and it belongs to Discord rather than to the
bot.

### On the Discord side

The bot needs the **Message Content Intent** enabled (Developer Portal → Bot →
Privileged Gateway Intents), or it will connect and then ignore every message.
Add `Manage Channels` if you want it to keep a status channel's topic updated.

---

## The control panel

```bash
scripts/start-gui.sh          # then http://127.0.0.1:8086
```

Settings, service start/stop, and live logs. Ctrl-C stops it.

It deliberately depends on **nothing** — no web framework, no virtualenv, and
Python 3.9 (what macOS ships) is enough. That is not minimalism for its own
sake: this is the tool you open when the bot will not start, and a broken
virtualenv is one of the commonest reasons for that. A panel that needed the
environment it exists to repair would be missing exactly when it is wanted.

**Settings** are generated from `schema.py`, so every setting the bot reads
appears with its help text, type and bounds, and a test asserts the two never
drift. Saving rewrites `.env` in place — comments, ordering and any setting
this build has never heard of all survive — and tells you which service to
restart, because configuration is read once at startup and nothing takes effect
until it is.

**Voices can be auditioned before saving.** Browse all 54 Kokoro voices, click
one, hear it. Japanese and Mandarin need an extra package and the panel offers
to install it.

**Tokens are write-only.** No response from the server ever contains a stored
secret — a set token shows as "set" plus its last four characters, so you can
tell one from another and nothing more. Leaving a token field alone leaves the
stored value alone.

**Access.** It listens on `127.0.0.1` and nothing else unless you both set a
password and turn remote access on — enforced where the socket is bound, not
merely in the UI. Turning remote access on exposes this page, and the token
fields on it, to everything that can route to your machine. Only on a network
you trust.

It also refuses requests carrying a hostname it does not answer to, which is
what stops a page on the internet pointing its own domain at `127.0.0.1` and
driving the panel through your browser. Reach it as `127.0.0.1`.

---

## Running it

```bash
scripts/start-athena.sh       # background, with a log file
scripts/stop-athena.sh        # stops the bot, then the mpv it owns
```

Use the stop script rather than killing Python. It stops the bot *before* the
mpv it owns — the other order makes the watchdog relaunch mpv instantly — and
it sends SIGINT rather than SIGKILL, so mpv shuts down cleanly instead of
leaving a fullscreen window behind.

To start on login, install the LaunchAgents with `scripts/launchd-*.sh`. After
that, use the control panel's Start and Stop rather than the scripts: launchd's
`KeepAlive` will undo anything else within ten seconds.

Slash commands exist as a fallback: `/play /queue /status /pause /skip /stop
/restart /help`.

---

## Voice and speech

Both are optional and off by default. Turn them on in the panel.

**Speech** uses [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), an 82M
model that runs on the GPU and produces a line in about a second. It needs its
own virtualenv, because it pins a different Python and torch than the bot:

```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv tts/.venv
tts/.venv/bin/python -m pip install -r tts/requirements.txt
scripts/start-tts.sh
```

3.12, not 3.13 — Kokoro declares `Requires-Python <3.13`.

**Voice input** uses faster-whisper on the CPU, and needs
`requirements-dev.txt` as well as two virtual audio devices.

### The audio cables

macOS has no built-in loopback, so both directions need a virtual device — two
of them, not one, because a single shared device would feed her own voice
straight back into her ears:

```bash
brew install --cask blackhole-2ch blackhole-16ch
```

That installs a system audio driver, so it asks for your password, and Discord
needs restarting afterwards to see the new devices. Then, in Discord:

| direction | device | Discord setting |
|---|---|---|
| she hears the room | **BlackHole 2ch** | Output Device |
| the room hears her | **BlackHole 16ch** | Input Device (mic) |

Routing output to BlackHole means *you* stop hearing it. Fix that with a
Multi-Output Device in **Audio MIDI Setup** (Applications → Utilities)
containing both BlackHole 2ch and your speakers, and point Discord at that.

Two things that are easy to miss and produce silence rather than an error: the
streaming account must be **muted, never deafened** — a deafened client
receives no audio at all, so there is nothing to capture — and Discord will not
move a live voice connection to a newly selected device, so leave and rejoin
the channel after changing it.

`scripts/check-audio.py` verifies both directions.

### What it writes down

With voice on, everything the microphone picks up is transcribed — including
conversation not addressed to the bot. The bot's own log records only the shape
of an utterance ("3.9s: NO-WAKE (13 words)"); the words go to a separate file,
so turning that off is one switch rather than an audit of every log line.

`VOICE_TUNING_ENABLED` is that switch, and it is **on** by default because it
is how you tune the wake word against a real room. It rotates at 5MB. If other
people are in the room, tell them, or turn it off.

---

## Why the defaults are what they are

This started on a Windows box with an RTX 2060 Super and moved to Apple
silicon. Several defaults changed as a result, and the reasoning sits in the
comments beside each one. The differences worth knowing up front:

| | 8GB discrete GPU | Apple silicon |
|---|---|---|
| Whisper | CUDA, `medium` | CPU only — CTranslate2 has no Metal backend. `small` measures 4.8x realtime; `medium` manages 1.7x |
| Model size | 8GB VRAM, hard ceiling | unified memory — the ceiling that picked a 3B model is gone |
| GPU vs screen encoding | real contention; inference stuttered the share | encode runs on the media engine, separate silicon |
| AV1 video | no hardware decode | still none before M3 |

Two consequences worth acting on:

- **Sleep will stop everything.** A sleeping host drops the Discord gateway and
  stops mpv, and some Macs report `sleep 1` on AC as well as battery. Disable
  it (`sudo pmset -a disablesleep 1`) if this is a machine that should stay up.
  The launchers wrap the bot in `caffeinate -dis` regardless, because `-d` also
  blocks *display* sleep — which matters on its own account when the screen is
  what is being shared.
- **The model choice is worth revisiting on your hardware.** It was made by
  measurement, not preference, and `scripts/bakeoff.py` is how. See below.

---

## Choosing a model

Most requests never reach the model, so a routing miss looks like a model
problem. Check `fast_match` and `try_direct_play` first.

If it genuinely is the model, score candidates rather than guessing. Run these
**from the repo root** — they are relative paths:

```bash
./scripts/bakeoff.py --list
./scripts/bakeoff.py --pull granite4:3b qwen3:8b
./scripts/bakeoff.py -v granite4:3b qwen3:8b --runs 3 --json bakeoff.json
```

`bakeoff.py` drives the real Ollama path with the real tool schema and a faked
Controls, so nothing plays. It ranks on **fabrication rate first** — a model
that says "Playing X" without calling the tool is worse than a slow one.

`chatoff.py` scores the other half: whether a question is routed to
conversation at all, and whether the reply is any good. They are different
skills, and a model can be flawless at picking `play_media` and still be poor
company.

`-v` prints a line per run; without it there is no output until a model
finishes all its cases, which is a long silence.

---

## Troubleshooting

**mpv won't start.** Run `mpv --version` in the same shell. If that works but
the bot cannot spawn it, set `MPV_PATH` to the absolute path. Under launchd
this is usually PATH: a LaunchAgent inherits none of your shell's environment.

**"Couldn't find the Spotify window" when Spotify is clearly open.** Almost
always the Accessibility grant, not the window. macOS refuses window queries
silently, which is indistinguishable from an absent window. Transport control
(play/pause/skip) uses **Automation**, a separate grant that can be missing on
its own.

**Nothing plays but no error.** Check the log for the direct-play URL. If Plex
is on another machine, the bot's host must reach `PLEX_URL` directly — there is
no transcode fallback, mpv plays the original file.

**A file genuinely won't play** (rare — mpv handles almost everything). Add
`MPV_EXTRA_OPTS=hwdec=no` to rule out hardware decode first.

**There is an orange dot in the corner of the screen.** That is macOS's
microphone-in-use indicator, on because the bot holds an open capture stream
the whole time voice is enabled. It cannot be turned off: WindowServer draws it
above all application content precisely so nothing can record without it
showing. Tried and did *not* help: `--native-fs=no` on mpv. The only real lever
is `VOICE_ENABLED=false`, which removes the dot and voice input together.

**Nothing is transcribed, or she never speaks.** The audio cables — see above.
The bot logs exactly which device is missing and how to install it.

**She pronounces things oddly.** The voice and the pronunciation rules are
separate settings. Kokoro's voice prefix *is* its language code — `af_` is
American, `bf_` British — and `TTS_LANG_CODE=auto` derives one from the other.
Setting them inconsistently gives you an American voice reading with British
rules, which sounds subtly wrong rather than obviously broken.

**It replied with paragraphs of reasoning.** A thinking model with
`OLLAMA_THINK` set to `true` or `none`. Blank it to send `think: false`.

**It narrated an action instead of doing it** — "Playing **X**" with nothing
happening. That is the failure mode `tests/test_no_fake_actions.py` exists to
catch; run it. Model choice matters more than prompt wording here.

---

## Development

```bash
.venv/bin/python tests/run_all.py
```

26 suites, no Plex, Spotify, mpv, Discord — or even a `.env` — required.
Placeholders are supplied for the four required settings, so a fresh clone
passes its own tests before you have credentials.

`.env.example` is **generated** from `schema.py`:

```bash
./scripts/gen-env-example.py
```

Adding a setting means adding it to `schema.py`. It then appears in the control
panel, in `.env.example`, and in validation, with no other change — and
`tests/test_schema.py` fails the build if `config.py` reads a setting the schema
does not describe, or the schema describes one nothing reads.

Windows has `.ps1` equivalents of the launchers and `winctl.py` for window
control. `wm.py` picks a backend at import. It is not the primary target and
the Windows paths are not routinely exercised.
