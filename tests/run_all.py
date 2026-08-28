"""Run every suite. No Plex, Spotify, mpv, Discord — or even a .env — needed.

    python tests/run_all.py

Placeholders are supplied for the four settings config.py treats as required,
so a fresh clone passes its own tests before anyone has credentials. They are
obvious nonsense on purpose: a test that quietly picked up a real token and
talked to a real server would be far worse than one that fails.

An existing .env still wins — python-dotenv does not override variables that
are already set, so these only fill gaps.
"""

import os
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252, which can't render the em-dashes and
# arrows the bot's own strings use.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Enough for `import config` to get past its required-setting checks. Nothing
# here is contacted: every suite fakes Plex, Spotify, mpv and Discord.
PLACEHOLDERS = {
    "DISCORD_TOKEN": "test-token-not-real",
    "PLEX_URL": "http://127.0.0.1:32400",
    "PLEX_TOKEN": "test-token-not-real",
    "ALLOWED_CHANNEL_ID": "0",
}
CHILD_ENV = {**PLACEHOLDERS, **os.environ, "PYTHONIOENCODING": "utf-8"}
HERE = Path(__file__).resolve().parent
SUITES = [
    ("config schema", "test_schema.py"),
    ("history trimming", "test_history.py"),
    ("event loop", "test_event_loop.py"),
    ("queue advance", "test_advance.py"),
    ("behaviour", "test_behaviour.py"),
    ("startup & cache", "test_startup_and_cache.py"),
    ("bot wiring", "test_bot_wiring.py"),
    ("source-swap phrasings", "test_phrasings.py"),
    ("query resolution", "test_resolution.py"),
    ("flavour text", "test_flavor.py"),
    ("no fake actions", "test_no_fake_actions.py"),
    ("spotify search", "test_spotify_search.py"),
    ("search command", "test_search_command.py"),
    ("youtube", "test_youtube.py"),
    ("live streams", "test_streams.py"),
    ("series playback", "test_show_playback.py"),
    ("episode phrasing", "test_episode_phrasing.py"),
    ("youtube links", "test_youtube_links.py"),
    ("karaoke", "test_karaoke.py"),
    ("spotify queue preserve", "test_spotify_queue_preserve.py"),
    ("spotify url", "test_spotify_url.py"),
    ("voice", "test_voice.py"),
    ("speech", "test_speech.py"),
    ("speaker attribution", "test_speakers.py"),
]

failed = []
for label, name in SUITES:
    print(f"\n{'=' * 70}\n{label}  ({name})\n{'=' * 70}")
    result = subprocess.run(
        [sys.executable, str(HERE / name)],
        cwd=HERE.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=CHILD_ENV,
    )
    tail = [ln for ln in result.stdout.splitlines() if ln.strip()][-4:]
    print("\n".join(tail) if tail else "(no output)")
    if result.returncode != 0:
        failed.append(label)
        print(f"--- stderr ---\n{result.stderr[-2000:]}")

print(f"\n{'=' * 70}")
if failed:
    print(f"FAILED: {', '.join(failed)}")
    sys.exit(1)
print(f"all {len(SUITES)} suites passed")
