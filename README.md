# Athena

A Discord bot that runs the media in a room: Plex films and shows through mpv,
music through Spotify, YouTube on request, karaoke lyrics over the idle screen —
and it will talk back, in a voice best described as unimpressed.

Plex is only a metadata and file source; mpv does the playing.

Requests are handled in tiers so the language model sees as little as possible:

1. `fast_match` — deterministic regex routing for anything unambiguous
2. `try_direct_play` — library-first scored resolution, then Spotify
3. the model — only genuinely fuzzy requests ("something with dragons in it")

## What changed and why (the mpv rewrite)

| Old | New |
|---|---|
| `client.playMedia()` over Plex Companion | mpv `loadfile` over JSON IPC |
| Plex owned the play queue | the bot owns the queue (a Python list) |
| Next episode = hope HTPC remembers | `end-file` event → we load the next file |
| Subtitle change = stop, sleep 5s, replay | set `sid`, instant, no interruption |
| Polled the client's `timeline` every 15s | polls mpv locally; never touches a Plex client |
| Crash = manual restart | watchdog relaunches mpv and resumes position |
| Slash commands only | plain chat, with slash fallbacks |

## Setup (macOS)

This branch runs on macOS. The Python is cross-platform — `wm.py` picks between
the Win32 and AppleScript window backends at import — but the two requirements
files and the `scripts/*.sh` launchers are macOS-only; their Windows
counterparts are the `.ps1` files, still in `scripts/`.

1. **Install the tools:**
   ```
   brew install python@3.13 mpv yt-dlp
   ```
   ffmpeg comes along with mpv. Leave `MPV_PATH` blank — brew puts mpv on PATH.
2. **Install deps:**
   ```
   python3.13 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```
   Add `-r requirements-dev.txt` as well if you want voice input.
3. **Grant permissions.** The bot moves mpv and Spotify windows around, and
   that needs two *separate* grants in System Settings > Privacy & Security.
   Both are asked for once and refused silently forever after if the prompt was
   dismissed, so they are worth setting deliberately:
   - **Accessibility** — for whatever runs the bot (Terminal, iTerm, or the
     Python binary). Without it every window call fails and the bot reports
     "Couldn't find the Spotify window" for windows that are plainly there.
   - **Automation** — for controlling Spotify. This is the one that gets
     missed, because the other prompt looks like it covered everything.

   Screen Recording, for Discord, is a third grant and belongs to Discord
   rather than to the bot.
4. **Credentials:** copy `.env.example` to `.env` and fill it in.
   - Generate a **new** Discord bot token — the old one was committed in plain
     text and should be treated as compromised.
   - Reset your Plex token: Plex account settings → Devices → sign out all,
     then grab a fresh token.
   - Language backend: Ollama, locally, and there is no remote fallback — the
     Anthropic backend was removed. Install it:
     `brew install ollama && ollama serve && ollama pull granite4:3b`. It runs
     on Metal out of unified memory, so the ~3GB figure from the Windows card
     is memory shared with everything else, not dedicated VRAM. Without it the
     bot still handles simple commands (`play X`, `pause`, `back 30s`) and only
     genuinely fuzzy requests are lost.
5. **Run:** `.venv/bin/python bot.py`, or `scripts/start-athena.sh` to
   background it with a log file. `scripts/stop-athena.sh` stops it — use that
   rather than killing python, because it stops the bot before the mpv it owns
   (the other order makes the watchdog relaunch mpv instantly) and it sends
   SIGINT rather than SIGKILL so mpv gets shut down cleanly.

   It must run in your logged-in GUI session: window control needs a real login
   session, so a LaunchAgent is fine and a LaunchDaemon is not.

Discord Developer Portal: the bot needs the **Message Content Intent** enabled
(Bot → Privileged Gateway Intents), plus `Manage Channels` if you want the
status channel topic updated.

## The control panel

A local web UI for everything below: the settings, starting and stopping the
services, and the logs.

```
scripts/start-gui.sh
```

Then open <http://127.0.0.1:8086>. Ctrl-C stops it.

It deliberately depends on **nothing**. No web framework, no virtualenv, and
Python 3.9 — the version macOS ships — is enough. That is not minimalism for
its own sake: this is the tool you open when the bot will not start, and a
broken virtualenv is one of the commonest reasons for that. A panel that needed
the environment it exists to repair would be missing exactly when it is wanted.

**Settings** are generated from `schema.py`, so every setting the bot reads
appears here with its help text, its bounds and its type, and a test asserts
that the two never drift apart. Saving writes `.env` in place — comments,
ordering and any setting this build has never heard of are all preserved — and
tells you which service to restart, because `config.py` reads its configuration
once at startup and nothing takes effect until it does.

**Tokens are write-only.** The server never sends one to the browser, not even
masked: a stored token shows as "set", with its last four characters so you can
tell one from another. Leaving the field alone leaves the stored value alone;
clearing one is a separate, deliberate checkbox.

**Access.** It listens on `127.0.0.1` and nothing else unless you both set a
password and turn remote access on — enforced where the socket is bound, not
just in the UI, so hand-editing `.athena-gui.json` cannot open it either.
Turning remote access on exposes this page, and the token fields on it, to
everything that can route to this machine. Only do it on a network you trust.

The panel also refuses requests carrying a hostname it does not answer to,
which is what stops a page on the internet from pointing its own domain at
`127.0.0.1` and driving the panel through your browser. If you reach it by some
other name, use `127.0.0.1` instead.

**Two checkouts on one machine** is handled rather than ignored: the panel
identifies processes by their working directory, so it will tell you a bot is
running *from somewhere else* rather than reporting Stopped and inviting you to
start a second one into the same Discord channel.

## The machine this is tuned for

Ported from a Windows box with an RTX 2060 Super (8GB VRAM) to a 14-inch
MacBook Pro, M1 Max, 32GB unified memory, 10 CPU cores (8 performance + 2
efficiency), 32 GPU cores. Several defaults changed because of that, and the
reasoning is in the comments at each one — but the differences worth knowing
up front:

| | RTX 2060 Super box | This M1 Max |
|---|---|---|
| Whisper | CUDA, `medium` | CPU only — no CTranslate2 Metal backend. `small`, measured at 4.8x realtime; `medium` manages just 1.7x |
| LLM memory budget | 8GB VRAM, hard ceiling | 32GB unified — the ceiling that picked `granite4:3b` is gone |
| GPU vs stream encoder | Real contention; inference stuttered the share | Encode runs on the media engine, separate silicon from the GPU cores |
| AV1 video | No hardware decode (Maxwell-era concern) | Still no hardware decode — arrives with M3 |
| Sleep | Desktop, always on | Laptop; sleep now disabled machine-wide, plus `caffeinate` per run |

Three consequences worth acting on:

- **Sleep had to be dealt with, and has been, twice over.** This machine
  shipped reporting `sleep 1` on AC as well as battery — a sleeping host drops
  the Discord gateway and stops mpv. It is now disabled machine-wide
  (`pmset -a disablesleep 1`; `pmset -g` shows `SleepDisabled 1`), and
  `scripts/start-athena.sh` still wraps the bot in `caffeinate -dis` because
  `-d` also blocks *display* sleep, which matters on its own account when the
  screen is what's being shared. If you start `bot.py` by hand, wrap it
  yourself.
- **The Ollama model was chosen against an 8GB VRAM ceiling that no longer
  exists.** `granite4:3b` won its bake-off partly on fitting in 3GB. With 32GB
  unified there is room for something far larger, and `tests/test_no_fake_actions.py`
  is the suite that decided it last time — the choice was measurement-driven, so
  re-measure rather than assuming bigger is better.
- **`OLLAMA_NUM_GPU=0` and `FLAVOR_NUM_GPU=0` are probably obsolete here.** Both
  existed to keep inference off a GPU that was also encoding the stream. On
  Apple silicon those are different blocks, and there is no separate VRAM to
  free. Leave them blank unless a stutter is actually measured.

Being a laptop matters in one more way: window control and screen sharing need
a real logged-in session, so closing the lid without an external display and
power will stop everything.

## Talking to it

Anything in the allowed channel that doesn't start with a punctuation prefix is
treated as a request:

```
put on the office s3e5
what's playing?
skip ahead 5 minutes
queue up dune part two after this
what did he say          → jumps back 15s
turn the subs off
it's frozen              → relaunches the player
```

Slash commands still exist as a fallback: `/play /queue /status /pause /skip
/stop /restart /help`.

## Tuning

- `MPV_EXTRA_OPTS` takes any mpv options, comma separated. On Apple silicon
  `hwdec=videotoolbox` and `vo=gpu-next` are the defaults to want —
  videotoolbox decodes on the media engine rather than the GPU proper, which
  leaves the GPU to whatever is encoding the screen share. Drop `vo=gpu-next`
  if you see artifacting.
- `FREEZE_TIMEOUT` (default 45s) is how long playback can stall before the
  watchdog reloads the file at the last known position.
- `AUTOPLAY_NEXT_EPISODE=false` if you'd rather it stop at the end of an episode.

## Troubleshooting

**mpv won't start:** run `mpv --version` in the same shell. If that works but
the bot can't spawn it, set `MPV_PATH` to the absolute path.

**"Couldn't find the Spotify window" when Spotify is clearly open:** this is
almost always the Accessibility grant, not the window. macOS refuses window
queries silently, which is indistinguishable from an absent window from the
bot's side — `macctl.py` logs a specific error the first time it happens, so
check the log. Transport control (play/pause/skip) uses **Automation**
permission instead, which is a separate grant that can be missing on its own.

**Nothing plays but no error:** check the console for the direct-play URL. If
Plex is on another machine, the bot's host must be able to reach `PLEX_URL`
directly — there's no transcode fallback, mpv plays the original file.

**A file genuinely won't direct play** (rare — mpv handles almost everything):
add `MPV_EXTRA_OPTS=hwdec=no` to rule out hardware decode first.

**There is an orange dot in the corner of the screen.** That is macOS's
microphone-in-use indicator, and it is on because the bot holds an open capture
stream on BlackHole 2ch the whole time voice is enabled. It cannot be turned
off: WindowServer draws it above all application content precisely so that no
app can record without it showing, and there is no setting, plist or
entitlement that suppresses it. Tried and did NOT help: `--native-fs=no` on
mpv, on the theory that non-native fullscreen would keep the menu bar and its
indicators off the film. The dot appears over the film either way.

The only real lever is not holding the microphone open — `VOICE_ENABLED=false`
removes the dot and voice input together. Releasing the stream only while a
film plays would work too, at the cost of no voice commands during films, which
is when they are most wanted.

**Nothing is transcribed even though voice is on**, or she never speaks: the
audio cables. macOS has no built-in loopback, so both directions need a virtual
device, and the bot logs exactly which one is missing and how to install it.

The Windows machine used two VB-Audio cables. The macOS equivalent is two
BlackHole devices — two, not one, because a single device shared by both
directions would feed her own voice straight back into her ears:

```
brew install --cask blackhole-2ch blackhole-16ch
```

That installs a system audio driver, so it asks for your password and Discord
needs restarting afterwards to see the new devices. Then, in Discord:

| direction | device | Discord setting |
|---|---|---|
| she hears the room | **BlackHole 2ch** | Output Device |
| the room hears her | **BlackHole 16ch** | Input Device (mic) |

Routing output to BlackHole means *you* stop hearing it. Fix that with a
Multi-Output Device in **Audio MIDI Setup** (Applications → Utilities)
containing both BlackHole 2ch and your speakers, and point Discord at that
instead.

Two things that are easy to miss and produce silence rather than an error: the
streaming account must be **muted, never deafened** — a deafened client
receives no audio at all, so there is nothing to capture — and Discord will not
move a live voice connection to a newly selected device, so leave and rejoin
the channel after changing it.

**Model is being weird:** most requests never reach it — check whether the
phrasing should have been caught by `fast_match` or `try_direct_play` first,
since a routing miss looks like a model problem. If it genuinely is the model,
try a larger one — 32GB unified allows far bigger than the 3B the Windows card
was limited to — and score candidates with `scripts/bakeoff.py`:

Run these **from the repo root** — they are relative paths, and from anywhere
else the shell just reports that the file does not exist:

```
cd ~/athena
./scripts/bakeoff.py --list
./scripts/bakeoff.py --pull granite4:3b qwen3:14b
./scripts/bakeoff.py -v granite4:3b qwen3:14b --runs 3 --json bakeoff.json
```

`-v` prints a line per run; without it there is no output until a model has
finished all its cases, which is a long silence. `--json` keeps the full
per-run detail, including the actual replies, which is what you want when a
score looks surprising.

It drives the real `_ask_ollama` path with the real tool schema and a faked
Controls, so nothing plays. It ranks on **fabrication rate first** — that is
what chose `granite4:3b` over models twice its size, and it does not improve
just because the machine got bigger. Note that `tests/test_no_fake_actions.py`
is not a substitute: it scripts a fake HTTP client and never contacts a model.

**It replied with paragraphs of reasoning:** a thinking model with
`OLLAMA_THINK` set to `true` or `none`. Blank it to send `think: false`.

**It narrated an action instead of doing it** ("Playing **X**" with nothing
happening): that is the failure mode `tests/test_no_fake_actions.py` exists to
catch. Run it. Model choice matters more than prompt wording here — see the
comments above `OLLAMA_MODEL` in `.env.example`.
