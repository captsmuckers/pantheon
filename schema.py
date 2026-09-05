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
    # Which speech engine and mode this setting actually does anything under.
    # Empty means "always". Otherwise a tuple of contexts: "kokoro",
    # "chatterbox", or "qwen:<mode>" where mode is base, customvoice or
    # voicedesign.
    #
    # This exists because a settings page that shows every field at once is
    # actively misleading here: on Qwen VoiceDesign, three of the five voice
    # fields do nothing, and there was no way to tell which. A setting that
    # silently applies nothing is the single most confusing thing this UI can
    # do — the same trap TTS_VOICE fell into when its restart target was wrong.
    applies: tuple = ()
    # Human labels for `choices`, in the same order. A dropdown whose options
    # are raw HuggingFace repo paths is unreadable, and worse than unreadable
    # here: "Base" means CLONING and "CustomVoice" means Qwen's OWN nine
    # voices, which reads as exactly the opposite of what it does. Anything
    # without a label falls back to the value.
    choice_labels: tuple = ()

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
    _s("BOT_NAME", "str", default="Athena", section="Discord", restart="bot",
       help="What she is called. Sets the mpv window title, the device Plex "
            "sees, the Firefox profile and the spoken wake word, all at once. "
            "Renaming is not free: the wake word was chosen by measurement — "
            "\"Athena\" was heard 26 times in 28 against 6 in 9 for \"Jarvis\" "
            "and 0 in 4 for \"Lilith\", because Discord's voice gate clips the "
            "opening consonant and names starting on an open vowel survive it. "
            "A name beginning with a hard consonant will be missed often."),
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
    _s("MPV_WINDOW_TITLE", "str", section="Player",
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
    _s("TTS_ENGINE", "choice", default="kokoro",
       choices=("kokoro", "chatterbox", "qwen"),
       section="Speech", restart="tts",
       help="Which speech engine runs. This decides which of the fields below "
            "do anything.  \u2022 kokoro: 54 fixed voices, ~0.4s a line, the "
            "fastest by far. Cannot copy a voice. Mispronounces proper nouns.  "
            "\u2022 qwen: ~1-2s a line. Can use 9 built-in voices, invent one "
            "from a written description, or copy one from a recording \u2014 "
            "pick which with 'Qwen model' below. Recommended.  "
            "\u2022 chatterbox: copies a voice from a recording. ~2.4s a line "
            "and slower on short ones than long. Kept for comparison; qwen "
            "does the same job faster."),
    # The checkpoint doubles as the mode switch because Qwen publishes Base,
    # CustomVoice and VoiceDesign as separate weights - you cannot flip between
    # them without loading a different model, so a separate mode setting could
    # only ever disagree with this one.
    # A dropdown, not a text box: this is the mode switch, it is the one Qwen
    # setting you actually change, and a mistyped repo id fails ~30s later
    # inside a model download rather than at save time. An id set by hand in
    # .env still shows, as "not a listed choice".
    _s("TTS_QWEN_MODEL", "choice",
       default="mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
       choices=(
           "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
           "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
           "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
           "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
           "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
       ),
       choice_labels=(
           "One of Qwen's 9 ready-made voices  (better quality)",
           "One of Qwen's 9 ready-made voices  (faster)",
           "A voice from a written description",
           "Clone a voice from a recording  (better quality)",
           "Clone a voice from a recording  (faster)",
       ),
       applies=("qwen:base", "qwen:customvoice", "qwen:voicedesign"),
       section="Speech", restart="tts",
       help="How the Qwen voice is chosen. The name says which: "
            "\u2022 CustomVoice \u2014 pick one of 9 ready-made voices.  "
            "\u2022 VoiceDesign \u2014 describe the voice you want in words.  "
            "\u2022 Base \u2014 copy a voice from a recording you upload.  "
            "1.7B sounds slightly better than 0.6B and is about 20% slower; "
            "both are comfortably faster than real time. Changing this loads "
            "different weights, so it takes about 30 seconds."),
    # The bot, not the speech service: it sends the description on every
    # synthesize call, exactly as it does the voice name. That keeps a voice
    # change off the ~30s Qwen checkpoint reload — the weights are the same
    # whatever you describe.
    _s("TTS_VOICE_DESIGN", "str", default="", section="Speech", restart="bot",
       applies=("qwen:voicedesign",),
       help="Describe the voice you want, in plain English \u2014 age, accent, "
            "pitch and manner all work: \"a woman in her thirties with a low, "
            "dry, aristocratic English voice, bored and faintly contemptuous\". "
            "Typing here changes nothing on its own: press Test this "
            "description to hear it in your browser, then Save and restart the "
            "bot for her to use it in Discord. The wording matters more than "
            "you would expect and is hard to predict, so expect to test a few."),
    _s("TTS_VOICE_REF_TEXT", "str", default="", section="Speech", restart="tts",
       applies=("qwen:base",),
       help="What is actually said in the recording above. Filled in for you "
            "when you upload \u2014 check it, because it is transcribed "
            "automatically and is occasionally wrong. Qwen copies a voice "
            "noticeably better when it has both the audio and the words; "
            "leaving this blank still works, it just sounds less like the "
            "original and nothing warns you."),
    # The bot, not the speech service: it sends the voice name on every
    # synthesize call, and the server only falls back to its own --voice when
    # a request omits one. Restarting the speech service therefore changes
    # nothing, which looked exactly like the setting being ignored.
    _s("TTS_VOICE", "str", default="bf_emma", section="Speech", restart="bot",
       applies=("kokoro", "qwen:customvoice"),
       help="Which ready-made voice to use. Under Qwen CustomVoice these are "
            "Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna and "
            "Sohee \u2014 press Browse voices to see them with descriptions. "
            "Under Kokoro they look like bf_emma or af_heart, where the first "
            "letter is the language and the second the sex. The two sets are "
            "not interchangeable. Use Test to hear one before saving."),
    _s("TTS_LANG_CODE", "choice", default="auto", section="Speech",
       restart="tts", applies=("kokoro",),
       choices=("auto", "a", "b", "e", "f", "h", "i", "j", "p", "z"),
       help="Kokoro only. Which pronunciation rules to read the text with \u2014 "
            "separate from the voice: the voice decides who it sounds like, "
            "this decides how words are said. \"auto\" takes it from the "
            "voice name's first letter, which is what you want unless you are "
            "deliberately reading one language in another's accent. Qwen has "
            "no equivalent and ignores this."),
    _s("TTS_VOICE_REF", "path", section="Speech", restart="tts",
       applies=("qwen:base", "chatterbox"),
       help="A recording of the voice to copy. Press Upload a clip and pick "
            "any audio or video file \u2014 it is trimmed to 10 seconds from "
            "the start offset, converted and levelled for you. Only the first "
            "6 seconds decide who it sounds like, so give it the best 10, one "
            "speaker, no music, in the tone you want back. A compressed or "
            "echoey source is copied faithfully, including the compression."),
    _s("TTS_URL", "str", default="http://127.0.0.1:8085", section="Speech",
       advanced=True),
    _s("TTS_MAX_CHARS", "int", default=600, lo=80, hi=2000, section="Speech",
       advanced=True,
       help="Long replies are where a slow engine hurts most."),
    _s("TTS_ACK_ENABLED", "bool", default=True, section="Speech",
       help="A short pre-rendered \"heard you\" the moment a wake word lands. "
            "The problem it solves is not the delay, it is the silence."),
    # No longer advanced=True. It was hidden behind the disclosure, which is
    # part of why "how do I add a phrase" was hard to answer — the setting is
    # ordinary and personal, not an internal knob.
    _s("TTS_ACK_LINES", "str", section="Speech", restart="bot",
       default="Mm. || Fine. || One moment. || If I must. || Working on it.",
       help="Short things she says the instant she hears you, so a request is "
            "not met with silence while she thinks. Separate them with || and "
            "press Save, then restart the bot — that is all; they are rendered "
            "at startup and cached automatically. Editing or adding a line "
            "renders just that one, and clips from a previous voice are "
            "cleaned up on the next successful start, so there is nothing to "
            "delete by hand."),
    _s("STREAM_AUDIO_ENABLED", "bool", default=False, section="Speech",
       restart="none",
       help="Route media audio through a clean virtual device so a Discord "
            "screen share captures it unprocessed. Without this, macOS renders "
            "through the built-in speaker route, which applies loudness and "
            "protection processing tuned for small drivers — the share then "
            "captures that, and music arrives bass-light with the level "
            "pumping. Off by default because it changes the machine's default "
            "output device. Applied by scripts/setup-stream-audio.py."),
    _s("STREAM_AUDIO_DEVICE", "str", default="BlackHole 64ch", section="Speech",
       restart="none", advanced=True,
       help="The clean virtual device to route media through. Deliberately a "
            "THIRD device, separate from the two the voice pipeline uses — "
            "reusing those would mix music into what the transcriber hears and "
            "wreck wake-word detection. Install with: "
            "brew install --cask blackhole-64ch"),
    _s("STREAM_AUDIO_MONITOR", "str", section="Speech", restart="none",
       advanced=True,
       help="Paired with the clean device so anyone at the machine still hears "
            "sound, and so the pair has a real hardware clock to follow. Blank "
            "detects the built-in output automatically."),
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
    _s("BROWSER_PROFILE", "str", section="YouTube",
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
    # -- Images -------------------------------------------------------
    #
    # Generation runs on another machine; this side is only a client. All of
    # these restart the bot, not some image service, because the bot is what
    # reads them — the same trap TTS_VOICE fell into.
    _s("IMAGE_ENABLED", "bool", default=False, section="Images", restart="bot",
       help="Let her draw. Needs a ComfyUI server reachable at the address "
            "below; off until there is one, so the command is not offered "
            "when it cannot work."),
    _s("IMAGE_URL", "str", default="http://127.0.0.1:8188", section="Images",
       restart="bot",
       help="ComfyUI's address. It binds 127.0.0.1 by default, so a server on "
            "another machine must be started with --listen for this to reach "
            "it."),
    _s("IMAGE_CHECKPOINT", "str", section="Images", restart="bot",
       help="Model file to load, as ComfyUI names it (for example "
            "sd_xl_base_1.0.safetensors). Leave blank to use whatever the "
            "workflow was exported with — which is the safer default, since "
            "which checkpoints exist is a fact about the other machine."),
    _s("IMAGE_STEPS", "int", default=25, lo=1, hi=150, section="Images",
       restart="bot",
       help="Sampler steps. More is slower, not reliably better; 20-30 is the "
            "usual range for SDXL."),
    _s("IMAGE_WIDTH", "int", default=1024, lo=256, hi=2048, section="Images",
       restart="bot",
       help="Width in pixels. SDXL was trained at 1024 and gets visibly worse "
            "far below it."),
    _s("IMAGE_HEIGHT", "int", default=1024, lo=256, hi=2048, section="Images",
       restart="bot", help="Height in pixels."),
    _s("IMAGE_NEGATIVE", "str",
       default="blurry, low quality, watermark, text, deformed",
       section="Images", restart="bot",
       help="Applied to every image unless a request overrides it."),
    _s("IMAGE_CFG", "float", default=7.0, lo=1.0, hi=30.0, section="Images",
       restart="bot", advanced=True,
       help="How hard the sampler is pushed toward the prompt. Above about 12 "
            "images start to look burnt."),
    _s("IMAGE_TIMEOUT", "float", default=180.0, lo=30, hi=1800,
       section="Images", restart="bot", advanced=True,
       help="How long to wait for one image. Sized for the hardware, not for "
            "patience: SDXL on an 8GB card takes tens of seconds, a quantised "
            "FLUX with offloading takes minutes."),
    _s("IMAGE_DENOISE", "float", default=0.65, lo=0.1, hi=1.0, section="Images",
       restart="bot",
       help="How far an edit may travel from the picture it started with. 1.0 "
            "discards the original entirely; below about 0.4 the prompt has no "
            "room to change anything. 0.65 restyles while keeping the "
            "composition. A local change that must leave the rest of the frame "
            "alone needs inpainting, not a lower number here."),
    _s("IMAGE_WORKFLOW_IMG2IMG", "str",
       default="workflows/sdxl-img2img.json", section="Images",
       restart="bot", advanced=True,
       help="Used instead of the workflow below when a picture is attached to "
            "the request. Needs a LoadImage node; the source image sets the "
            "size, so IMAGE_WIDTH/HEIGHT do not apply."),
    _s("IMAGE_WORKFLOW", "str", default="workflows/sdxl.json", section="Images",
       restart="bot", advanced=True,
       help="A ComfyUI graph in API format — not the format the editor saves "
            "by default. See workflows/README.md."),

    _s("TTS_NORMALIZE", "bool", default=True, section="Speech", restart="bot",
       help="Level her voice to a normal speaking level before it goes down "
            "the cable. Kokoro delivers about -23 dBFS; broadcast speech sits "
            "near -18, and the quiet end of Kokoro's range is where a voice "
            "gate starts cutting syllables. Cannot clip — a -1 dBFS peak "
            "ceiling always overrides the target."),
    _s("TTS_LEVEL_DBFS", "float", default=-18.0, lo=-40.0, hi=-6.0,
       section="Speech", restart="bot", advanced=True,
       help="The RMS level to aim for, measured over speech only. Louder than "
            "about -12 leaves no headroom for the peaks."),
    _s("CHAT_MAX_TOKENS", "int", default=160, lo=40, hi=600, section="Model",
       restart="bot", advanced=True,
       help="Ceiling on a conversational reply. These get spoken, so length "
            "is a listening cost: 260 tokens is roughly thirty seconds of "
            "speech. The persona asks for a few lines; this enforces it when "
            "the model ignores that."),
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
