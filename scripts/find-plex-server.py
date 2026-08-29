#!/usr/bin/env python3
"""Find the PLEX_URL and PLEX_TOKEN for a server, including someone else's.

    ./scripts/find-plex-server.py

Signs you in to plex.tv with a four-character code, then prints the two
settings for every server you can reach — servers you own and servers shared
with you alike.

WHY THIS EXISTS. The setup instructions assume your own Plex on your own LAN,
where PLEX_URL is just http://<box>:32400. Someone watching a friend's library
has neither: no local address, and their own account rather than the owner's.
Both of the things they need are discoverable, and neither is guessable.

THERE ARE TWO KINDS OF PLEX TOKEN, and confusing them is the whole reason this
script exists rather than a paragraph telling you where to click:

  A SERVER token authenticates you to one particular server. It is what the
  bot needs in PLEX_TOKEN, and it is what you get if you follow the usual
  "open any item -> Get Info -> View XML and copy X-Plex-Token" advice.

  An ACCOUNT token authenticates you to plex.tv itself, which is the only
  thing that knows which servers exist and how to reach them from outside a
  LAN. A server token returns 401 there.

So the usual advice hands you the token the bot wants but not the one needed to
FIND the server in the first place. Rather than explain that, this uses plex.tv
link codes: you get a four-character code, you type it into a page while signed
in as yourself, and plex.tv hands back an account token. No password is typed
here, and nothing is stored.

TWO THINGS TO KNOW BEFORE RELYING ON A REMOTE SERVER.

  The bot never transcodes. It hands mpv the ORIGINAL file, which is the whole
  reason subtitle changes are instant and any codec plays — but it means
  playback needs as much bandwidth as the file's own bitrate, over the OWNER'S
  upload. A 1080p film at 8 Mbps is comfortable on most connections; a 4K
  remux at 60-80 Mbps is not, and no quality setting exists to lower it.
  Ordinary Plex clients transcode in that situation. This one cannot.

  The owner must have Remote Access enabled. Without it, only local addresses
  are advertised and nothing here will be reachable from anywhere else.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_VENV = ROOT / ".venv" / "bin" / "python"
if _VENV.exists() and Path(sys.executable).resolve() != _VENV.resolve():
    import os
    os.execv(str(_VENV), [str(_VENV), str(Path(__file__).resolve()), *sys.argv[1:]])

try:
    from plexapi.myplex import MyPlexAccount, MyPlexPinLogin
except ImportError:
    print("plexapi is not installed. Create the environment first:\n"
          "  .venv/bin/python -m pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)


LINK_URL = "https://plex.tv/link"


def _account_via_pin():
    """Sign in with a plex.tv link code. Returns an account, or None.

    Chosen over asking for a token because the token people can readily find is
    the wrong one — "Get Info -> View XML" yields a SERVER token, which plex.tv
    rejects with a 401 that explains nothing. A link code cannot be the wrong
    kind, and no password is typed into this script.
    """
    try:
        pin = MyPlexPinLogin()
    except Exception as exc:
        print(f"Could not reach plex.tv: {exc}", file=sys.stderr)
        return None

    print("To sign in, open this page while signed in to Plex as yourself:\n")
    print(f"    {LINK_URL}\n")
    print(f"and enter this code:      {pin.pin}\n")
    print("Waiting... (Ctrl-C to give up)", flush=True)

    try:
        pin.run(timeout=180)
        ok = pin.waitForLogin()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return None
    except Exception as exc:
        print(f"\nSign-in failed: {exc}", file=sys.stderr)
        return None

    if not ok or not pin.token:
        print("\nThe code was not entered in time. Run this again for a new one.",
              file=sys.stderr)
        return None

    try:
        return MyPlexAccount(token=pin.token)
    except Exception as exc:
        print(f"\nSigned in, but plex.tv would not describe the account: {exc}",
              file=sys.stderr)
        return None


def main() -> int:
    account = _account_via_pin()
    if account is None:
        return 1

    print(f"\nSigned in as {account.username}.\n")

    servers = [r for r in account.resources() if "server" in (r.provides or "")]
    if not servers:
        print("No Plex servers are visible to this account.")
        return 1

    for res in servers:
        owner = "yours" if res.owned else f"shared with you"
        online = "online" if res.presence else "OFFLINE right now"
        print(f"{res.name}  ({owner}, {online})")

        remote = [c for c in res.connections if not c.local]
        local = [c for c in res.connections if c.local]

        if res.owned and local:
            print("  On the same network as the server, prefer a local address:")
            for c in local:
                print(f"    PLEX_URL={c.uri}")
        if remote:
            print("  From anywhere else:")
            for c in remote:
                print(f"    PLEX_URL={c.uri}")
        elif not local:
            print("  No usable address advertised. The owner may not have "
                  "Remote Access enabled.")

        if res.accessToken:
            print(f"    PLEX_TOKEN={res.accessToken}")
        else:
            print("    PLEX_TOKEN=(none issued — no access to this server)")

        if not res.owned:
            print("  Note: playback streams the original file over the owner's\n"
                  "  upload, because this bot never transcodes. Fine for 1080p,\n"
                  "  usually not for 4K remuxes.")
        print()

    print("Paste the pair you want into the Settings page, or into .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
