#!/usr/bin/env python3
"""Route media audio to a clean virtual device, so the Discord stream sounds right.

    scripts/setup-stream-audio.py            create if missing, then select it
    scripts/setup-stream-audio.py --status   report, change nothing
    scripts/setup-stream-audio.py --revert    go back to the built-in speakers

THE PROBLEM. Music and video sounded compressed and bass-light in the Discord
screen share while sounding fine everywhere else — same track on a phone with
the same headphones was clean. Measured while Spotify was playing: BOTH
existing BlackHole devices read exactly 0.0 RMS, and the system default output
was "MacBook Pro Speakers". So everything was rendering through the built-in
speaker route, which carries Apple's loudness/protection processing tuned for
small drivers, and the share captured that.

THE FIX. A Multi-Output Device pairing BlackHole 64ch with the built-in
speakers, set as the system default:

  * BlackHole 64ch is a THIRD, separate driver — deliberately not the existing
    2ch or 16ch. Those two are the bot's own voice pipeline (Discord's output
    is 2ch so she hears the room; 16ch is her mic so the room hears her).
    Reusing either would mix music into what Whisper transcribes, poisoning
    wake-word detection with song lyrics.
  * The speakers stay in the group so anyone physically at the machine still
    hears something, and so the group has a real hardware clock to follow.
    BlackHole gets drift correction as the follower.

WHAT THIS DOES NOT TOUCH. TTS is pinned to BlackHole 16ch by
TTS_OUTPUT_DEVICE, and Discord's own output device is set inside Discord.
Neither follows the system default, so both are unaffected. Only mpv, Spotify
and the browser move -- which is exactly the set that feeds the stream.

WHY IT RUNS AT LOGIN. An aggregate built through AudioHardwareCreateAggregateDevice
lives in memory only; it is NOT written to com.apple.audio.AudioMIDISetup.plist
and does not survive a reboot. Verified by checking that plist after creating
one. So this is idempotent and gets run again at every login.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _coreaudio as ca  # noqa: E402

DEVICE_NAME = "Pantheon Stream Out"
DEVICE_UID = "com.pantheon.streamout"

# Settings come from .env, read directly rather than through config.py: this
# runs from a LaunchAgent at login and has no business importing the bot's
# whole configuration (or python-dotenv) to learn two device names.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui import envfile  # noqa: E402


def settings():
    env = envfile.read(Path(__file__).resolve().parent.parent / ".env")
    return {
        "enabled": (env.get("STREAM_AUDIO_ENABLED", "").strip().lower()
                    in ("1", "true", "yes", "on")),
        "clean": env.get("STREAM_AUDIO_DEVICE", "").strip() or "BlackHole 64ch",
        "monitor": env.get("STREAM_AUDIO_MONITOR", "").strip(),
    }


def builtin_output():
    """The machine's own speakers, for the monitor half of the pair.

    Detected rather than hardcoded: "BuiltInSpeakerDevice" is right on a
    MacBook and wrong on a Mac mini or anything with an audio interface. Falls
    back to any real output device that is not one of the virtual ones, since a
    machine with no built-in speakers at all still needs a clock master.
    """
    virtual_hint = ("blackhole", "soundflower", "loopback", "aggregate",
                    "multi-output", DEVICE_NAME.lower())
    candidates = []
    for d in ca.devices():
        name = ca.dev_str(d, ca.kAudioObjectPropertyName) or ""
        uid = ca.dev_str(d, ca.kAudioDevicePropertyDeviceUID) or ""
        if uid == "BuiltInSpeakerDevice":
            return uid                      # the common case, exactly right
        if any(h in name.lower() for h in virtual_hint):
            continue
        candidates.append((d, uid))
    for dev_id, uid in candidates:
        if uid:
            return uid
    return None


def find(name):
    for d in ca.devices():
        if ca.dev_str(d, ca.kAudioObjectPropertyName) == name:
            return d
    return None


def uid_present(uid):
    return any(ca.dev_str(d, ca.kAudioDevicePropertyDeviceUID) == uid
               for d in ca.devices())


def report():
    cur = ca.default_output()
    print(f"default output: [{cur}] {ca.dev_str(cur, ca.kAudioObjectPropertyName)}")
    for d in ca.devices():
        print(f"  [{d}] {ca.dev_str(d, ca.kAudioObjectPropertyName)}")


def uid_for_name(name):
    for d in ca.devices():
        if ca.dev_str(d, ca.kAudioObjectPropertyName) == name:
            return ca.dev_str(d, ca.kAudioDevicePropertyDeviceUID)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true",
                    help="report what is selected and change nothing")
    ap.add_argument("--revert", action="store_true",
                    help="go back to the built-in output")
    ap.add_argument("--force", action="store_true",
                    help="run even when STREAM_AUDIO_ENABLED is off")
    args = ap.parse_args()

    if args.status:
        report()
        return 0

    if args.revert:
        uid = builtin_output()
        target = next((d for d in ca.devices()
                       if ca.dev_str(d, ca.kAudioDevicePropertyDeviceUID) == uid), None)
        if target is None:
            print("No built-in output to revert to.", file=sys.stderr)
            return 1
        ca.set_default_output(target)
        time.sleep(1)
        report()
        return 0

    cfg = settings()
    if not cfg["enabled"] and not args.force:
        # Off by default, deliberately. This changes the machine's default
        # output device, which is not something to do to somebody uninvited.
        print("STREAM_AUDIO_ENABLED is off — nothing to do. "
              "Turn it on in Settings, or pass --force.")
        return 0

    # The virtual driver may still be registering at login. Waiting beats
    # failing: a LaunchAgent that loses this race leaves the stream on the
    # processed built-in route with nothing to say about it.
    clean_uid = None
    for _ in range(30):
        clean_uid = uid_for_name(cfg["clean"])
        if clean_uid:
            break
        time.sleep(1)
    if not clean_uid:
        print(f"No audio device named {cfg['clean']!r}. Install it, or set "
              "STREAM_AUDIO_DEVICE to one that exists.", file=sys.stderr)
        return 1

    monitor_uid = uid_for_name(cfg["monitor"]) if cfg["monitor"] else builtin_output()
    if not monitor_uid:
        print("Could not find an output device to pair with. Set "
              "STREAM_AUDIO_MONITOR to one.", file=sys.stderr)
        return 1

    dev = find(DEVICE_NAME)
    if dev is None:
        dev = ca.make_multi_output(
            name=DEVICE_NAME, uid=DEVICE_UID,
            sub_uids=[monitor_uid, clean_uid],
            master_uid=monitor_uid, drift_uids=[clean_uid],
        )
        print(f"created {DEVICE_NAME}: {cfg['clean']} + monitor")
        # CoreAudio reassigns the id once the device is fully registered.
        # Setting the default with the id the create call returned succeeds and
        # silently does nothing, so look it up again by name.
        time.sleep(1)
        dev = find(DEVICE_NAME) or dev
    else:
        print(f"{DEVICE_NAME} already exists")

    for _ in range(3):
        ca.set_default_output(dev)
        time.sleep(1.5)
        if ca.default_output() == dev:
            print(f"default output is now {DEVICE_NAME}")
            return 0
        dev = find(DEVICE_NAME) or dev
    print("could not make it the default output", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
