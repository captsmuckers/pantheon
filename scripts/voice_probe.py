"""Phase 0 voice probe — throwaway diagnostic, not part of the bot.

Answers one question before any voice feature gets built: does Discord hand
us usable per-speaker audio in this channel?

Since Discord's DAVE (end-to-end encryption) enforcement for non-stage voice
calls in March 2026, that question has two halves that look identical from
the outside — silence. Either nobody spoke, or packets are arriving and we
cannot decrypt them. This probe separates the two by counting three things
independently:

  speaking events   from the gateway, in the clear, before any crypto
  raw UDP packets   arriving at the socket, still encrypted
  decoded PCM       what actually survived decryption

    speaking = 0, raw = 0                -> nobody talked, inconclusive
    speaking > 0, raw > 0, pcm = 0       -> DAVE wall, receive is blocked
    speaking > 0, pcm > 0                -> it works

Touches nothing the bot owns: no mpv, no Plex, no Spotify, no text channel.
It joins voice, records, writes WAVs, prints a verdict and exits.

    .venv/Scripts/python scripts/voice_probe.py [seconds]

Needs the venv interpreter — DAVE support requires discord.py >= 2.7 and the
davey package, which the global environment deliberately does not have, so
that production stays on the version it was tested against.
"""

import asyncio
import logging
import os
import sys
import wave
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord

try:
    from discord.ext import voice_recv
except ImportError:
    sys.exit(
        "discord-ext-voice-recv is not installed in this interpreter.\n"
        "  .venv/Scripts/python -m pip install discord-ext-voice-recv"
    )

import config

# Read from the environment rather than hard-coded: these identify one
# person's Discord server, and a probe script is exactly the sort of file that
# gets committed with someone's IDs still in it.
#
#   VOICE_CHANNEL_ID    the voice channel to sit in (config.VOICE_CHANNEL_ID)
#   PROBE_USER_ID       whose audio to capture; blank captures everyone
VOICE_CHANNEL_ID = config.VOICE_CHANNEL_ID
STREAMING_USER_ID = int(os.getenv("PROBE_USER_ID", "0") or 0)

if not VOICE_CHANNEL_ID:
    sys.exit("Set VOICE_CHANNEL_ID in .env before running the probe.")

# Discord's voice format, fixed by the protocol.
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

OUT_DIR = Path(__file__).resolve().parent.parent / "logs" / "voice-probe"
DEFAULT_SECONDS = 30

# A sink callback can fire with essentially no payload — a silence frame or a
# stream opening — which is not evidence that decryption works. An earlier
# version counted any callback at all as success and reported GO on 0.0
# seconds of audio from one user. Real speech clears this easily.
MIN_AUDIO_SECONDS = 0.4


class CryptoErrorCounter(logging.Handler):
    """Catch the CryptoError the reader logs when decryption fails.

    reader.py logs this at ERROR and then swallows the packet, so a decrypt
    failure is otherwise indistinguishable from silence.
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.first: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "crypto" in msg.lower() or "decrypt" in msg.lower():
            self.count += 1
            if self.first is None:
                self.first = msg


class Probe(discord.Client):
    def __init__(self, seconds: int):
        intents = discord.Intents.default()  # voice_states is already in here
        super().__init__(intents=intents)
        self.seconds = seconds
        self.audio: dict[int, bytearray] = defaultdict(bytearray)
        self.names: dict[int, str] = {}
        self.unknown_ssrc_packets = 0

        # The three independent counters the verdict rests on.
        self.speaking_events = 0
        self.speakers_seen: set[int] = set()
        self.raw_packets = 0
        self.raw_bytes = 0

        self.crypto = CryptoErrorCounter()
        logging.getLogger("discord.ext.voice_recv").addHandler(self.crypto)
        logging.getLogger("discord.ext.voice_recv").setLevel(logging.ERROR)

    # -- speaking state --------------------------------------------------

    async def on_voice_member_speaking_start(self, ssrc: int) -> None:
        """voice_recv dispatches an ssrc int here, not a Member.

        reader.py:319 — getting this wrong means the handler raises and
        discord.py swallows it, which reads as "nobody spoke".
        """
        self.speaking_events += 1
        self.speakers_seen.add(ssrc)

    # -- decrypted audio -------------------------------------------------

    def _on_voice(self, user, data) -> None:
        if user is None:
            self.unknown_ssrc_packets += 1
            return
        self.audio[user.id].extend(data.pcm)
        self.names.setdefault(user.id, str(user))

    async def on_ready(self) -> None:
        print(f"connected as {self.user}")
        try:
            await self._run()
        finally:
            await self.close()

    def _count_raw_packet(self, data: bytes) -> None:
        """Every UDP packet, still encrypted, before the reader touches it."""
        self.raw_packets += 1
        self.raw_bytes += len(data)

    def _instrument_socket(self, vc) -> None:
        """Register an independent listener on the raw voice socket.

        vc.socket is MISSING on the client itself in discord.py 2.7; the
        connection owns it, and exposes the same registration hook the reader
        uses (voice_state.py:611). Counting here proves packets are physically
        arriving, which is what separates "nobody spoke" from "we cannot read
        what they said" — the two look identical downstream.
        """
        try:
            vc._connection.add_socket_listener(self._count_raw_packet)
        except Exception as exc:
            print(f"  (could not instrument socket: {exc} — raw count unavailable)")

    def _uninstrument_socket(self, vc) -> None:
        try:
            vc._connection.remove_socket_listener(self._count_raw_packet)
        except Exception:
            pass

    async def _run(self) -> None:
        channel = self.get_channel(VOICE_CHANNEL_ID) or await self.fetch_channel(
            VOICE_CHANNEL_ID
        )

        if not isinstance(channel, discord.VoiceChannel):
            print(f"\nFAIL  {VOICE_CHANNEL_ID} is a {type(channel).__name__}, not a voice channel.")
            return

        print(f"channel: #{channel.name}  ({channel.guild.name})")
        present = [m for m in channel.members if m.id != self.user.id]
        print("in the channel: " + (", ".join(str(m) for m in present) if present else "nobody"))

        perms = channel.permissions_for(channel.guild.me)
        if not perms.connect:
            print("\nFAIL  the bot lacks Connect permission on this channel.")
            return

        print(f"\njoining, recording {self.seconds}s — TALK NOW\n")

        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        try:
            self._instrument_socket(vc)
            vc.listen(voice_recv.BasicSink(self._on_voice))
            for remaining in range(self.seconds, 0, -1):
                print(
                    f"  {remaining:3d}s   speaking:{self.speaking_events:<4} "
                    f"raw:{self.raw_packets:<6} pcm:{len(self.audio)}   ",
                    end="\r",
                    flush=True,
                )
                await asyncio.sleep(1)
            print(" " * 72, end="\r")
        finally:
            vc.stop_listening()
            self._uninstrument_socket(vc)
            await vc.disconnect()

        self._report()

    def _report(self) -> None:
        print("=" * 70)
        print("COUNTERS")
        print("-" * 70)
        print(f"  gateway speaking events   {self.speaking_events}")
        print(f"  distinct speakers seen    {len(self.speakers_seen)}")
        print(f"  raw UDP packets           {self.raw_packets}  ({self.raw_bytes} bytes)")
        print(f"  users with decoded PCM    {len(self.audio)}")
        print(f"  crypto errors logged      {self.crypto.count}")
        if self.crypto.first:
            print(f"    first: {self.crypto.first}")
        if self.unknown_ssrc_packets:
            print(f"  unmappable-SSRC packets   {self.unknown_ssrc_packets}")

        # Only tracks with real duration count as evidence.
        real = {u: p for u, p in self.audio.items()
                if len(p) / BYTES_PER_SECOND >= MIN_AUDIO_SECONDS}

        if self.audio:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            print("-" * 70)
            for uid, pcm in sorted(self.audio.items(), key=lambda kv: -len(kv[1])):
                secs = len(pcm) / BYTES_PER_SECOND
                tag = "  <- streaming account" if uid == STREAMING_USER_ID else ""
                if uid not in real:
                    tag += "  (below threshold, not evidence)"
                print(f"  {self.names.get(uid, '?'):<30} {secs:>7.1f}s  "
                      f"{len(pcm):>9} bytes{tag}")
                with wave.open(str(OUT_DIR / f"{uid}.wav"), "wb") as w:
                    w.setnchannels(CHANNELS)
                    w.setsampwidth(SAMPLE_WIDTH)
                    w.setframerate(SAMPLE_RATE)
                    w.writeframes(bytes(pcm))
            print(f"\n  wrote {len(self.audio)} file(s) to {OUT_DIR}")

        print("\n" + "=" * 70)
        print("VERDICT")
        print("=" * 70)

        if real:
            print("GO — audio decrypted successfully. Voice receive works")
            print("     through DAVE. The original design holds.")
            print()
            print("     Now listen to the WAVs: the numbers prove the streams")
            print("     are separate, only your ears prove each one is clean.")
        elif self.speaking_events == 0 and self.raw_packets == 0:
            print("INCONCLUSIVE — nobody spoke during the recording.")
            print("     No gateway speaking events and no packets at all, so")
            print("     there was nothing to decode. Re-run and talk.")
        elif self.speaking_events > 0 or self.raw_packets > 0:
            print("NO-GO — Discord sent us speech we cannot read.")
            print()
            print("     The gateway reported people speaking and packets")
            print("     arrived, but nothing survived decryption. That is the")
            print("     DAVE end-to-end encryption wall: voice_recv only")
            print("     implements the pre-E2EE transport modes.")
            print()
            print("     Not fixable from here — it needs DAVE support upstream")
            print("     in discord-ext-voice-recv. Pivot to local audio")
            print("     capture instead.")


if __name__ == "__main__":
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS
    if not config.DISCORD_TOKEN:
        sys.exit("DISCORD_TOKEN is not set in this checkout's .env")
    Probe(seconds).run(config.DISCORD_TOKEN, log_handler=None)
