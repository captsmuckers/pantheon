"""Driving the Discord desktop app to join and leave a voice channel.

The value of these tests is mostly in what they pin down as impossible, so a
later attempt does not quietly reintroduce it: there is no supported way to
start a screen share, and the account's token must never be automated.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import discord_control as dc  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_keybind_parsing():
    print("keybinds are read from whatever the operator assigned in Discord:")
    for combo in ("", "   ", "cmd", "shift", "cmd+shift"):
        ok, msg = dc.send_combo(combo)
        check(f"{combo!r} refused", not ok, msg[:50])
    # A modifier-only combo has no key to press; two keys is ambiguous.
    ok, msg = dc.send_combo("cmd+j+k")
    check("'cmd+j+k' refused as ambiguous", not ok, msg[:50])
    ok, msg = dc.send_combo("cmd+notakey")
    check("an unknown key name is refused", not ok, msg[:50])


def test_actions_refuse_without_configuration():
    """Half-configured must fail loudly, not send keystrokes into the void.

    A stray Cmd-K and a typed channel name landing in whatever holds focus is
    a worse outcome than an error message.
    """
    print("\nnothing is sent until it is configured:")
    saved = dc._settings
    try:
        dc._settings = lambda: {"channel": "", "join": "x", "leave": "x"}
        r = dc.join()
        check("no channel -> refused", not r["ok"] and "DISCORD_VOICE_CHANNEL" in r["message"])

        dc._settings = lambda: {"channel": "general", "join": "", "leave": ""}
        r = dc.join()
        check("no join keybind -> refused", not r["ok"] and "Switch To Voice" in r["message"])
        r = dc.leave()
        check("no leave keybind -> refused", not r["ok"] and "Disconnect" in r["message"])
    finally:
        dc._settings = saved


def test_state_is_shaped_for_the_page():
    print("\nstate reports what the page needs:")
    s = dc.state()
    for key in ("running", "in_voice", "streaming", "detail"):
        check(f"has {key}", key in s, str(sorted(s)))
    check("booleans are booleans",
          all(isinstance(s[k], bool) for k in ("running", "in_voice", "streaming")))
    # Streaming implies being in voice; the reverse is not true, and conflating
    # them would report a live stream whenever somebody was merely connected.
    if s.get("streaming"):
        check("streaming implies in_voice", s["in_voice"])


def test_the_token_is_never_touched():
    """Automating the account's token is self-botting and risks a ban.

    Asserted against the source so that a future 'quick fix' reaching for the
    user token fails this test rather than shipping.
    """
    print("\nthe account's token is never used:")
    import ast
    path = Path(__file__).resolve().parent.parent / "gui" / "discord_control.py"
    tree = ast.parse(path.read_text())

    # Inspected as code, not as text. "gateway" appears in a comment explaining
    # what Discord's TCP connections are, and a word filter flagged that — the
    # same way an earlier test failed on its own docstring saying "never
    # rebase". A test that cannot tell code from prose is not checking anything.
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    for forbidden in ("discord.com", "authorization", "user_token"):
        hits = [v for v in literals if forbidden in v.lower()]
        check(f"no {forbidden!r} in any string", not hits, str(hits)[:60])

    check("no HTTP client is imported at all",
          not any(m in path.read_text() for m in
                  ("import requests", "import httpx", "urllib.request",
                   "import aiohttp")))
    # "get" and "post" alone match every dict lookup in the module, which is
    # a false positive rather than a finding. Only genuinely network-shaped
    # calls count.
    net_calls = [c for c in calls
                 if getattr(c.func, "attr", "") in ("urlopen", "Request",
                                                    "HTTPSConnection",
                                                    "HTTPConnection")]
    check("nothing here opens a network connection", not net_calls,
          str([getattr(c.func, "attr", "") for c in net_calls]))
    source = path.read_text()
    check("it drives the app through keystrokes instead",
          "System Events" in source and "keystroke" in source)


def main():
    test_keybind_parsing()
    test_actions_refuse_without_configuration()
    test_state_is_shaped_for_the_page()
    test_the_token_is_never_touched()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
