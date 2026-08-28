#!/usr/bin/env python3
"""Write .env.example from schema.py.

    ./scripts/gen-env-example.py            # write it
    ./scripts/gen-env-example.py --check    # fail if it is out of date

.env.example is documentation that has to stay true, and hand-written
documentation about 84 settings does not. Generating it means adding a setting
to the schema is the only step: the example file, the settings UI, validation
and the update migration all follow from that one edit.

--check is for CI: it regenerates into memory and compares, so a schema change
that forgets to regenerate fails the build rather than shipping a stale file.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import schema  # noqa: E402

HEADER = """\
# Copy to .env and fill in the blanks.
#
# GENERATED FROM schema.py — do not edit by hand, the next regeneration will
# overwrite it. Change a setting's description or default in schema.py and run
# scripts/gen-env-example.py.
#
# Only the four settings marked REQUIRED are needed to start. Everything else
# has a working default, and the advanced ones are worth leaving alone until
# something specific is wrong.
#
# Values here are examples, not secrets. Your real .env holds tokens and should
# be mode 600; it is gitignored and must never be committed.
"""


def wrap(text: str, width: int = 76) -> list:
    out, line = [], "#"
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = "#"
        line += " " + word
    if line != "#":
        out.append(line)
    return out


def render() -> str:
    lines = [HEADER.rstrip()]
    for section in schema.SECTIONS:
        settings = [s for s in schema.SETTINGS if s.section == section]
        if not settings:
            continue
        lines.append("")
        lines.append(f"# ---- {section} " + "-" * max(0, 66 - len(section)))
        for s in settings:
            lines.append("")
            notes = []
            if s.required:
                notes.append("REQUIRED")
            if s.advanced:
                notes.append("advanced")
            if s.platform:
                notes.append({"darwin": "macOS only", "win32": "Windows only"}[s.platform])
            if s.restart == "tts":
                notes.append("restarts the TTS service")
            if s.choices:
                notes.append("one of: " + ", ".join(c or "(blank)" for c in s.choices))
            if s.lo is not None or s.hi is not None:
                notes.append(f"range {s.lo:g}-{s.hi:g}")
            if notes:
                lines += wrap("[" + "] [".join(notes) + "]")
            if s.help:
                lines += wrap(s.help)
            default = s.default
            if default is True:
                default = "true"
            elif default is False:
                default = "false"
            elif default is None:
                default = ""
            lines.append(f"{s.name}={default}")
    return "\n".join(lines) + "\n"


def main() -> int:
    target = ROOT / ".env.example"
    generated = render()
    if "--check" in sys.argv:
        current = target.read_text("utf-8") if target.exists() else ""
        if current == generated:
            print("  .env.example is up to date")
            return 0
        print("  .env.example is STALE — run scripts/gen-env-example.py", file=sys.stderr)
        return 1
    target.write_text(generated, "utf-8")
    print(f"  wrote {target.name}: {len(schema.SETTINGS)} settings, "
          f"{len(generated.splitlines())} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
