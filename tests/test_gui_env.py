"""The settings page rewrites .env. It must not damage it.

Every case here is something that has either gone wrong already or is one
plausible edit away from going wrong. The file being written holds a Discord
token and is the only reason the bot can start, so "mostly correct" is not a
useful standard for it.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import envfile  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def scratch(text: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="athena-env-"))
    p = d / ".env"
    p.write_text(text, encoding="utf-8")
    return p


SAMPLE = """\
# Discord
DISCORD_TOKEN=abc123
ALLOWED_CHANNEL_ID=786436748414550026

# The persona. Long, and full of punctuation.
BOT_PERSONA=You are Athena. Be brief; don't waffle.
OLLAMA_MODEL=qwen3:8b
"""


def test_reads_what_dotenv_reads():
    print("reads the same values python-dotenv would:")
    p = scratch(SAMPLE)
    got = envfile.read(p)
    check("plain value", got.get("DISCORD_TOKEN") == "abc123")
    check("value with punctuation", got.get("BOT_PERSONA", "").endswith("don't waffle."))
    check("comments are not settings", "# Discord" not in got and len(got) == 4, str(sorted(got)))

    q = scratch('A="quoted value"\nB=trailing space   \nC=has # hash\nD="line\\nbreak"\n')
    got = envfile.read(q)
    check("quotes stripped", got.get("A") == "quoted value", repr(got.get("A")))
    check("unquoted value stripped", got.get("B") == "trailing space", repr(got.get("B")))
    check("inline comment dropped", got.get("C") == "has", repr(got.get("C")))
    check("escaped newline decoded", got.get("D") == "line\nbreak", repr(got.get("D")))

    r = scratch("export FOO=1\n  BAR = 2\n")
    got = envfile.read(r)
    check("export prefix tolerated", got.get("FOO") == "1")
    check("whitespace around = tolerated", got.get("BAR") == "2", repr(got.get("BAR")))

    # scripts/_common.sh resolves a repeated key with `tail -1`. If this
    # disagreed, the GUI and the launcher would show different configurations.
    d = scratch("SAME=first\nSAME=second\n")
    check("last occurrence wins, as the launcher does", envfile.read(d).get("SAME") == "second")


def test_edits_preserve_the_file():
    print("\nan edit changes one line and nothing else:")
    p = scratch(SAMPLE)
    before = p.read_text("utf-8")
    envfile.write(p, {"OLLAMA_MODEL": "granite4:3b"})
    after = p.read_text("utf-8")

    check("comments survive", "# Discord" in after and "# The persona." in after)
    check("order survives", after.index("DISCORD_TOKEN") < after.index("BOT_PERSONA"))
    check("the value changed", envfile.read(p)["OLLAMA_MODEL"] == "granite4:3b")
    check("nothing else changed",
          [l for l in before.splitlines() if "OLLAMA_MODEL" not in l]
          == [l for l in after.splitlines() if "OLLAMA_MODEL" not in l])


def test_unknown_keys_survive():
    print("\na setting this build has never heard of is not thrown away:")
    # ANTHROPIC_API_KEY is the real case: the backend was removed, and a .env
    # written before that still carries the key.
    p = scratch("ANTHROPIC_API_KEY=sk-old\nOLLAMA_MODEL=qwen3:8b\n")
    envfile.write(p, {"OLLAMA_MODEL": "granite4:3b"})
    check("kept on edit", envfile.read(p).get("ANTHROPIC_API_KEY") == "sk-old")
    envfile.write(p, {"OLLAMA_MODEL": "qwen3:8b"}, prune=("ANTHROPIC_API_KEY",))
    check("removed only when asked", "ANTHROPIC_API_KEY" not in envfile.read(p))


def test_appending_to_a_file_without_a_trailing_newline():
    print("\nappending to a file whose last line has no newline:")
    # This exact case produced `WHISPER_CPU_THREADS=6MPV_NATIVE_FS=yes` in a
    # real .env and took a while to spot.
    p = scratch("WHISPER_CPU_THREADS=6")          # deliberately no "\n"
    envfile.write(p, {"MPV_NATIVE_FS": "yes"})
    got = envfile.read(p)
    check("the existing setting is intact", got.get("WHISPER_CPU_THREADS") == "6", repr(got))
    check("the new setting is its own line", got.get("MPV_NATIVE_FS") == "yes", repr(got))
    check("the file ends with a newline", p.read_text("utf-8").endswith("\n"))


def test_values_that_need_quoting_survive_a_round_trip():
    print("\nawkward values survive being written and read back:")
    p = scratch("X=1\n")
    awkward = {
        "HASH": "red #1 pick",
        "SPACES": "  padded  ",
        "QUOTE": 'she said "no"',
        "APOS": "don't",
        "BACKSLASH": r"C:\path\to",
        "NEWLINE": "two\nlines",
        "EMPTY": "",
        "PERSONA": "You are Athena — dry, brief; never say \"as an AI\". 100% real.",
    }
    envfile.write(p, awkward)
    got = envfile.read(p)
    for k, want in awkward.items():
        check(f"{k} round-trips", got.get(k) == want, f"wrote {want!r}, read {got.get(k)!r}")


def test_permissions():
    print("\nthe file holding the tokens is not readable by anyone else:")
    p = scratch("A=1\n")
    os.chmod(p, 0o777)
    envfile.write(p, {"A": "2"})
    mode = p.stat().st_mode & 0o777
    check("mode is 0600 after a write", mode == 0o600, oct(mode))
    leftovers = [f.name for f in p.parent.iterdir() if f.name != ".env"]
    check("no temporary file left behind", not leftovers, str(leftovers))


def test_write_reports_only_real_changes():
    print("\nsaving an unedited form restarts nothing:")
    p = scratch(SAMPLE)
    moved = envfile.write(p, {"OLLAMA_MODEL": "qwen3:8b", "DISCORD_TOKEN": "abc123"})
    check("no change reported when nothing moved", moved == [], str(moved))
    moved = envfile.write(p, {"OLLAMA_MODEL": "qwen3:8b", "DISCORD_TOKEN": "different"})
    check("only the edited key is reported", moved == ["DISCORD_TOKEN"], str(moved))


def test_a_failed_write_leaves_the_original():
    print("\na write that fails does not leave a half-written .env:")
    p = scratch(SAMPLE)
    original = p.read_text("utf-8")
    try:
        # A directory is not writable as a file: the rename must fail.
        envfile._atomic_write(p.parent, "nonsense")
    except Exception:
        pass
    check("original intact", p.read_text("utf-8") == original)
    leftovers = [f.name for f in p.parent.iterdir() if f.name != ".env"]
    check("no temporary file left behind", not leftovers, str(leftovers))


def main():
    test_reads_what_dotenv_reads()
    test_edits_preserve_the_file()
    test_unknown_keys_survive()
    test_appending_to_a_file_without_a_trailing_newline()
    test_values_that_need_quoting_survive_a_round_trip()
    test_permissions()
    test_write_reports_only_real_changes()
    test_a_failed_write_leaves_the_original()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
