#!/usr/bin/env python3
"""Find the PLEX_URL and PLEX_TOKEN for a server, including someone else's.

    ./scripts/find-plex-server.py

Asks for your plex.tv token, then prints the two settings for every server you
can reach — servers you own and servers shared with you alike.

WHY THIS EXISTS. The setup instructions assume your own Plex on your own LAN,
where PLEX_URL is just http://<box>:32400. Someone watching a friend's library
has neither: no local address, and their own account rather than the owner's.
Both of the things they need are discoverable, and neither is guessable:

  PLEX_URL    A remote server advertises an HTTPS address that looks like
              https://192-168-1-10.<hash>.plex.direct:32400 — a real
              certificate for an address that resolves to the server. Nobody
              is going to work that out by hand.

  PLEX_TOKEN  NOT the owner's token, and not the plex.tv token you paste in
              here either. Each server issues its own access token to each
              user who can reach it, and that is what the bot needs.

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

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_VENV = ROOT / ".venv" / "bin" / "python"
if _VENV.exists() and Path(sys.executable).resolve() != _VENV.resolve():
    import os
    os.execv(str(_VENV), [str(_VENV), str(Path(__file__).resolve()), *sys.argv[1:]])

try:
    from plexapi.myplex import MyPlexAccount
except ImportError:
    print("plexapi is not installed. Create the environment first:\n"
          "  .venv/bin/python -m pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)


HELP = """\
This needs your plex.tv account token — the one tied to YOUR account, not to
any particular server.

To find it:
  1. Sign in at https://app.plex.tv in a browser
  2. Open any item -> the ... menu -> Get Info -> View XML
  3. In the address bar of the tab that opens, copy the value of X-Plex-Token

It is not stored anywhere by this script; it is used once, now, to ask plex.tv
which servers you can reach.
"""


def main() -> int:
    print(HELP)
    # getpass so it is not echoed into a shared terminal or a screen recording,
    # and never written to shell history.
    token = getpass.getpass("plex.tv token (input hidden): ").strip()
    if not token:
        print("Nothing entered.", file=sys.stderr)
        return 1

    try:
        account = MyPlexAccount(token=token)
    except Exception as exc:
        print(f"\nplex.tv rejected that token: {exc}", file=sys.stderr)
        print("\nA server token will not work here — it authenticates to one "
              "server, not to your account. Take the token from app.plex.tv "
              "while signed in as yourself.", file=sys.stderr)
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
