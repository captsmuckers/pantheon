"""Re-authorise Spotify after the scope list changes.

Spotipy caches the token together with the scopes it was granted. Widening
SCOPES makes that cache insufficient, and the next start needs an interactive
browser round trip — which the scheduled task cannot do, because it runs hidden
with nothing to click. It hangs instead of failing.

Run this by hand, once, with the bot stopped.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import spotipy  # noqa: E402
from spotipy.oauth2 import SpotifyOAuth  # noqa: E402

import config  # noqa: E402
from spotify import SCOPES  # noqa: E402

print("Requesting these scopes:")
for scope in SCOPES.split():
    print(f"   {scope}")
print("\nA browser will open. Approve the request, then come back here.\n")

auth = SpotifyOAuth(
    client_id=config.SPOTIFY_CLIENT_ID,
    client_secret=config.SPOTIFY_CLIENT_SECRET,
    redirect_uri=config.SPOTIFY_REDIRECT_URI,
    scope=SCOPES,
    cache_path=config.SPOTIFY_CACHE,
    open_browser=True,
)
client = spotipy.Spotify(auth_manager=auth, requests_timeout=10)

me = client.current_user()
print(f"Authorised as {me.get('display_name')} ({me.get('id')})\n")

# The three that were failing on scope rather than deprecation. Real numbers
# here mean it worked; another 403 means the approval did not include them.
checks = (
    ("liked songs", lambda: client.current_user_saved_tracks(limit=1)["total"]),
    ("saved albums", lambda: client.current_user_saved_albums(limit=1)["total"]),
    ("followed artists",
     lambda: client.current_user_followed_artists(limit=1)["artists"]["total"]),
)
failed = False
for label, call in checks:
    try:
        print(f"  {label:<18} {call()}")
    except Exception as exc:
        failed = True
        print(f"  {label:<18} STILL FAILING — {str(exc)[:90]}")

print()
print("Done. Start the bot again." if not failed
      else "Some scopes did not take — re-run and check the approval screen.")
sys.exit(1 if failed else 0)
