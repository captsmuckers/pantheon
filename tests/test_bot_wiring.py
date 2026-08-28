"""Import bot.py and check the Discord-facing wiring, without connecting."""

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as bot_mod  # noqa: E402
import config  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))

    async def defer(self):
        pass


class FakeInteraction:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.response = FakeResponse()


async def test_guard_responds():
    """A refused command must answer, or Discord shows 'did not respond'."""
    print("slash command guard:")
    check("_guard is async", inspect.iscoroutinefunction(bot_mod._guard))

    wrong = FakeInteraction(config.ALLOWED_CHANNEL_ID + 1)
    ok = await bot_mod._guard(wrong)
    check("wrong channel refused", ok is False)
    check("wrong channel got a reply", len(wrong.response.sent) == 1,
          str(wrong.response.sent))
    check("reply is ephemeral", wrong.response.sent[0][1].get("ephemeral") is True)

    # player/brain are None before startup.
    right = FakeInteraction(config.ALLOWED_CHANNEL_ID)
    ok = await bot_mod._guard(right)
    check("not-ready refused", ok is False)
    check("not-ready got a reply", len(right.response.sent) == 1)
    check("says starting up", "starting up" in (right.response.sent[0][0] or ""),
          str(right.response.sent[0][0]))

    # brain missing but player present — the window the old guard let through.
    bot_mod.player = object()
    try:
        half = FakeInteraction(config.ALLOWED_CHANNEL_ID)
        ok = await bot_mod._guard(half)
        check("player-without-brain refused", ok is False)
        check("and answered", len(half.response.sent) == 1)
    finally:
        bot_mod.player = None


async def test_every_command_awaits_guard():
    print("\nall slash commands await the guard:")
    src = Path(bot_mod.__file__).read_text(encoding="utf-8")
    bare = src.count("if not _guard(")
    awaited = src.count("if not await _guard(")
    check("no un-awaited guard calls left", bare == 0, f"found {bare}")
    check(f"{awaited} commands guarded", awaited >= 15, f"{awaited}")

    commands = list(bot_mod.bot.tree.get_commands())
    check("slash commands registered", len(commands) >= 15, f"{len(commands)} commands")
    names = {c.name for c in commands}
    expected = {"play", "queue", "status", "pause", "resume", "skip", "stop",
                "forward", "rewind", "music", "musicqueue", "karaoke", "speed",
                "audio", "subs", "tracks", "restart", "help"}
    missing = expected - names
    check("no command lost in the rewrite", not missing, f"missing={missing or 'none'}")


async def test_picker_expiry():
    print("\npicker expiry:")
    from brain import MusicChoice

    choice = MusicChoice([{"uri": "spotify:track:1", "kind": "track", "label": "A Song"}],
                         "a song")
    view = bot_mod.build_picker(choice)
    check("built an ExpiringView", isinstance(view, bot_mod.ExpiringView))
    check("starts enabled", all(not c.disabled for c in view.children))

    edited = {}

    class FakeMessage:
        async def edit(self, **kw):
            edited.update(kw)

    view.message = FakeMessage()
    await view.on_timeout()
    check("controls disabled on timeout", all(c.disabled for c in view.children))
    check("message updated", "expired" in (edited.get("content") or ""), str(edited.get("content")))

    # No message assigned must not raise.
    view2 = bot_mod.build_picker(choice)
    await view2.on_timeout()
    check("survives a missing message reference", True)


async def test_shutdown_path():
    print("\nshutdown wiring:")
    check("_run exists", inspect.iscoroutinefunction(bot_mod._run))
    src = Path(bot_mod.__file__).read_text(encoding="utf-8")
    # Ignore prose: the docstring names the old pattern to explain the fix.
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#") and "previous version" not in ln
    )
    check("no asyncio.run(player.shutdown()) in code",
          "asyncio.run(player.shutdown())" not in code)
    check("shutdown awaited inside the bot's loop",
          "await player.shutdown()" in src)
    check("post_notice wired to the player",
          "player.on_notice = post_notice" in src)
    check("spotify connect is backgrounded",
          "create_task(_connect_spotify" in src)
    check("brain built before spotify connects",
          src.index("brain = Brain(") < src.index("create_task(_connect_spotify"))


async def main():
    await test_guard_responds()
    await test_every_command_awaits_guard()
    await test_picker_expiry()
    await test_shutdown_path()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)


asyncio.run(main())
