"""schema.py must describe the settings config.py actually reads.

The whole value of a schema is that it is the single description. The moment it
drifts from config.py it starts lying to the settings UI, to validation and to
the migration that runs on update — and it drifts silently, because nothing
breaks when a setting is merely undescribed.

So this reads config.py's source, extracts every environment variable it looks
at, and compares. It is deliberately allowed for config.py to read something the
schema does not describe — plenty are internal, or derived, or deliberately not
worth surfacing — but that has to be an explicit decision recorded in UNLISTED
below, not an oversight.

The reverse is never allowed: a schema entry naming a setting config.py never
reads is a control that does nothing.
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schema  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


# Settings config.py reads that the schema deliberately does not describe.
# Each needs a reason; "we forgot" is not one.
UNLISTED = {
    # Derived or internal plumbing, with no sensible control.
    "MPV_IPC_SOCKET": "internal — a socket name, not a preference",
    "MPV_START_TIMEOUT": "internal timeout",
    "PREFS_FILE": "internal path",
    "SPOTIFY_CACHE": "internal path — the OAuth token cache",
    "SPOTIFY_EXE": "platform default, resolved automatically",
    "SPOTIFY_PROCESS": "platform default, resolved automatically",
    "YTDL_PATH": "resolved automatically",
    "TTS_ACK_DIR": "internal path",
    # Deliberately not offered: measured to make things worse, and kept only
    # so the finding is not rediscovered. See the comments in config.py.
    "YTDL_COOKIES_FROM_BROWSER": "measured harmful — documented, not offered",
    "YTDL_COOKIEFILE": "measured harmful — documented, not offered",
    "TTS_EXCLUSIVE": "Windows-only WASAPI concept, forced off on macOS",
    # Tuning nobody should meet in a UI until they have a reason.
    "NL_BACKEND": "only one backend exists",
    "OLLAMA_NUM_GPU": "obsolete on Apple silicon — see config.py",
    "FLAVOR_NUM_GPU": "obsolete on Apple silicon — see config.py",
    # Identity the bot presents to Plex. Changing it would orphan the device
    # entry rather than achieve anything a user wants.
    "PLEX_DEVICE_NAME": "internal — how the bot identifies itself to Plex",
    "PLEX_CLIENT_ID": "internal — derived, stable per machine",
    "COMPUTERNAME": "not a setting — read to seed the Plex client id",
    "TITLE_CACHE_FILE": "internal path",
}


# Settings read by the shell launchers rather than by Python. scripts/_common.sh
# picks the TTS interpreter and flags out of .env before the server starts, so
# these never pass through config.py and an AST walk cannot see them.
LAUNCHER_READ = {"TTS_ENGINE", "TTS_VOICE_REF", "TTS_LANG_CODE",
                 "STREAM_AUDIO_ENABLED", "STREAM_AUDIO_DEVICE",
                 "STREAM_AUDIO_MONITOR"}

# Python files that read settings. flavor.py reads its own length limit rather
# than routing it through config, so scanning config.py alone reported a real
# setting as a phantom control.
SOURCES = ("config.py", "flavor.py")


def env_names_read_by_code() -> set:
    """Every environment variable name the code looks up, from its AST.

    Parsed rather than grepped so that a name built at runtime, or a call
    shaped differently from the usual helpers, does not silently pass.
    """
    root = Path(__file__).resolve().parent.parent
    src = "\n".join((root / f).read_text("utf-8") for f in SOURCES)
    tree = ast.parse(src)
    names = set(LAUNCHER_READ)
    helpers = {"_req", "_int", "_bool", "_float", "_wordlist", "_int_env"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        called = None
        if isinstance(fn, ast.Name):
            called = fn.id
        elif isinstance(fn, ast.Attribute) and fn.attr == "getenv":
            called = "getenv"
        if called not in helpers | {"getenv"}:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def test_no_phantom_controls():
    print("every schema entry names a setting config.py actually reads:")
    read = env_names_read_by_code()
    phantom = sorted(s.name for s in schema.SETTINGS if s.name not in read)
    for name in phantom:
        check(f"{name} is read by config.py", False, "schema describes it, config never reads it")
    check("no phantom settings", not phantom, str(phantom))


def test_nothing_undescribed_by_accident():
    print("\nevery setting config.py reads is described or explicitly unlisted:")
    read = env_names_read_by_code()
    described = set(schema.BY_NAME)
    missing = sorted(read - described - set(UNLISTED))
    for name in missing:
        check(f"{name} described", False, "add to schema.SETTINGS or to UNLISTED with a reason")
    check("nothing silently undescribed", not missing, str(missing))

    stale = sorted(set(UNLISTED) - read)
    check("no stale UNLISTED entries", not stale,
          f"config.py no longer reads {stale}" if stale else "")


def test_every_setting_is_usable():
    print("\nevery setting can drive a widget and be validated:")
    kinds = {"str", "text", "int", "float", "bool", "choice", "words", "list",
             "path", "secret"}
    bad_kind = [s.name for s in schema.SETTINGS if s.kind not in kinds]
    check("known kinds only", not bad_kind, str(bad_kind))

    no_choices = [s.name for s in schema.SETTINGS if s.kind == "choice" and not s.choices]
    check("every choice offers choices", not no_choices, str(no_choices))

    bad_restart = [s.name for s in schema.SETTINGS
                   if s.restart not in ("bot", "tts", "none")]
    check("restart targets are real", not bad_restart, str(bad_restart))

    # A default must itself pass validation, or the shipped config is invalid.
    bad_default = []
    for s in schema.SETTINGS:
        if s.required or s.default in ("", None):
            continue
        raw = "true" if s.default is True else "false" if s.default is False else str(s.default)
        if schema.validate(s.name, raw):
            bad_default.append(f"{s.name}={raw}: {schema.validate(s.name, raw)}")
    check("every default validates", not bad_default, "; ".join(bad_default))

    unhelped = [s.name for s in schema.SETTINGS
                if not s.help and not s.advanced and s.kind != "secret"]
    check("non-advanced settings carry help text", not unhelped, str(unhelped))


def test_secrets_are_marked():
    print("\nanything token-shaped is marked secret, so a UI never renders it:")
    should = {"DISCORD_TOKEN", "PLEX_TOKEN", "SPOTIFY_CLIENT_SECRET"}
    marked = {s.name for s in schema.secrets()}
    check("the known secrets are marked", should <= marked, str(should - marked))
    for s in schema.SETTINGS:
        # TOKEN singular is a credential (DISCORD_TOKEN, PLEX_TOKEN); TOKENS
        # plural is a count (CHAT_MAX_TOKENS). The negative lookahead keeps the
        # check strict without renaming a setting whose unit really is tokens.
        looks_secret = re.search(r"TOKEN(?!S)|SECRET|PASSWORD|_KEY$", s.name)
        if looks_secret and not s.is_secret():
            check(f"{s.name} marked secret", False, "name looks like a credential")
    check("nothing credential-shaped left unmarked", True)


def test_restart_targets_name_the_process_that_reads_it():
    """A setting must bounce whichever process actually reads it.

    TTS_VOICE said restart="tts", which reads as obvious and is wrong: the bot
    sends the voice name on every synthesize call and the speech service only
    falls back to its own --voice when a request omits one. So restarting the
    speech service applied nothing, the panel still reported the new voice
    because it reads .env, and the setting looked simply broken.

    Anything the bot reads through config.* has to restart the bot, whatever
    the setting sounds like it belongs to.
    """
    print("\nrestart targets name the process that reads the setting")
    root = Path(__file__).resolve().parent.parent
    sources = list(root.glob("*.py")) + list(root.glob("commands/**/*.py"))
    read_by_bot = set()
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        read_by_bot |= set(re.findall(r"config\.([A-Z][A-Z0-9_]+)", text))

    # Without this the check below passes trivially when the glob finds nothing.
    check(f"the bot's source was found ({len(read_by_bot)} settings read)",
          len(read_by_bot) > 20)

    wrong = [(s.name, s.restart) for s in schema.SETTINGS
             if s.name in read_by_bot and s.restart != "bot"]
    check("everything the bot reads restarts the bot", not wrong,
          "; ".join(f"{n} says restart={r!r}" for n, r in wrong))


def test_the_example_env_is_not_stale():
    """.env.example is generated, and drifted 23 settings behind anyway.

    It carries a "do not edit by hand, run scripts/gen-env-example.py" header,
    which is exactly the sort of instruction that gets skipped when a setting
    is added in the middle of doing something else. Every IMAGE_* setting was
    missing, so a fresh clone had no way to discover that image generation
    exists, let alone configure it.

    Regenerating is one command. Nothing here fixes it automatically, because
    a test that quietly rewrites a tracked file hides the drift rather than
    reporting it.
    """
    print("\nthe example .env documents every setting")
    example = Path(__file__).resolve().parent.parent / ".env.example"
    if not example.exists():
        check("there is an .env.example at all", False,
              "a fresh clone cannot be configured without one")
        return
    text = example.read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
    missing = sorted(s.name for s in schema.SETTINGS
                     if s.name not in documented)
    check(f"all {len(schema.SETTINGS)} settings appear", not missing,
          ("run scripts/gen-env-example.py — missing: "
           + ", ".join(missing[:6]) + ("..." if len(missing) > 6 else ""))
          if missing else "")


def main():
    test_no_phantom_controls()
    test_nothing_undescribed_by_accident()
    test_every_setting_is_usable()
    test_secrets_are_marked()
    test_restart_targets_name_the_process_that_reads_it()
    test_the_example_env_is_not_stale()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
