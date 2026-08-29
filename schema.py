"""One description of every setting, for everything that needs to know.

config.py knows how to PARSE settings — types, defaults, coercion. .env.example
knows how to EXPLAIN them — 124 lines of prose about why each one is what it is.
Neither knows the other exists, and four things want both:

  * a settings UI, which needs a widget per setting and help text beside it
  * validation, so a bad value is refused before it reaches a running bot
  * migration, so a config written by an older version gains new settings
    instead of silently falling back to defaults that were never chosen
  * first-run setup, which needs to know what is required and what can wait

Hand-maintaining that list in four places guarantees it drifts. So it lives
here once, and .env.example is GENERATED from it (see scripts/gen-env-example.py)
rather than written by hand.

Deliberately NOT imported by config.py. config is what the bot reads at startup
and it has no business depending on a description of itself — this module is for
tools that manage the bot, not for the bot. The one thing that keeps them honest
is a test asserting every setting config.py reads appears here.
"""

# Postponed evaluation of annotations, which is what lets `float | None` be
# written here and still import on Python 3.9 — the version macOS ships. The
# control panel is meant to run before anything is installed, on whatever
# interpreter is already on the machine, and it imports this module. Without
# this line the dataclass below raises TypeError at import on a stock Mac.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ----------------------------------------------------------------------
# what a setting is
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    """One environment variable, described well enough to build a UI from.

    `kind` drives the widget and the validation:

        str      a line of text
        text     several lines (a persona, a prompt)
        int      whole number, optionally bounded by lo/hi
        float    likewise
        bool     a toggle
        choice   one of `choices`
        words    a space-separated list
        list     a comma-separated list
        path     a file or directory, with `must_exist` deciding whether a
                 missing one is an error or merely not yet chosen
        secret   a token. Never rendered back to a UI; write-only.

    `restart` says what must be bounced for a change to take effect. Every
    setting needs something bounced, because config.py calls load_dotenv() at
    import and freezes the lot — there is no live reload, and pretending
    otherwise would be the single most confusing thing a settings UI could do.
    """

    name: str
    kind: str
    default: Any = ""
    help: str = ""
    section: str = "General"
    restart: str = "bot"          # "bot" | "tts" | "none"
    required: bool = False
    choices: tuple = ()
    lo: float | None = None
    hi: float | None = None
    must_exist: bool = False
    platform: str = ""            # "" both, "darwin", "win32"
    advanced: bool = False        # hidden behind a disclosure in a UI

    def is_secret(self) -> bool:
        return self.kind == "secret"


def _s(*args, **kwargs) -> Setting:
    return Setting(*args, **kwargs)


# ----------------------------------------------------------------------
# the settings
# ----------------------------------------------------------------------
#
# Ordered as someone setting the bot up would meet them: the things without
# which it cannot start, then the things that make it good, then the tuning
# nobody touches until something is wrong.

SETTINGS: tuple[Setting, ...] = (
    # -- Discord ------------------------------------------------------
    _s("DISCORD_TOKEN", "secret", required=True, section="Discord",
       help="Bot token from the Discord Developer Portal. The bot also needs "
            "the Message Content Intent enabled there, under Bot > Privileged "
            "Gateway Intents, or it will connect and then ignore every message."),
    _s("ALLOWED_CHANNEL_ID", "str", required=True, section="Discord",
       help="The text channel it listens in. Anything said there that does not "
            "start with a punctuation prefix is treated as a request."),
    _s("STATUS_CHANNEL_ID", "str", section="Discord",
       help="Optional. A channel whose topic is kept up to date with what is "
            "playing. Needs the Manage Channels permission."),

    # -- Plex ---------------------------------------------------------
    _s("PLEX_URL", "str", required=True, section="Plex",
       help="e.g. http://192.168.1.10:32400. The machine running the bot must "
            "reach this directly — mpv plays the original file and there is no "
            "transcode fallback."),
    _s("PLEX_TOKEN", "secret", required=True, section="Plex",
       help="Plex account settings > Devices will show one, or sign out of all "
            "devices and take a fresh one."),
    _s("EXCLUDE_LIBRARIES", "list", section="Plex",
       help="Libraries to ignore, matched as a substring — \"4K\" hides any "
            "library whose name contains it. Blank excludes nothing."),
    _s("ANIME_LIBRARIES", "list", section="Plex", advanced=True,
       help="Libraries that should default to subtitled audio. Blank means none."),

    # -- Player -------------------------------------------------------
    _s("MPV_PATH", "path", section="Player",
       help="Leave blank if mpv is on PATH, which it is after `brew install "
            "mpv` or `winget install mpv`."),
    _s("MPV_FULLSCREEN", "bool", default=True, section="Player",
       help="Fullscreen on launch. The point of this bot is a shared screen, so "
            "this is almost always what you want."),
    _s("MPV_EXTRA_OPTS", "str", section="Player", advanced=True,
       default="hwdec=videotoolbox,vo=gpu-next", platform="darwin",
       help="Any mpv options, comma separated. videotoolbox decodes on Apple "
            "silicon's media engine rather than the GPU, which leaves the GPU "
            "for whatever is encoding the screen share."),
    _s("MPV_NATIVE_FS", "choice", default="no", choices=("no", "yes"),
       section="Player", platform="darwin", advanced=True,
       help="macOS only. \"no\" keeps mpv on the current Space, which is what a "
            "screen share needs. \"yes\" is the green-button fullscreen, which "
            "moves it to a Space of its own AND breaks minimising."),
    _s("IDLE_IMAGE", "path", section="Player",
       help="A still shown between things, so a shared screen is never a frozen "
            "frame. Blank means mpv shows its own default."),
    _s("MPV_WINDOW_TITLE", "str", default="Athena", section="Player",
       advanced=True,
       help="What the player window is called, and how the bot finds it again."),

    # -- Language model -----------------------------------------------
    _s("OLLAMA_HOST", "str", default="http://127.0.0.1:11434", section="Model",
       help="Where Ollama is listening."),
    _s("OLLAMA_MODEL", "str", default="qwen3:8b", section="Model",
       help="The model for tool calling and conversation. Judge a candidate on "
            "how often it claims an action it did not take, not on size — a 3B "
            "has beaten every 14B tried here on exactly that."),
    _s("FLAVOR_MODEL", "str", section="Model", advanced=True,
       help="Optional separate model for the one-line asides, which fire far "
            "more often than tool calls. Blank follows OLLAMA_MODEL."),
    _s("OLLAMA_KEEP_ALIVE", "str", default="-1", section="Model", advanced=True,
       help="How long Ollama keeps the model in memory. Its default of 5 "
            "minutes means the first question after a quiet evening pays a full "
            "reload. -1 pins it, at the cost of holding the memory."),
    _s("OLLAMA_THINK", "choice", default="", choices=("", "true", "none"),
       section="Model", advanced=True,
       help="Blank sends think:false, which is what you want — a reasoning "
            "model left to think writes its deliberation out as the reply. "
            "\"none\" omits the field for an Ollama too old to know it."),
    _s("OLLAMA_TIMEOUT", "int", default=180, lo=10, hi=600, section="Model",
       advanced=True,
       help="Cold prefill of the tool schema can take a while on slow hardware."),
    _s("BOT_PERSONA", "text", section="Model",
       help="Who it is. Describe a voice, not a set of rules. Keep it about "
            "manner rather than actions — persona pressure in the tool prompt "
            "measurably traded against tool discipline."),
    _s("BOT_PERSONA_FLOURISH", "text", section="Model", advanced=True,
       help="An occasional extra flavour, mixed in on FLAVOR_FLOURISH_CHANCE of "
            "replies. Kept apart because a small model treats \"occasionally "
            "reference X\" as \"always\"."),
    _s("FLAVOR_ENABLED", "bool", default=True, section="Model",
       help="The short asides appended to replies."),
    _s("FLAVOR_STYLE", "choice", default="inline", choices=("inline", "line"),
       section="Model",
       help="inline runs the aside on as part of the sentence; line puts it "
            "underneath in italics, which reads as a footnote in a second voice."),
    _s("FLAVOR_MAX_LENGTH", "int", default=120, lo=40, hi=400, section="Model",
       advanced=True,
       help="Ceiling on an aside. Trimming cuts at a sentence boundary."),
    _s("FLAVOR_FLOURISH_CHANCE", "float", default=0.35, lo=0.0, hi=1.0,
       section="Model", advanced=True),

    # -- Spotify ------------------------------------------------------
    _s("SPOTIFY_CLIENT_ID", "str", section="Spotify",
       help="Register an app at developer.spotify.com. Playback control needs "
            "a Premium account."),
    _s("SPOTIFY_CLIENT_SECRET", "secret", section="Spotify"),
    _s("SPOTIFY_REDIRECT_URI", "str", default="http://127.0.0.1:8888/callback",
       section="Spotify", advanced=True,
       help="Must be a loopback address, not localhost — Spotify stopped "
            "accepting localhost."),
    _s("SPOTIFY_DEVICE_NAME", "str", section="Spotify", advanced=True,
       help="Match a Connect device by substring. Blank picks the active one."),

    # -- Voice input --------------------------------------------------
    _s("VOICE_ENABLED", "bool", default=False, section="Voice",
       help="Spoken commands, captured from the Discord client's audio output "
            "through a virtual cable. Needs the dev requirements installed."),
    _s("VOICE_DEVICE", "str", default="BlackHole 2ch", section="Voice",
       platform="darwin",
       help="The loopback device Discord plays into. macOS has none built in: "
            "`brew install --cask blackhole-2ch`."),
    _s("VOICE_WAKE_WORDS", "words", section="Voice",
       help="Spellings the transcriber actually produces for the bot's name. "
            "Blank uses the built-in list."),
    _s("VOICE_REQUIRE_PREFIX", "bool", default=False, section="Voice",
       advanced=True,
       help="Demand \"hey\"/\"ok\" before the name. Short unstressed words are "
            "the first thing a noise gate eats, so this costs wakes."),
    _s("VOICE_DRY_RUN", "bool", default=True, section="Voice",
       help="Transcribe, wake-match and log, but never act. Leave on until the "
            "log shows the wake word firing reliably and nothing else firing."),
    _s("WHISPER_MODEL", "choice", default="small", section="Voice",
       choices=("tiny", "base", "small", "medium", "large-v3", "distil-large-v3"),
       help="Measured on an M1 Max, int8: tiny 15.9x realtime, base 13.4x, "
            "small 4.8x, medium 1.7x. base is quick and gets words wrong, which "
            "is the worse failure — a mis-heard command silently does nothing."),
    _s("WHISPER_DEVICE", "choice", default="cpu", choices=("cpu", "cuda"),
       section="Voice", advanced=True,
       help="CTranslate2 has no Metal backend, so macOS is CPU whatever this says."),
    _s("WHISPER_CPU_THREADS", "int", default=6, lo=1, hi=16, section="Voice",
       advanced=True,
       help="Leave headroom: on a busy channel this runs almost continuously, "
            "and starving the audio system makes playback break up."),
    _s("VOICE_TUNING_ENABLED", "bool", default=True, section="Voice",
       help="Write down what the room says. Every utterance the microphone "
            "picks up — including conversations not addressed to the bot — is "
            "transcribed and recorded with the verdict it got. It is how you "
            "tune the wake word against a real room, and it is a record of "
            "what people said near the microphone. Turn it off and nothing is "
            "written; the bot still hears and still responds."),
    _s("VOICE_TUNING_LOG", "path", default="logs/voice-tuning.log",
       section="Voice", advanced=True,
       help="Where that record goes. Relative paths resolve next to the code."),
    _s("VOICE_TUNING_MAX_MB", "float", default=5.0, lo=0.1, hi=500.0,
       section="Voice", advanced=True,
       help="Rotate once the file passes this size, in megabytes. A busy room "
            "produces a line per utterance and this grew to 700 KB in two days, "
            "so it is bounded rather than left to run."),
    _s("VOICE_TUNING_KEEP", "int", default=3, lo=0, hi=50,
       section="Voice", advanced=True,
       help="How many rotated files to keep beside the current one. 0 keeps "
            "none, so nothing older than the current file survives."),

    # -- Speech output ------------------------------------------------
    _s("TTS_ENABLED", "bool", default=False, section="Speech", restart="bot",
       help="Whether she speaks her replies into the voice channel."),
    _s("TTS_ENGINE", "choice", default="kokoro", choices=("kokoro", "chatterbox"),
       section="Speech", restart="tts",
       help="kokoro is ~0.2s a reply and mispronounces proper nouns. chatterbox "
            "reads them correctly and takes ~8s, holding ~200% CPU while it "
            "does, which can break playback up on a loaded machine."),
    _s("TTS_VOICE", "str", default="bf_emma", section="Speech", restart="tts",
       help="Kokoro voice name. 54 are published. The first letter is the "
            "language: af_/am_ American, bf_/bm_ British, and so on. Use Test "
            "to hear one before saving it."),
    _s("TTS_LANG_CODE", "choice", default="auto", section="Speech", restart="tts",
       choices=("auto", "a", "b", "e", "f", "h", "i", "j", "p", "z"),
       help="Which pronunciation rules to read the text with. This is separate "
            "from the voice: the voice decides who it sounds like, this decides "
            "how words are said. \"auto\" takes it from the voice name's first "
            "letter, which is what you want unless you are deliberately "
            "mismatching them — a=American, b=British, e=Spanish, f=French, "
            "h=Hindi, i=Italian, j=Japanese, p=Portuguese, z=Mandarin. Japanese "
            "and Mandarin need an extra package the settings page will offer to "
            "install."),
    _s("TTS_VOICE_REF", "path", section="Speech", restart="tts",
       help="Chatterbox only. A clip of the voice to clone. Only the first ~10 "
            "seconds are read, so it should be the best ten, not the longest."),
    _s("TTS_URL", "str", default="http://127.0.0.1:8085", section="Speech",
       advanced=True),
    _s("TTS_MAX_CHARS", "int", default=600, lo=80, hi=2000, section="Speech",
       advanced=True,
       help="Long replies are where a slow engine hurts most."),
    _s("TTS_ACK_ENABLED", "bool", default=True, section="Speech",
       help="A short pre-rendered \"heard you\" the moment a wake word lands. "
            "The problem it solves is not the delay, it is the silence."),
    _s("TTS_ACK_LINES", "str", section="Speech", advanced=True,
       default="Mm. || Fine. || One moment. || If I must. || Working on it.",
       help="Separated by ||. Delete the acks directory to re-render."),
    _s("TTS_OUTPUT_DEVICE", "str", default="BlackHole 16ch", section="Speech",
       platform="darwin", restart="bot",
       help="The return cable, feeding Discord's microphone. A SECOND device, "
            "not the capture one — sharing one would feed her own voice back."),

    # -- Behaviour ----------------------------------------------------
    _s("AUTOPLAY_NEXT_EPISODE", "bool", default=True, section="Behaviour",
       help="Load the next episode when one finishes, rather than stopping."),
    _s("FREEZE_TIMEOUT", "int", default=45, lo=10, hi=300, section="Behaviour",
       advanced=True,
       help="How long playback may stall before the watchdog reloads at the "
            "last known position."),
    _s("KARAOKE_DEFAULT", "bool", default=False, section="Behaviour",
       help="Draw synced lyrics over the idle screen while music plays."),
    _s("NOWPLAYING_ENABLED", "bool", default=False, section="Behaviour",
       help="Post a message to a channel whenever the track changes."),
    _s("MUSIC_MINIMIZE_MPV", "bool", default=True, section="Behaviour",
       advanced=True,
       help="Get the player out of the way when music starts, so a shared "
            "screen shows the music app rather than a still image."),
# -- YouTube / browser fallback -----------------------------------
    _s("YOUTUBE_BROWSER_FALLBACK", "bool", default=True, section="YouTube",
       help="A link that hits YouTube's age gate opens in a real signed-in "
            "browser instead of failing. There is no playback control once it "
            "is open."),
    _s("BROWSER_PATH", "path", section="YouTube", advanced=True,
       help="Blank finds Firefox in the usual places."),
    _s("BROWSER_PROFILE", "str", default="Athena", section="YouTube",
       advanced=True,
       help="A dedicated Firefox profile, so the bot's YouTube login is its own "
            "and not whoever's is signed in."),
    _s("BROWSER_KIOSK", "bool", default=True, section="YouTube", advanced=True),
    _s("YTDL_FORMAT", "str", section="YouTube", advanced=True,
       help="Blank prefers H.264 at 1080p. Worth leaving alone: AV1 has no "
            "hardware decoder on Apple silicon before M3, and left to itself "
            "\"best\" resolves to AV1."),
    _s("YOUTUBE_TIMEOUT", "float", default=20.0, lo=5, hi=120, section="YouTube",
       advanced=True),

    # -- Languages ----------------------------------------------------
    _s("DEFAULT_AUDIO_LANG", "str", default="English", section="Languages",
       help="Preferred audio track when a file offers several."),
    _s("DEFAULT_SUBTITLE_LANG", "str", default="off", section="Languages",
       help="Preferred subtitle track. \"off\" means none."),
    _s("ANIME_AUDIO_LANG", "str", default="Japanese", section="Languages",
       advanced=True,
       help="Applied to libraries named in ANIME_LIBRARIES."),
    _s("ANIME_SUBTITLE_LANG", "str", default="English", section="Languages",
       advanced=True),

    # -- Window handling ----------------------------------------------
    _s("MUSIC_FOCUS_SPOTIFY", "bool", default=True, section="Behaviour",
       advanced=True,
       help="Bring the music app forward when music starts."),
    _s("MUSIC_MAXIMIZE_SPOTIFY", "bool", default=True, section="Behaviour",
       advanced=True,
       help="Fill the screen with it, so viewers can read the queue."),
    _s("NOWPLAYING_CHANNEL_ID", "str", section="Behaviour", advanced=True,
       help="Where now-playing messages go, if enabled."),

    # -- Voice, the rest ----------------------------------------------
    _s("VOICE_CHANNEL_ID", "str", section="Voice",
       help="The VOICE channel to sit in — not the text channel it talks in. "
            "Needed only for speaker attribution and for speaking replies."),
    _s("VOICE_TRACK_SPEAKERS", "bool", default=False, section="Voice",
       help="Name who spoke each line in the tuning log. Requires holding a "
            "voice connection, so the bot appears in the channel; it joins "
            "muted and transmits nothing."),
    _s("VOICE_WAKE_PREFIXES", "words", section="Voice", advanced=True,
       help="Accepted before the name. Blank uses the built-in list."),
    _s("VOICE_WAKE_COMPOUNDS", "list", section="Voice", advanced=True,
       help="Whole-phrase renderings for when recognition fuses the prefix into "
            "the name. Each entry is damage control for a badly-heard name."),
    _s("VOICE_SAVE_AUDIO", "path", section="Voice", advanced=True,
       help="Save each utterance as a WAV so a mis-heard command can be listened "
            "to rather than guessed at. This records everyone in the channel, so "
            "it is a tuning tool and not something to leave on."),
    _s("VOICE_SAVE_AUDIO_KEEP", "int", default=200, lo=1, hi=5000,
       section="Voice", advanced=True),
    _s("WHISPER_COMPUTE", "choice", default="int8", section="Voice",
       choices=("int8", "int8_float16", "float16", "float32"), advanced=True,
       help="int8 uses NEON on Apple silicon and is the reason CPU transcription "
            "is usable at all."),
    _s("VOICE_BEAM_SIZE", "int", default=5, lo=1, hi=10, section="Voice",
       advanced=True,
       help="Beam search rather than greedy. Utterances are seconds long and the "
            "model runs far faster than realtime, so the latency is invisible "
            "while the accuracy is not."),
    _s("VOICE_THRESHOLD", "float", default=0.003, lo=0.0, hi=1.0,
       section="Voice", advanced=True,
       help="Energy gate. The cable carries exact digital silence between "
            "utterances, so a plain gate is enough."),
    _s("VOICE_SILENCE_MS", "int", default=1000, lo=100, hi=5000,
       section="Voice", advanced=True,
       help="How much quiet ends an utterance."),
    _s("VOICE_PREROLL_MS", "int", default=500, lo=0, hi=3000,
       section="Voice", advanced=True,
       help="Audio kept from before the gate opened, so a clipped attack does "
            "not eat the wake word."),
    _s("VOICE_MIN_MS", "int", default=400, lo=50, hi=3000, section="Voice",
       advanced=True),
    _s("VOICE_MAX_S", "float", default=10.0, lo=2, hi=60, section="Voice",
       advanced=True,
       help="Anything longer is a conversation, not an instruction."),
    _s("VOICE_MAX_WORDS", "int", default=14, lo=3, hi=60, section="Voice",
       advanced=True),
    _s("VOICE_STT_TIMEOUT", "float", default=30.0, lo=5, hi=300,
       section="Voice", advanced=True),
    _s("VOICE_SPEAKER_TOLERANCE_MS", "int", default=1500, lo=0, hi=10000,
       section="Voice", advanced=True,
       help="How far outside an utterance a speaking frame still counts as "
            "belonging to it. Deliberately loose: the audio and the signalling "
            "arrive by independent paths with independent latency."),

    # -- Timeouts -----------------------------------------------------
    _s("TTS_TIMEOUT", "float", default=60.0, lo=5, hi=600, section="Speech",
       advanced=True,
       help="Raise this if using a slow engine — chatterbox takes ~8s a reply."),
    _s("TTS_ECHO_GUARD_MS", "int", default=400, lo=0, hi=3000, section="Speech",
       advanced=True,
       help="Capture stays suppressed this long past the end of playback: the "
            "tail is still moving through Discord's encoder and someone else's "
            "open mic can echo it back."),
    _s("FLAVOR_TIMEOUT", "float", default=8.0, lo=1, hi=60, section="Model",
       advanced=True,
       help="An aside is cosmetic — it is dropped rather than allowed to delay "
            "the reply."),

)


# ----------------------------------------------------------------------
# lookups
# ----------------------------------------------------------------------

BY_NAME = {s.name: s for s in SETTINGS}
SECTIONS = tuple(dict.fromkeys(s.section for s in SETTINGS))


def required() -> tuple[Setting, ...]:
    """The settings without which the bot cannot start at all."""
    return tuple(s for s in SETTINGS if s.required)


def secrets() -> tuple[Setting, ...]:
    """Settings that must never be rendered back to a UI or written to a log."""
    return tuple(s for s in SETTINGS if s.is_secret())


def for_platform(platform: str) -> tuple[Setting, ...]:
    """Settings that apply on a given sys.platform."""
    return tuple(s for s in SETTINGS if not s.platform or s.platform == platform)


def restarts_for(changed: list) -> set:
    """Which services a set of changed setting names needs bounced.

    Returns a subset of {"bot", "tts"}. An unknown name is treated as needing
    the bot, on the grounds that guessing "nothing" is the only answer that can
    silently do the wrong thing.
    """
    out = set()
    for name in changed:
        s = BY_NAME.get(name)
        out.add(s.restart if s and s.restart != "none" else "bot")
    return out - {"none"}


def validate(name: str, raw: str) -> str:
    """Return "" if `raw` is a usable value for `name`, else why it is not.

    Deliberately string-in: this validates what someone typed into a form,
    before it reaches a file the bot will try to start from.
    """
    s = BY_NAME.get(name)
    if s is None:
        return f"unknown setting {name!r}"
    raw = (raw or "").strip()
    if not raw:
        return f"{name} is required" if s.required else ""

    if s.kind in ("int", "float"):
        try:
            value = float(raw) if s.kind == "float" else int(raw)
        except ValueError:
            return f"{name} must be a{'' if s.kind == 'float' else ' whole'} number"
        if s.lo is not None and value < s.lo:
            return f"{name} must be at least {s.lo:g}"
        if s.hi is not None and value > s.hi:
            return f"{name} must be at most {s.hi:g}"
    elif s.kind == "bool":
        if raw.lower() not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
            return f"{name} must be true or false"
    elif s.kind == "choice" and raw not in s.choices:
        return f"{name} must be one of: {', '.join(c or '(blank)' for c in s.choices)}"
    elif s.kind == "path" and s.must_exist:
        import os
        if not os.path.exists(os.path.expanduser(raw)):
            return f"{name}: no such file — {raw}"
    return ""
