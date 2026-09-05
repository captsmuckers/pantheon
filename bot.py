"""Discord front end.

Run this file. Everything else is a module.
"""

import asyncio
import logging
import os
import sys
import time
from typing import Optional

import discord
from discord.ext import commands

import config
from brain import (
    Brain,
    Choice,
    Controls,
    HELP_SECTIONS,
    MusicChoice,
    _format_queue,
)
from library import Library
from player import Player
from spotify import SpotifyController

# Both import cleanly without numpy/sounddevice/faster-whisper — production has
# none of them, and start() returns None there rather than raising.
import voice
import speech
import speakers
import imagegen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("athena")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!!", intents=intents)

lib: Library | None = None
player: Player | None = None
brain: Brain | None = None
spotify: SpotifyController | None = None

# Cold start runs once, even if the gateway reconnects while it's still going.
_startup_lock = asyncio.Lock()
_started = False

# Longest a single chat request may take before we answer with an apology
# instead of nothing. Generous: a tool-calling round trip on a local model can
# legitimately take a while.
# A turn is abandoned after this. It has to outlast anything a turn can
# legitimately do, and the slowest of those is an image: imagegen waits
# IMAGE_TIMEOUT for one, so a shorter ceiling here kills the turn while the GPU
# is still working and throws away a picture that then finishes into nothing.
# Observed: an edit from a 6.7MB reference was abandoned at 120s having never
# been delivered. Derived rather than written down twice, so the two cannot
# drift apart again.
REPLY_TIMEOUT = (
    int(config.IMAGE_TIMEOUT) + 30 if config.IMAGE_ENABLED else 120
)


# ----------------------------------------------------------------------
# status broadcasting
# ----------------------------------------------------------------------

class StatusBroadcaster:
    """Three surfaces, three very different rate limits.

    Presence is cheap and updates on every change. The now-playing message is
    edited within a couple of seconds. The channel topic is throttled hard,
    because Discord allows roughly two channel edits per ten minutes.
    """

    TOPIC_COOLDOWN = 330  # seconds

    def __init__(self):
        self.last_presence = None
        self.last_topic = None
        self.last_topic_at = 0.0
        self.pending_topic = None
        self.pending_status = None
        self.rendered = None
        self.message: discord.Message | None = None

    async def update(self, plr: Player) -> None:
        status = plr.status()
        self.pending_status = status

        if not status["playing"]:
            presence, topic = "Nothing", "Idle"
        else:
            verb = "Paused" if status["paused"] else "Now playing"
            presence = status["title"]
            topic = f"{verb}: {status['title']}"

        if presence != self.last_presence:
            self.last_presence = presence
            try:
                await bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching, name=presence
                    )
                )
            except Exception as exc:
                log.debug("Presence update failed: %s", exc)

        self.pending_topic = topic
        await self.flush_message()
        await self.flush_topic()

    # ---------- now playing message ----------

    def _render(self, status: dict) -> discord.Embed:
        if not status["playing"]:
            embed = discord.Embed(title="Nothing playing", color=0x4E5058)
            embed.set_footer(text="Type a title to start something")
            return embed

        embed = discord.Embed(
            title=status["title"],
            color=0xE5A00D if not status["paused"] else 0x8A8A8A,
        )
        state = "Paused" if status["paused"] else "Playing"
        if status.get("speed", 1.0) != 1.0:
            state += f" · {status['speed']:g}x"
        embed.add_field(name="Status", value=state, inline=True)
        embed.add_field(
            name="Audio / Subs",
            value=f"{status['audio_language']} / {status['subtitle_language']}",
            inline=True,
        )
        if status["queue_length"]:
            embed.add_field(
                name="Queued", value=str(status["queue_length"]), inline=True
            )
        if status.get("library"):
            embed.set_footer(text=status["library"])
        return embed

    def _signature(self, status: dict) -> str:
        """What we consider a real change. Deliberately excludes elapsed time,
        so the message isn't edited every few seconds forever."""
        return "|".join(
            str(status.get(k))
            for k in (
                "playing", "paused", "title", "queue_length",
                "speed", "audio_language", "subtitle_language",
            )
        )

    async def flush_message(self) -> None:
        if not config.NOWPLAYING_ENABLED or self.pending_status is None:
            return
        signature = self._signature(self.pending_status)
        if signature == self.rendered:
            return
        channel = bot.get_channel(config.NOWPLAYING_CHANNEL_ID)
        if channel is None:
            return
        embed = self._render(self.pending_status)
        try:
            if self.message is None:
                self.message = await channel.send(embed=embed)
            else:
                await self.message.edit(embed=embed)
            self.rendered = signature
        except discord.NotFound:
            self.message = None  # someone deleted it; recreate next time
        except Exception as exc:
            log.debug("Now playing update failed: %s", exc)

    async def adopt_existing_message(self) -> None:
        """Reuse the previous now-playing message across restarts instead of
        leaving a trail of dead embeds in the channel."""
        if not config.NOWPLAYING_ENABLED:
            return
        channel = bot.get_channel(config.NOWPLAYING_CHANNEL_ID)
        if channel is None:
            return
        try:
            async for msg in channel.history(limit=30):
                if msg.author.id == bot.user.id and msg.embeds:
                    self.message = msg
                    return
        except Exception as exc:
            log.debug("Could not adopt an existing status message: %s", exc)

    # ---------- channel topic ----------

    async def flush_topic(self) -> None:
        """Discord allows roughly two channel edits per ten minutes, so this
        deliberately runs far behind. The now-playing message is the live one."""
        if not config.STATUS_CHANNEL_ID or self.pending_topic is None:
            return
        if self.pending_topic == self.last_topic:
            return
        if time.monotonic() - self.last_topic_at < self.TOPIC_COOLDOWN:
            return  # a later flush will pick it up

        channel = bot.get_channel(config.STATUS_CHANNEL_ID)
        if channel is None:
            return
        try:
            if isinstance(channel, discord.VoiceChannel):
                await channel.edit(status=self.pending_topic)
            else:
                await channel.edit(topic=self.pending_topic)
            self.last_topic = self.pending_topic
            self.last_topic_at = time.monotonic()
        except discord.Forbidden:
            log.warning("Missing Manage Channels permission for the status channel")
        except Exception as exc:
            log.debug("Topic update failed: %s", exc)


status_broadcaster = StatusBroadcaster()


async def status_flush_loop():
    """Catches anything a throttle deferred, and keeps the message honest if an
    edit failed transiently."""
    while True:
        await asyncio.sleep(15)
        try:
            await status_broadcaster.flush_message()
            await status_broadcaster.flush_topic()
        except Exception:
            log.exception("Status flush failed")


# ----------------------------------------------------------------------
# disambiguation picker
# ----------------------------------------------------------------------

class ExpiringView(discord.ui.View):
    """A picker that visibly expires instead of going quietly dead.

    Discord leaves the dropdown looking live after the view times out, so
    clicking it just fails. Assign `.message` after sending and the controls
    grey out on expiry.
    """

    def __init__(self, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(content="That picker expired.", view=self)
        except Exception as exc:
            log.debug("Could not expire a picker: %s", exc)


class MediaChoiceSelect(discord.ui.Select):
    def __init__(self, choice: Choice):
        self.choice = choice
        options = [
            discord.SelectOption(
                label=c.label()[:100],
                value=str(c.rating_key),
                description=(
                    ("Movie" if c.kind == "movie" else "Show")
                    + (f" · {c.library}" if c.library else "")
                )[:100],
            )
            for c in choice.candidates
        ]
        verb = "play" if choice.action == "play" else "queue"
        super().__init__(placeholder=f"Pick which one to {verb}...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # Routed through Controls rather than straight at the player: picking
        # from here has to park the music and claim the screen exactly like a
        # typed request does, which calling player.play() directly skipped.
        result = await brain.controls.play_media_option(
            int(self.values[0]), action=self.choice.action
        )
        await interaction.edit_original_response(content=str(result)[:1900], view=None)
        self.view.stop()


class MediaChoiceView(ExpiringView):
    def __init__(self, choice: Choice):
        super().__init__(timeout=120)
        self.add_item(MediaChoiceSelect(choice))


class MusicChoiceSelect(discord.ui.Select):
    KIND_LABEL = {
        "track": "Song", "album": "Album",
        "artist": "Artist", "playlist": "Playlist",
    }

    def __init__(self, choice: MusicChoice):
        self.choice = choice
        options = [
            discord.SelectOption(
                label=c["label"][:100],
                value=str(index),
                description=self.KIND_LABEL.get(c["kind"], c["kind"])[:100],
            )
            for index, c in enumerate(choice.candidates)
        ]
        verb = "queue" if choice.action == "queue" else "play"
        super().__init__(placeholder=f"Which one to {verb}?", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        option = self.choice.candidates[int(self.values[0])]
        result = await brain.controls.play_music_option(option, action=self.choice.action)
        await interaction.edit_original_response(content=str(result)[:1900], view=None)
        self.view.stop()


class MusicChoiceView(ExpiringView):
    def __init__(self, choice: MusicChoice):
        super().__init__(timeout=120)
        self.add_item(MusicChoiceSelect(choice))


def build_picker(result) -> ExpiringView:
    return MediaChoiceView(result) if isinstance(result, Choice) else MusicChoiceView(result)


async def _send_result(target, result, *, reply: bool = True, prefix: str = ""):
    """Send a Controls result, rendering a picker if it needs one.

    `prefix` is prepended to the message body rather than sent separately, so a
    spoken command can show what was heard without splitting the picker off
    into a second message.
    """
    if isinstance(result, (Choice, MusicChoice)):
        view = build_picker(result)
        body = f"{prefix}{result.text()}"[:1900]
        if reply:
            sent = await target.reply(body, view=view, mention_author=False)
        else:
            sent = await target.send(body, view=view)
        view.message = sent  # so on_timeout can grey the dropdown out
        return sent

    if isinstance(result, imagegen.Picture):
        # The image is the reply. Discord wants a fresh file object per send,
        # so this is built here rather than carried on the result — a retry
        # with an already-consumed file posts an empty attachment.
        import io as _io

        upload = discord.File(_io.BytesIO(result.data), filename=result.filename)
        body = f"{prefix}{result.text()}"[:1900]
        if reply:
            return await target.reply(body, file=upload, mention_author=False)
        return await target.send(body, file=upload)
    text = f"{prefix}{result}"[:1900]
    if reply:
        return await target.reply(text, mention_author=False)
    return await target.send(text)


# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------

async def post_notice(message: str) -> None:
    """Surface a player-level problem in the channel.

    Going quietly idle because a file wouldn't play looks identical to the bot
    being broken, so these get said out loud rather than only logged.
    """
    channel = bot.get_channel(config.ALLOWED_CHANNEL_ID)
    if channel is None:
        return
    try:
        await channel.send(message[:1900])
    except Exception as exc:
        log.debug("Could not post notice: %s", exc)


async def library_refresh_loop() -> None:
    """Keep the title cache fresh off the hot path.

    Startup loads the cache from disk, so the first pass here is what actually
    checks Plex. Doing it on a timer means a changed library is refetched in
    the background rather than inside whichever search happens to notice.
    """
    while True:
        try:
            await asyncio.to_thread(lib.revalidate)
        except Exception:
            log.exception("Library revalidation failed")
        await asyncio.sleep(Library.REFRESH_INTERVAL)


async def _connect_spotify(controller: SpotifyController) -> None:
    try:
        await asyncio.to_thread(controller.connect)
    except Exception:
        log.exception("Spotify connect failed — music commands stay unavailable")


def _show_player_window() -> None:
    """Un-minimise mpv and bring it forward. Blocking; call in a thread.

    Deliberately tolerant: mpv may not have created its window yet on a slow
    start, and failing to raise a window is never a reason to abort startup.
    """
    try:
        import wm
        hwnd = wm.find_window(title=config.MPV_WINDOW_TITLE)
        if not hwnd:
            log.debug("No mpv window to raise yet")
            return
        wm.restore(hwnd)
        wm.bring_to_front(hwnd)
        log.info("Idle screen on display")
    except Exception:
        log.debug("Could not raise the mpv window", exc_info=True)


async def _handle_voice_command(text: str):
    """Run a spoken command through exactly the path a typed one takes.

    The wake phrase is already stripped by voice.py, so what arrives here looks
    identical to a chat message — which matters, because fast_match() anchors
    its patterns at the start of the string and would miss every one of them if
    the name were still attached.
    """
    if brain is None:
        return None
    log.info("<- (voice): %r", text[:120])
    try:
        result = await asyncio.wait_for(brain.handle(text), timeout=REPLY_TIMEOUT)
    except asyncio.TimeoutError:
        log.error("Voice command %r timed out after %ss", text[:60], REPLY_TIMEOUT)
        result = "That took too long and I gave up."
    except Exception:
        log.exception("Voice command %r failed", text[:60])
        result = "Something went wrong handling that."
    log.info("-> (voice): %r", str(result)[:120])

    # Posted here rather than handed back for voice.py to announce, because an
    # ambiguous request returns a Choice object and only _send_result knows how
    # to turn one into a dropdown. Stringifying it — which is what a plain
    # notify callback does — produced a numbered list nobody could click, and
    # answering "2" started a fresh search for the title "2".
    channel = bot.get_channel(config.ALLOWED_CHANNEL_ID)
    if channel is not None:
        try:
            await _send_result(channel, result, reply=False,
                               prefix=f"\N{STUDIO MICROPHONE} `{text}`\n")
        except Exception:
            log.exception("Could not post the voice result")

    # Spoken only for spoken commands. Answering a typed message out loud would
    # talk over a room that never asked. This is also the case that motivated
    # speech at all: an ambiguous request posts a dropdown, and without hearing
    # "which one?" it just looks like she ignored you.
    if isinstance(result, (Choice, MusicChoice)):
        spoken = result.text()
    elif isinstance(result, imagegen.Picture):
        # The image is already in the channel. Speaking str(result) read the
        # generated prompt aloud — 8.2 seconds of style keywords describing
        # something everyone could already see.
        spoken = result.spoken()
    else:
        spoken = str(result)
    await speech.say(spoken)

    # Returned only so the tuning log records what she said back.
    return str(result)


async def _start_services() -> None:
    global lib, player, brain, spotify

    loop = asyncio.get_running_loop()
    lib = await asyncio.to_thread(Library)
    player = Player(lib, loop)
    player.on_change = status_broadcaster.update
    player.on_notice = post_notice
    await asyncio.to_thread(player.start)
    player.start_background_tasks()
    # Put the idle screen on screen rather than trusting mpv to arrive visible.
    # A new process inherits its parent's show-window state, so mpv launched
    # from a hidden or minimised console comes up minimised — and then the idle
    # screen is simply absent with nothing to say why.
    #
    # Both halves are needed. SW_RESTORE returns the window but not mpv's own
    # fullscreen state, which is separate — restoring alone leaves it windowed
    # with the taskbar across the bottom.
    await asyncio.to_thread(_show_player_window)
    await player.ensure_fullscreen()
    await status_broadcaster.adopt_existing_message()
    loop.create_task(status_flush_loop())
    loop.create_task(library_refresh_loop())
    await status_broadcaster.update(player)

    # Constructing the controller is free; connect() can block for a long time
    # on the first run, because it opens a browser for the OAuth handshake and
    # waits. So the brain is wired up immediately and Spotify warms up behind
    # it — otherwise every command is dead until someone finishes an OAuth flow
    # nobody knew was waiting.
    spotify = SpotifyController()
    brain = Brain(Controls(player, lib, spotify))
    if spotify.enabled:
        loop.create_task(_connect_spotify(spotify))

    # Spoken commands, if configured. Deliberately last and deliberately
    # non-fatal: voice is an accessory, and a bot that refuses to boot because
    # a sound device moved is worse than one that cannot hear. start() handles
    # its own failures and returns None.
    # Speech first, so the listener can be handed its suppression hook.
    await speech.start()

    # Joins the voice channel to learn who is talking, for the tuning log
    # only. Before the listener, so the first utterance can already be
    # attributed rather than logged as an unknown speaker.
    await speakers.start(bot)

    # No notify callback: _handle_voice_command posts its own result, because
    # only it can render a Choice as a real dropdown. suppress_when stops her
    # transcribing her own voice as it returns through someone else's mic.
    await voice.start(handler=_handle_voice_command,
                      suppress_when=speech.is_speaking,
                      attribute=speakers.who_spoke,
                      acknowledge=speech.ack)

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands globally", len(synced))
        # ...and again to the one guild we serve, because a GLOBAL sync can
        # take up to an hour to reach clients. Until it does, a newly added
        # command does not exist as far as Discord is concerned: typing it
        # sends "/draw ..." as an ordinary message, which on_message then
        # ignores for starting with a slash, and the whole thing looks broken
        # while the log cheerfully reports a successful sync. A guild sync is
        # immediate, and a guild command shadows the global one of the same
        # name rather than duplicating it.
        channel = bot.get_channel(config.ALLOWED_CHANNEL_ID)
        guild = getattr(channel, "guild", None) or (
            bot.guilds[0] if bot.guilds else None)
        if guild is not None:
            bot.tree.copy_global_to(guild=guild)
            here = await bot.tree.sync(guild=guild)
            log.info("Synced %d slash commands to %s — usable immediately",
                     len(here), guild.name)
        else:
            log.warning("No guild to sync to; new slash commands may take "
                        "up to an hour to appear")
    except Exception:
        log.exception("Slash command sync failed")

    log.info("Ready. Talk to me in channel %s", config.ALLOWED_CHANNEL_ID)


async def _teardown_partial_start() -> None:
    """Undo a startup that died halfway, so a retry starts from nothing."""
    global lib, player, brain, spotify
    try:
        await voice.stop()
    except Exception:
        log.debug("Could not stop voice capture", exc_info=True)
    try:
        await speech.stop()
    except Exception:
        log.debug("Could not stop speech output", exc_info=True)
    try:
        await speakers.stop()
    except Exception:
        log.debug("Could not leave the voice channel", exc_info=True)
    if player is not None:
        try:
            await player.shutdown()
        except Exception:
            log.debug("Could not clean up the partial player", exc_info=True)
    lib = player = brain = spotify = None


@bot.event
async def on_ready():
    global _started
    log.info("Connected to Discord as %s", bot.user)

    # on_ready fires again on every gateway reconnect, and the old guard
    # (`if player is not None`) only closed after the Library scan finished —
    # a reconnect during that window started a second Library, a second mpv and
    # a duplicate set of background tasks.
    async with _startup_lock:
        if _started:
            return
        _started = True
        try:
            await _start_services()
        except Exception:
            # Let a later reconnect try again rather than sit here half-built —
            # but tear down whatever did get created first, or the retry
            # abandons a running mpv and starts a second one.
            log.exception("Startup failed — will retry on the next reconnect")
            await _teardown_partial_start()
            _started = False


# Discord allows more than this, but a reference is going over the LAN to be
# VAE-encoded on an 8GB card; a 25MB phone photo is all cost and no benefit.
MAX_REFERENCE_BYTES = 12 * 1024 * 1024


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != config.ALLOWED_CHANNEL_ID:
        return
    text = message.content.strip()
    # Don't steal other bots' commands or slash-command echoes.
    if not text or text[0] in "!/.?$%&>":
        # Logged, because a silently ignored message is indistinguishable from
        # a broken bot when you're looking at the log afterwards.
        log.info("Ignoring message from %s: %r", message.author.display_name,
                 (message.content or "<empty>")[:60])
        return
    if brain is None:
        await message.reply("Still starting up, give me a second.", mention_author=False)
        return

    # A picture posted with the message is something to work FROM, not
    # decoration. It has to be picked up here because the model never sees the
    # message — it only writes the prompt — so the bytes travel out of band.
    # Always set, including to None: leaving a previous reference in place
    # would silently edit the last picture somebody posted.
    picture = next((a for a in message.attachments
                    if (a.content_type or "").startswith("image/")), None)
    if picture is not None and config.IMAGE_ENABLED:
        if picture.size > MAX_REFERENCE_BYTES:
            imagegen.set_reference(None)
            await message.reply(
                f"That picture is {picture.size / 1024**2:.0f}MB — too big to "
                "work from. Under "
                f"{MAX_REFERENCE_BYTES // 1024**2}MB, please.",
                mention_author=False)
            return
        try:
            imagegen.set_reference(await picture.read(), picture.filename)
            log.info("   with reference %s (%.0f KB)",
                     picture.filename, picture.size / 1024)
        except Exception:
            # Not fatal: fall through and treat it as an ordinary request
            # rather than refusing the message outright.
            log.warning("Could not read the attached picture", exc_info=True)
            imagegen.set_reference(None)
    else:
        imagegen.set_reference(None)

    log.info("<- %s: %r", message.author.display_name, text[:120])
    try:
        async with message.channel.typing():
            # A hard ceiling on the whole turn. Without it a wedged model call
            # leaves the request hanging with no reply and no log line, which
            # looks exactly like the bot having died.
            reply = await asyncio.wait_for(brain.handle(text), timeout=REPLY_TIMEOUT)
    except asyncio.TimeoutError:
        log.error("Gave up on %r after %ss", text[:60], REPLY_TIMEOUT)
        await message.reply(
            "That took too long and I gave up. Try again, or use a slash command.",
            mention_author=False,
        )
        return
    except Exception:
        # on_message swallowing an exception is the other way a request can
        # vanish without trace.
        log.exception("Handling %r failed", text[:60])
        await message.reply("Something went wrong handling that.", mention_author=False)
        return

    log.info("-> %r", str(reply)[:120])
    await _send_result(message, reply)


# ----------------------------------------------------------------------
# slash commands (kept as a reliable fallback)
# ----------------------------------------------------------------------

async def _guard(interaction: discord.Interaction) -> bool:
    """Gate a slash command, and say why when it's refused.

    Returning silently makes Discord show "the application did not respond"
    after three seconds, which reads as a crash rather than a refusal. Also
    checks brain, not just player: the handlers below reach through
    brain.controls, and brain is the later of the two to exist.
    """
    if interaction.channel_id != config.ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            "I only listen in the media channel.", ephemeral=True
        )
        return False
    if player is None or brain is None:
        await interaction.response.send_message(
            "Still starting up — give me a moment.", ephemeral=True
        )
        return False
    return True


async def _followup(interaction: discord.Interaction, result) -> None:
    """Send a Controls result as an interaction followup, pickers included."""
    if isinstance(result, (Choice, MusicChoice)):
        view = build_picker(result)
        view.message = await interaction.followup.send(
            result.text(), view=view, wait=True
        )
        return
    if isinstance(result, imagegen.Picture):
        # Same reason as _send_result: a fresh file object per send, because a
        # consumed one uploads as an empty attachment.
        import io as _io

        await interaction.followup.send(
            result.text()[:1900],
            file=discord.File(_io.BytesIO(result.data), filename=result.filename),
        )
        return
    await interaction.followup.send(str(result)[:1900])


@bot.tree.command(name="voices", description="List the saved voices /tts can use")
async def slash_voices(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    voices = speech.saved_voices()
    if not voices:
        await interaction.response.send_message(
            "No saved voices yet. Add one in the control panel's voice lab.")
        return
    lines = "\n".join(f"`{name}` — {label}" for name, label, _ in voices)
    await interaction.response.send_message(
        f"**Saved voices** ({len(voices)})\n{lines}\n\n"
        f"Use `/tts <voice> <phrase>`.")


async def _voice_choices(interaction: discord.Interaction, current: str):
    """Autocomplete from the live library.

    Read per keystroke rather than cached, so a voice added in the panel is
    offered immediately without restarting the bot. Discord caps this at 25.
    """
    current = (current or "").lower()
    out = []
    for name, label, _ in speech.saved_voices():
        if current in name.lower() or current in label.lower():
            out.append(discord.app_commands.Choice(name=label[:100], value=name))
        if len(out) >= 25:
            break
    return out


@bot.tree.command(name="tts", description="Say something in one of the saved voices")
@discord.app_commands.autocomplete(voice=_voice_choices)
async def slash_tts(interaction: discord.Interaction, voice: str, phrase: str):
    """Deliberately open to everyone in the channel — it is a toy, and the
    channel is already trusted enough to control playback.

    The voice is matched against the library rather than used as a path: this
    argument comes from a person typing, and the only thing it may ever select
    is a clip already saved here.
    """
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    match = next((v for v in speech.saved_voices()
                  if v[0].lower() == voice.lower().strip()), None)
    if match is None:
        known = ", ".join(f"`{n}`" for n, _, _ in speech.saved_voices()[:12]) or "none saved"
        await _followup(interaction, f"No saved voice called `{voice}`. Try: {known}")
        return
    problem = await speech.say_as(phrase, match[2])
    await _followup(interaction,
                    problem or f"*{match[1]}:* {phrase}")


@bot.tree.command(name="play", description="Play a movie or episode right now")
async def slash_play(interaction: discord.Interaction, query: str):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    # Via Controls, not the player directly: these used to duplicate the
    # resolve logic and skip the video/music handoff, so /play left Spotify
    # playing over the top of the film.
    await _followup(interaction, await brain.controls.fast("play", query=query))


@bot.tree.command(name="queue", description="Add something to the queue, or show the queue")
async def slash_queue(interaction: discord.Interaction, query: Optional[str] = None):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    if not query:
        await interaction.followup.send(_format_queue(await player.queue_titles()))
        return
    await _followup(interaction, await brain.controls.fast("queue", query=query))


@bot.tree.command(name="status", description="What's playing")
async def slash_status(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await brain.controls.fast("status"))


@bot.tree.command(
    name="draw",
    description="Generate an image from exactly this prompt — no rewriting",
)
async def slash_draw(interaction: discord.Interaction, prompt: str):
    """The prompt goes to the image server verbatim.

    Deliberately no language model in the path. Asked in conversation she
    writes the prompt herself, which is usually an improvement — "a spooky
    alien" became ninety characters of scene description — but it means the
    words that reach the diffusion model are never the ones that were typed.
    This is the way to say exactly what you want, and the way to use the whole
    of CLIP's 77-token window on your own terms rather than hers.
    """
    if not await _guard(interaction):
        return
    # Generation runs to tens of seconds; Discord abandons an interaction that
    # has not answered in three.
    await interaction.response.defer()
    imagegen.set_reference(None)
    await _followup(interaction, await imagegen.generate(prompt))


@bot.tree.command(
    name="edit",
    description="Change an attached picture using exactly this prompt",
)
async def slash_edit(interaction: discord.Interaction, prompt: str,
                     picture: discord.Attachment):
    """Same as /draw, starting from a picture instead of from nothing."""
    if not await _guard(interaction):
        return
    if not (picture.content_type or "").startswith("image/"):
        await interaction.response.send_message(
            f"That is a {picture.content_type or 'unknown file'}, not an image.",
            ephemeral=True)
        return
    if picture.size > MAX_REFERENCE_BYTES:
        await interaction.response.send_message(
            f"That picture is {picture.size / 1024**2:.0f}MB — too big to work "
            f"from. Under {MAX_REFERENCE_BYTES // 1024**2}MB, please.",
            ephemeral=True)
        return
    await interaction.response.defer()
    try:
        imagegen.set_reference(await picture.read(), picture.filename)
    except Exception:
        log.warning("Could not read the attached picture", exc_info=True)
        await interaction.followup.send("I couldn't read that picture.")
        return
    try:
        await _followup(interaction, await imagegen.generate(prompt))
    finally:
        # Cleared whatever happened: this task's reference must not outlive it.
        imagegen.set_reference(None)


@bot.tree.command(name="pause", description="Pause playback")
async def slash_pause(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    # Defer: pausing routes through the active source and may hit the network,
    # and Discord kills any interaction that doesn't answer within 3 seconds.
    await interaction.response.defer()
    await interaction.followup.send(await brain.controls.fast("pause"))


@bot.tree.command(name="resume", description="Resume playback")
async def slash_resume(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await brain.controls.fast("resume"))


@bot.tree.command(name="skip", description="Skip to the next thing")
async def slash_skip(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await brain.controls.fast("skip"))


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def slash_stop(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await brain.controls.fast("stop"))


@bot.tree.command(name="forward", description="Skip ahead (default 30 seconds)")
async def slash_forward(interaction: discord.Interaction, seconds: int = 30):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await player.seek(abs(seconds)))


@bot.tree.command(name="rewind", description="Go back (default 15 seconds)")
async def slash_rewind(interaction: discord.Interaction, seconds: int = 15):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await player.seek(-abs(seconds)))


@bot.tree.command(name="music", description="Play music on Spotify (blank resumes)")
async def slash_music(interaction: discord.Interaction, query: str = ""):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await _followup(interaction, await brain.controls.start_music(query))


@bot.tree.command(name="musicqueue", description="Queue music on Spotify (blank shows the queue)")
async def slash_musicqueue(interaction: discord.Interaction, query: str = ""):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await _followup(interaction, await brain.controls.queue_music(query))


@bot.tree.command(name="karaoke", description="Show synced lyrics on screen during music")
async def slash_karaoke(interaction: discord.Interaction, on: bool = True):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await brain.controls.set_karaoke(on))


@bot.tree.command(name="speed", description="Playback speed: 2 to fast forward, 1 for normal")
async def slash_speed(interaction: discord.Interaction, rate: float = 2.0):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await player.set_speed(rate))


@bot.tree.command(name="audio", description="Set the audio language (remembered per library)")
async def slash_audio(interaction: discord.Interaction, language: str):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await player.set_audio_language(language))


@bot.tree.command(name="subs", description="Set the subtitle language, or 'off'")
async def slash_subs(interaction: discord.Interaction, language: str):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    await interaction.followup.send(await player.set_subtitle_language(language))


@bot.tree.command(name="tracks", description="List audio and subtitle tracks on this file")
async def slash_tracks(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    import tracks as tk

    rows = await player.track_list()
    await interaction.followup.send(
        tk.describe_tracks(rows, "audio") + "\n\n" + tk.describe_tracks(rows, "sub")
    )


@bot.tree.command(name="restart", description="Relaunch the player if it's stuck")
async def slash_restart(interaction: discord.Interaction):
    if not await _guard(interaction):
        return
    await interaction.response.defer()
    ok = await player.restart_player()
    await interaction.followup.send(
        "Player relaunched." if ok else "Couldn't relaunch mpv — check the console."
    )


@bot.tree.command(name="help", description="How to use this bot")
async def slash_help(interaction: discord.Interaction, topic: Optional[str] = None):
    """Rendered from the same HELP_SECTIONS the typed `help` uses, so the two
    can't drift apart as commands get added."""
    embed = discord.Embed(title="Plex bot", color=0xE5A00D)
    embed.description = "Just talk to me in this channel — no slash needed."
    wanted = (topic or "").strip().lower()
    for key, title, lines in HELP_SECTIONS:
        if wanted and not (key.startswith(wanted) or wanted.startswith(key)):
            continue
        embed.add_field(name=title, value="\n".join(lines), inline=False)
    if not embed.fields:
        embed.add_field(
            name="No such section",
            value=", ".join(key for key, _, _ in HELP_SECTIONS),
            inline=False,
        )
    embed.set_footer(
        text="Slash: /play /queue /status /pause /resume /skip /stop /forward "
             "/rewind /speed /audio /subs /tracks /music /musicqueue /karaoke /restart"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------------------------------------------------------------

REQUIRED_CONFIG = [
    "DISCORD_TOKEN", "ALLOWED_CHANNEL_ID", "STATUS_CHANNEL_ID",
    "PLEX_URL", "PLEX_TOKEN",
    "MPV_PATH", "MPV_FULLSCREEN", "MPV_EXTRA_OPTS", "MPV_IPC_SOCKET",
    "MPV_START_TIMEOUT",
    "NL_ENABLED", "NL_BACKEND", "OLLAMA_HOST", "OLLAMA_MODEL",
    "NOWPLAYING_ENABLED", "NOWPLAYING_CHANNEL_ID", "IDLE_IMAGE",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI",
    "SPOTIFY_CACHE", "SPOTIFY_DEVICE_NAME", "SPOTIFY_EXE",
    "MUSIC_MINIMIZE_MPV", "MUSIC_FOCUS_SPOTIFY", "MUSIC_MAXIMIZE_SPOTIFY",
    "MPV_WINDOW_TITLE",
    "SPOTIFY_PROCESS", "KARAOKE_DEFAULT",
    "EXCLUDE_LIBRARIES", "PLEX_DEVICE_NAME", "PLEX_CLIENT_ID",
    "DEFAULT_AUDIO_LANG", "DEFAULT_SUBTITLE_LANG",
    "ANIME_LIBRARIES", "ANIME_AUDIO_LANG", "ANIME_SUBTITLE_LANG", "PREFS_FILE",
    "TITLE_CACHE_FILE",
    "BOT_PERSONA", "FLAVOR_ENABLED", "FLAVOR_TIMEOUT",
    "YTDL_PATH", "YTDL_FORMAT", "YOUTUBE_TIMEOUT",
    "AUTOPLAY_NEXT_EPISODE", "FREEZE_TIMEOUT",
    "RESUME_MIN_SECONDS", "RESUME_MAX_FRACTION",
]


def check_config() -> None:
    """Fail loudly and early if config.py is older than the rest of the bot."""
    missing = [name for name in REQUIRED_CONFIG if not hasattr(config, name)]
    if missing:
        log.error(
            "config.py is out of date — missing: %s\n"
            "Replace config.py with the current version, then restart.",
            ", ".join(missing),
        )
        raise SystemExit(1)


def check_mpv() -> None:
    """Confirm mpv is actually launchable before we try, so a missing binary
    reads as one clear line instead of a WinError 2 traceback."""
    import shutil

    if config.MPV_PATH:
        if os.path.exists(config.MPV_PATH):
            return
        log.error(
            "MPV_PATH points to a file that doesn't exist:\n  %s\n"
            "Fix the path in .env, or clear MPV_PATH if mpv is on your PATH.",
            config.MPV_PATH,
        )
        raise SystemExit(1)

    if shutil.which("mpv") or shutil.which("mpv.exe"):
        return
    if sys.platform == "darwin":
        hint = "Install it with `brew install mpv`, or set MPV_PATH in .env."
    else:
        hint = ("Either add it to your PATH, or set MPV_PATH in .env to the "
                "full path of mpv.exe (e.g. C:\\Program Files\\mpv\\mpv.exe).")
    log.error("mpv was not found. %s", hint)
    raise SystemExit(1)


async def _run() -> None:
    """Start the bot and shut the player down on the *same* loop.

    The previous version called asyncio.run(player.shutdown()) after bot.run()
    had already closed its loop, so the cancellations and thread hops silently
    did nothing and mpv was left running.
    """
    async with bot:
        try:
            await bot.start(config.DISCORD_TOKEN)
        finally:
            if player is not None:
                try:
                    await player.shutdown()
                except Exception:
                    log.exception("Player shutdown failed")


def main():
    check_config()
    check_mpv()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()