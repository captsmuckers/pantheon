#!/usr/bin/env python3
"""Does DRM video survive a screen capture on this machine?

THIS DECIDES WHETHER /watchtv IS WORTH BUILDING. Friends see the game because
the Mac is screen-shared into Discord, and protected content is frequently
blacked out in captures. On macOS that splits by browser: Safari uses FairPlay
with hardware protection and typically captures black, while Chrome's Widevine
L3 is software and typically captures fine. "Typically" is why this exists.

If the video region captures black, no amount of automation helps and the
feature is dead — better to know in September than at kickoff.

    ./.venv/bin/python scripts/drm-capture-test.py

Requires: a Chrome profile already signed into YouTube TV, and something
playing. Run it with a game (or any live channel) on screen.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def grab(path: Path) -> bool:
    """Whole-screen capture, no shutter sound, no cursor."""
    r = subprocess.run(["screencapture", "-x", "-C", str(path)],
                       capture_output=True)
    return r.returncode == 0 and path.exists()


def analyse(paths):
    """Is there moving picture in these frames, or a black rectangle?

    Two signals, because either alone can lie. A black region is the classic
    DRM block. Two IDENTICAL frames taken seconds apart during playback mean
    the capture is frozen even if it is not black — also a failure, and one
    that looks fine in a single screenshot.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, ("Pillow is not installed in this environment, so the "
                      "frames cannot be measured. They are saved; look at "
                      "them.")
    import statistics

    frames = []
    for p in paths:
        im = Image.open(p).convert("L")
        w, h = im.size
        # The middle of the screen, where a fullscreen video is. Sampling the
        # whole screen would average in the desktop and hide a black player.
        box = (int(w * 0.25), int(h * 0.25), int(w * 0.75), int(h * 0.75))
        frames.append(im.crop(box))

    reports = []
    verdict = True
    for i, f in enumerate(frames):
        px = list(f.getdata())
        mean = sum(px) / len(px)
        sd = statistics.pstdev(px)
        reports.append(f"  frame {i + 1}: mean brightness {mean:6.1f}/255, "
                       f"variation {sd:6.1f}")
        # A near-black, near-flat centre is the DRM block.
        if mean < 8 and sd < 6:
            verdict = False

    if len(frames) >= 2:
        a, b = list(frames[0].getdata()), list(frames[1].getdata())
        diff = sum(1 for x, y in zip(a, b) if abs(x - y) > 8) / len(a)
        reports.append(f"  frames differ in {diff * 100:5.1f}% of pixels")
        if diff < 0.005:
            verdict = False
            reports.append("  -> frames are effectively identical: either the "
                           "capture is frozen or nothing is playing")
    return verdict, "\n".join(reports)


def main():
    print(__doc__.split("\n\n")[0])
    print()
    prof = config.WATCHTV_PROFILE
    print(f"  profile   : {prof}")
    print(f"  signed in : {'looks set up' if os.path.isdir(os.path.join(prof, 'Default')) else 'NOT YET — sign in first'}")
    print()
    print("  Make sure a YouTube TV stream is PLAYING and fullscreen, then")
    print("  this will capture two frames two seconds apart.")
    print()
    for i in range(5, 0, -1):
        print(f"    capturing in {i}...", end="\r", flush=True)
        time.sleep(1)
    print("                          ")

    out = Path(tempfile.mkdtemp(prefix="drmtest-"))
    shots = []
    for i in range(2):
        p = out / f"frame{i + 1}.png"
        if not grab(p):
            print("  FAILED to capture the screen at all.")
            print("  Grant Screen Recording permission to your terminal in")
            print("  System Settings > Privacy & Security, then re-run.")
            return 2
        shots.append(p)
        if i == 0:
            time.sleep(2)

    verdict, detail = analyse(shots)
    print(detail)
    print()
    if verdict is None:
        print(f"  Frames saved to {out}")
        return 0
    if verdict:
        print("  PASS — the video is present in the capture.")
        print("  Discord screen share will show the game. Build the feature.")
    else:
        print("  FAIL — the captured video region is black or frozen.")
        print()
        print("  If something WAS playing fullscreen, this machine's DRM")
        print("  playback is being excluded from screen capture. Chrome is")
        print("  the best case on macOS; if this was Safari, retry in Chrome")
        print("  before concluding anything.")
    print(f"\n  Frames saved to {out}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
