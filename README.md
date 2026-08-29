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

## Setup

Roughly **20 minutes**, most of it waiting for downloads. The control panel does
the software side for you; the parts that need a password, a web login, or a
macOS dialog are yours, and they are listed here in order.

| | who does it |
|---|---|
| Homebrew, Python, mpv, Ollama | **you** — one command each, below |
| virtualenvs, dependencies, `.env`, model download | the panel |
| Discord / Plex / Spotify credentials | **you** — three web pages |
| macOS permission grants | **you** — two dialogs |
| Audio devices for voice | **you** — needs your password |

---

### 1. Prerequisites

If you do not have [Homebrew](https://brew.sh):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then:

```bash
brew install python@3.13 mpv yt-dlp
```

macOS ships Python 3.9. The control panel runs on it deliberately, but the bot
needs 3.13. `ffmpeg` arrives with mpv.

**For fuzzy requests and conversation** you need [Ollama](https://ollama.com).
The control panel can install and start it for you in step 2, or:

```bash
brew install ollama
brew services start ollama
```

`brew services` rather than `ollama serve`, so it survives a reboot.

Skip it if you like — the bot still handles anything unambiguous (`play X`,
`pause`, `back 30s`) and only vague requests are lost.

---

### 2. Start the control panel

```bash
git clone https://github.com/captsmuckers/pantheon.git
cd pantheon
scripts/start-gui.sh
```

Open **<http://127.0.0.1:8086>**. It lands on a setup page listing what is
present and what is not.

Press **"Do this for me"** on each outstanding item. It will create the
virtualenv, install dependencies, copy the config file, and pull a language
model, streaming the output as it goes. The model is about 5 GB and the slowest
part.

Leave it open — the rest of this is done in it.

---

### 3. Discord

You need a bot account, its token, and the ID of the channel it listens in.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**. Name it whatever you like.
2. Open the **Bot** tab.
   - Under **Privileged Gateway Intents**, turn on **MESSAGE CONTENT INTENT**.
     Without this the bot connects and then ignores every message, with no
     error — it is the single commonest setup mistake.
   - Click **Reset Token**, then **Copy**. This is `DISCORD_TOKEN`. You cannot
     view it again later, only reset it.
3. Open **OAuth2 → URL Generator**.
   - Scopes: **`bot`** and **`applications.commands`**
   - Bot permissions: **View Channels**, **Send Messages**, **Read Message
     History**, **Connect**, **Speak**. Add **Manage Channels** only if you want
     it to keep a status channel's topic updated.
   - Copy the generated URL at the bottom, open it, and invite the bot to your
     server.
4. Get the channel ID. In Discord: **Settings → Advanced → Developer Mode**, on.
   Then right-click the channel it should listen in → **Copy Channel ID**. This
   is `ALLOWED_CHANNEL_ID`.

Paste both into the panel's **Settings → Discord**.

---

### 4. Plex

`PLEX_URL` is your server, for example `http://192.168.1.10:32400`. The machine
running the bot must be able to reach it directly — there is no transcode
fallback, mpv plays the original file.

For `PLEX_TOKEN`, follow Plex's own instructions:
[Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
The short version: open any item in Plex Web, **⋯ → Get Info → View XML**, and
the token is the `X-Plex-Token=` value in the address bar.

Paste both into **Settings → Plex**.

#### Using someone else's Plex server

If you watch a friend's library rather than running your own, both settings are
different and neither is guessable. Run:

```bash
./scripts/find-plex-server.py
```

It gives you a four-character code to enter at
[plex.tv/link](https://plex.tv/link) while signed in **as yourself**, then
prints a ready-to-paste `PLEX_URL` and `PLEX_TOKEN` for every server you can
reach — including shared ones. No password is typed into it and nothing is
stored.

The link code exists because **there are two kinds of Plex token** and the one
that is easy to find is the wrong one for this job:

| | what it is | how to get it |
|---|---|---|
| **Server token** | authenticates you to one server — this is what goes in `PLEX_TOKEN` | the usual "Get Info → View XML" trick |
| **Account token** | authenticates you to plex.tv, the only thing that knows how to reach a server from outside its LAN | a link code |

The usual advice hands you the token the bot wants but not the one needed to
*find* the server. Hence the code.

Two more things to understand:

- **You use your own Plex account, not the owner's.** Each server issues its
  own access token to each user who can reach it. Yours is the one the bot
  needs, and it keeps your watched state separate from theirs.
- **`PLEX_URL` will be an address like
  `https://203-0-113-9.<hash>.plex.direct:32400`.** The owner must have
  **Remote Access** enabled or no such address is advertised.

**plex.tv handles sign-in and discovery only — the video does not go through
Plex.** That `plex.direct` hostname is a DNS trick: the dashed label decodes to
the literal IP (`203-0-113-9…` resolves to `203.0.113.9`), and Plex holds a
wildcard certificate so HTTPS works against an address that has no name of its
own. The stream goes straight from your machine to the owner's connection, the
same as any other Plex client.

The exception is **Plex Relay**, which genuinely is proxied through Plex — and
throttled to roughly 1–2 Mbps. Plex falls back to it when a direct connection
cannot be made. `find-plex-server.py` lists relay addresses separately and tells
you not to use them: this bot plays the original file and cannot drop quality
to fit, so a relay connection would connect and then buffer forever. Anyone
already watching a shared library at full quality has a direct connection and
will be fine.

**The bandwidth caveat is the important one.** This bot never transcodes — it
hands mpv the original file, which is why subtitle changes are instant and
practically any codec plays. Over the internet that means playback needs as
much bandwidth as the file's own bitrate, sustained, over the **owner's
upload**. A 1080p film at 8 Mbps is comfortable; a 4K remux at 60–80 Mbps is
not, and there is no quality setting to lower it. Ordinary Plex clients
transcode in that situation; this one cannot.

At this point the setup page should show nothing outstanding. Start the bot
from the **Status** page and talk to it in the channel.

---

### 5. macOS permissions

The bot moves mpv and Spotify windows around. Two **separate** grants, in
**System Settings → Privacy & Security**:

- **Accessibility** — add whatever runs the bot (Terminal, iTerm, or the Python
  binary itself). Without it every window call fails and the bot reports it
  cannot find windows that are plainly on screen.
- **Automation** — for controlling Spotify. Requested the first time the bot
  tries; if you dismiss that dialog it is never asked again and must be
  re-enabled by hand.

Both fail *silently*, which is why they are worth granting deliberately. The
setup page checks Accessibility and will tell you if it is missing.

Screen Recording is a third grant and belongs to Discord, not to the bot.

---

### 6. Spotify (optional)

Only needed for music. **Spotify Premium is required** — the API will not
control playback on a free account.

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   and click **Create app**.
2. Set the **Redirect URI** to exactly `http://127.0.0.1:8888/callback`.
3. Copy the **Client ID** and **Client Secret** into **Settings → Spotify**.
4. Run `scripts/reauth-spotify.sh` once to authorise. It opens a browser; the
   token is cached afterwards.

---

### 7. Voice and speech (optional)

Both are off by default. Turn them on in **Settings → Voice** and
**Settings → Speech**.

**Speech** — the panel can install it for you from the setup page. It is about
2 GB and needs Python 3.12, because Kokoro declares `Requires-Python <3.13`:

```bash
brew install python@3.12
```

Then press **"Do this for me"** on the speech step.

**Voice input** needs the packages (the panel installs those) and two virtual
audio devices (it cannot — this installs a system driver and needs your
password):

```bash
brew install --cask blackhole-2ch blackhole-16ch
```

**Restart Discord afterwards** or it will not see the new devices. Then, in
Discord's **Voice & Video** settings:

| direction | device | Discord setting |
|---|---|---|
| she hears the room | **BlackHole 2ch** | Output Device |
| the room hears her | **BlackHole 16ch** | Input Device (mic) |

Routing output to BlackHole means *you* stop hearing it. Fix that with a
**Multi-Output Device** in **Audio MIDI Setup** (Applications → Utilities):
click **+** at the bottom left, tick both BlackHole 2ch and your speakers, and
point Discord at that instead.

Two things that produce silence rather than an error:

- The streaming account must be **muted, never deafened**. A deafened client
  receives no audio at all, so there is nothing to capture.
- Discord will not move a live voice connection to a newly selected device.
  **Leave and rejoin** the channel after changing it.

`scripts/check-audio.py` verifies both directions.

---

### 8. Age-restricted YouTube (optional)

yt-dlp cannot reliably get past YouTube's age gate — an authenticated client is
served zero usable formats, so supplying cookies makes it worse rather than
better. The fallback does not try: it opens the ordinary youtube.com page in a
signed-in Firefox and lets YouTube's own player handle it.

Everything else on YouTube works without this. If you skip it, age-gated links
report that they cannot be played.

```bash
brew install --cask firefox
scripts/setup-firefox-profile.sh --open
```

The script creates the profile and configures it; the window it opens is for
**you to sign in to YouTube**, which is the one part nothing can automate.

The configuration matters more than it looks. A fresh Firefox profile blocks
autoplay (it has no interaction history with youtube.com), and shows a welcome
tour and a data notice *on top of the page* — in kiosk mode there is no toolbar
to dismiss them from. The video would open and appear to do nothing. The script
writes a `user.js` that turns all of that off.

The setup page reports this as done only when the profile exists, is
configured, **and** is signed in — installed-but-not-signed-in is the state
that looks finished and is not.

There is no playback control once a video opens this way: pause, seek and stop
do not reach it.

---

### 9. Start on login (optional)

Press **"Do this for me"** on the last setup step, or:

```bash
scripts/install-launchagents.sh              # install and start
scripts/install-launchagents.sh --uninstall  # stop and remove
```

This writes the two LaunchAgents with the right absolute paths for your
checkout and loads them. Both services then start when you log in and restart
if they crash.

After that, use the control panel's Start and Stop rather than the scripts:
launchd's `KeepAlive` will undo anything else within ten seconds.

It must run in your logged-in GUI session — window control needs a real login
session, so a LaunchAgent is fine and a LaunchDaemon is not. Closing the lid
with no external display stops everything.

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

Slash commands exist as a fallback: `/play /queue /status /pause /skip /stop
/restart /help`.

---

## What voice input writes down

With voice on, everything the microphone picks up is transcribed — including
conversation not addressed to the bot. The bot's own log records only the shape
of an utterance ("3.9s: NO-WAKE (13 words)"); the words go to a separate file,
so turning that off is one switch rather than an audit of every log line.

`VOICE_TUNING_ENABLED` is that switch, and it is **on** by default because it
is how you tune the wake word against a real room. It rotates at 5 MB, and the
panel shows it under **Logs → Heard**.

If other people are in the room, tell them, or turn it off.

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

## Keeping it up to date

The **Status** page checks the repository and shows what is new. Updating is
two clicks — check, then update — and never happens on its own.

It will only ever **fast-forward**. If you have committed local changes, or
have uncommitted edits to tracked files, it refuses and tells you which,
rather than merging or discarding anything. `.env` is untracked, so an update
cannot touch it.

If a requirements file changed, those are reinstalled as part of the update.
Afterwards it names the services to restart — except the control panel itself,
which you restart by hand, because it is the thing serving the page you asked
from.

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
