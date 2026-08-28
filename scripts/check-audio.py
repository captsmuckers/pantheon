#!/usr/bin/env python3
"""Prove both audio cables work, without Discord or the bot in the way.

    cd ~/athena && ./scripts/check-audio.py

macOS has no built-in loopback, so voice input and spoken output each depend
on a virtual device that is easy to get subtly wrong — installed but not
selected, selected but muted, or selected on a live connection Discord has not
re-read. Every one of those failures is silent. This makes them loud.

Three checks, in the order that isolates a fault:

  1. The configured names resolve to real devices at all.
  2. Capture: BlackHole is a loopback, so playing a tone into it and recording
     it back proves the capture path end to end without Discord.
  3. Playback: the same, in the other direction, on the return cable.

Deliberately does NOT touch Discord. If this passes and the bot still cannot
hear anything, the fault is in Discord's own routing — the account is deafened
rather than muted, or its output device was changed on a live voice connection,
which Discord will not migrate until you leave and rejoin.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_VENV = ROOT / ".venv" / "bin" / "python"
if _VENV.exists() and Path(sys.executable).resolve() != _VENV.resolve():
    import os
    os.execv(str(_VENV), [str(_VENV), str(Path(__file__).resolve()), *sys.argv[1:]])

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

import config  # noqa: E402

RATE = 48000
FAILED = []


def resolve(name: str, kind: str):
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d[key] > 0:
            return i
    return None


def loopback(name: str, seconds: float = 1.2) -> float:
    """Play a tone into a device and record it back. Returns the peak heard.

    Two processes, because CoreAudio refuses a second stream on a device this
    process already holds — which is also how it works in production, where
    Discord and the bot are separate processes.
    """
    tone = (0.25 * np.sin(2 * np.pi * 440 *
            np.arange(int(RATE * seconds)) / RATE)).astype("float32")
    stereo = np.column_stack([tone, tone])
    path = Path("/tmp/athena-audio-check.npy")
    np.save(path, stereo)
    player = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,numpy as np,sounddevice as sd;"
         "a=np.load(sys.argv[1]);"
         "d=next(i for i,x in enumerate(sd.query_devices())"
         " if x['name']==sys.argv[2] and x['max_output_channels']>0);"
         "sd.play(a,samplerate=48000,device=d);sd.wait()",
         str(path), name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    got = []
    dev = resolve(name, "input")
    with sd.InputStream(device=dev, samplerate=RATE, channels=2, dtype="float32",
                        callback=lambda i, f, t, s: got.append(i.copy())):
        time.sleep(seconds + 0.6)
    player.wait(timeout=10)
    path.unlink(missing_ok=True)
    buf = np.concatenate(got) if got else np.zeros((0, 2))
    return float(np.abs(buf).max()) if len(buf) else 0.0


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main():
    print("1. configured devices exist\n")
    pairs = [("VOICE_DEVICE (she hears the room)", config.VOICE_DEVICE, "input"),
             ("TTS_OUTPUT_DEVICE (room hears her)", config.TTS_OUTPUT_DEVICE, "output")]
    for label, name, kind in pairs:
        idx = resolve(name, kind)
        check(f"{label}: {name!r}", idx is not None,
              f"index {idx}" if idx is not None else "not found")

    if FAILED:
        print("\n  macOS ships no loopback device. Install both:")
        print("    brew install --cask blackhole-2ch blackhole-16ch")
        print("  Then restart Discord so it sees them.")
        return 1

    print("\n2. capture path — tone into the cable, recorded back off it\n")
    peak = loopback(config.VOICE_DEVICE)
    check(f"{config.VOICE_DEVICE} carries audio", peak > 0.05, f"peak {peak:.3f}")
    check("signal clears VOICE_THRESHOLD", peak > config.VOICE_THRESHOLD,
          f"threshold {config.VOICE_THRESHOLD}")

    print("\n3. return path — same, on the cable feeding Discord's mic\n")
    peak = loopback(config.TTS_OUTPUT_DEVICE)
    check(f"{config.TTS_OUTPUT_DEVICE} carries audio", peak > 0.05, f"peak {peak:.3f}")

    print()
    if FAILED:
        print(f"  {len(FAILED)} check(s) failed.")
        return 1
    print("  Both cables work.\n"
          "  If the bot still hears nothing, the fault is Discord's routing:\n"
          "    - the streaming account must be MUTED, never DEAFENED\n"
          "      (a deafened client receives no audio at all to capture)\n"
          "    - Discord will not move a LIVE voice connection to a newly\n"
          "      selected device — leave and rejoin the channel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
