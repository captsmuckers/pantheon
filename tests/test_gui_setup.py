"""The first-run wizard runs commands. What it can run must be a fixed menu.

Two things are worth testing here and the second is the important one.

The probe is easy: it must describe every step well enough to act on, and never
claim a state it has not checked.

The actions are not. This is the one place in the panel that creates
virtualenvs and invokes pip, so the test that matters is not that installing
works — it is that nothing arriving from a request can change WHAT gets run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import setup  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_actions_are_a_fixed_menu():
    print("nothing from a request can decide what gets run:")
    hostile = ["", "rm -rf /", "../../evil", "make_venv; rm -rf /", "pip install evil",
               "MAKE_VENV", "make_venv make_env", None, 0, "brew"]
    for evil in hostile:
        result = setup.start(evil)
        refused = not result.get("ok") and "No such setup action" in result.get("message", "")
        check(f"{evil!r} refused", refused, str(result.get("message", ""))[:60])

    check("the menu is exactly the known six",
          set(setup.ACTIONS) == {"brew_tools", "make_venv", "make_env",
                                 "pull_model", "make_tts_venv", "install_voice_deps"},
          str(sorted(setup.ACTIONS)))

    # Every argv must be built here, from literals and paths this module
    # computes — never from anything passed in.
    for name, spec in setup.ACTIONS.items():
        steps = spec["steps"]()
        flat = [part for step in steps for part in step]
        check(f"{name}: every argument is a string",
              all(isinstance(p, str) for p in flat), str(flat)[:70])
        check(f"{name}: no shell metacharacters",
              not any(c in p for p in flat for c in ";|&$`\n"), str(flat)[:70])


def test_env_creation_never_destroys():
    print("\ncreating .env cannot overwrite one that exists:")
    # The real repo has a .env, which is exactly the case that must be refused.
    existing = (setup.ROOT / ".env").exists()
    result = setup.start("make_env")
    if existing:
        check("refused when .env is already there",
              not result.get("ok") and "already exists" in result.get("message", ""),
              result.get("message", ""))
    else:
        check("(no .env here, so nothing to protect)", True)


def test_probe_is_actionable():
    print("\nevery step says what it is, why, and what to do:")
    d = setup.probe()
    check("probe reports readiness", isinstance(d.get("ready"), bool))
    check("there are steps", len(d.get("steps", [])) >= 5, str(len(d.get("steps", []))))

    states = {"ok", "todo", "optional"}
    for s in d["steps"]:
        for field in ("key", "title", "why", "state", "detail"):
            present = s.get(field) is not None
            check(f"{s.get('key', '?')}: has {field}", present,
                  "" if present else f"missing {field}")
        check(f"{s['key']}: state is known", s["state"] in states, s["state"])
        # A step that is not done has to say how to finish it, or it is just
        # a complaint.
        if s["state"] != "ok":
            check(f"{s['key']}: offers a way forward",
                  bool(s.get("fix") or s.get("manual")),
                  "neither an action nor a command")
        if s.get("fix"):
            check(f"{s['key']}: its fix is a real action",
                  s["fix"] in setup.ACTIONS, s["fix"])


def test_job_lookup_survives_nonsense():
    print("\nasking about a job that does not exist:")
    for bad in ("", "nope", "../../etc/passwd", "0" * 200):
        r = setup.job(bad)
        check(f"{bad[:20]!r} handled", not r.get("ok") and "message" in r)


def main():
    test_actions_are_a_fixed_menu()
    test_env_creation_never_destroys()
    test_probe_is_actionable()
    test_job_lookup_survives_nonsense()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
