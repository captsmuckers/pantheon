"""Updating runs git against a checkout somebody's install lives in.

So the tests that matter are the refusals. Applying an update is easy to get
right and easy to verify; what has to be certain is that it never merges, never
resets, and never touches a tree with work in it — because the failure mode is
destroying something that exists nowhere else.

Uses real git repositories in a temporary directory, with one serving as the
other's origin. No network, and nothing here can reach the real repository.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import updates  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def git(repo, *args, **kw):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, **kw)


def commit(repo, filename, text, message):
    (Path(repo) / filename).parent.mkdir(parents=True, exist_ok=True)
    with open(Path(repo) / filename, "a") as fh:
        fh.write(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)


class Pair:
    """An origin repository and a clone of it, both real, both local."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pantheon-upd-"))
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        commit(self.origin, "README.md", "hello\n", "first")
        self.clone = self.dir / "clone"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)],
                       capture_output=True)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def with_root(root, fn):
    """Run fn with updates.ROOT pointed at a temporary checkout."""
    original = updates.ROOT
    updates.ROOT = Path(root)
    try:
        return fn()
    finally:
        updates.ROOT = original


def test_restart_mapping():
    print("a changed file maps to the service that has to be bounced:")
    cases = [
        (["bot.py", "brain.py"], ["bot"], "bot code"),
        (["tts/tts_server.py"], ["tts"], "speech code"),
        (["gui/server.py"], ["panel"], "panel code"),
        (["README.md"], [], "documentation only"),
        (["scripts/start-gui.sh"], [], "a helper script"),
        (["tests/test_voice.py"], [], "a test"),
        (["requirements.txt"], ["bot"], "the bot's dependencies"),
        (["bot.py", "tts/requirements.txt", "gui/pages.py"],
         ["tts", "bot", "panel"], "all three"),
    ]
    for changed, want, label in cases:
        got = updates._restarts_for(changed)
        check(label, got == want, f"{got} wanted {want}")


def test_not_a_repo():
    print("\nan install that is not a git checkout says so:")
    tmp = Path(tempfile.mkdtemp(prefix="pantheon-norepo-"))
    try:
        d = with_root(tmp, updates.status)
        check("reports not-a-repo", d.get("kind") == "not-a-repo", str(d.get("kind")))
        check("says how to fix it", "clone" in (d.get("hint") or ""), str(d.get("hint")))
        check("apply refuses", not with_root(tmp, updates.apply).get("ok"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sees_and_applies_an_update():
    print("\nan update is seen, and fast-forwarded:")
    p = Pair()
    try:
        d = with_root(p.clone, updates.status)
        check("clean checkout is not blocked", "blocked" not in d, str(d.get("blocked")))
        check("nothing to do yet", d["behind"] == 0, str(d["behind"]))

        commit(p.origin, "README.md", "second line\n", "a new commit")
        d = with_root(p.clone, updates.plan)
        check("the new commit is seen", d["behind"] == 1, str(d["behind"]))
        check("it is described", d["commits"][0]["subject"] == "a new commit",
              str(d.get("commits")))
        check("can_update is true", d.get("can_update") is True)

        before = git(p.clone, "rev-parse", "HEAD").stdout.strip()
        result = with_root(p.clone, updates.apply)
        check("apply starts a job", result.get("ok") and result.get("job"),
              str(result)[:70])
        if result.get("job"):
            from gui import setup as setup_mod
            import time
            for _ in range(80):
                j = setup_mod.job(result["job"])
                if j.get("done"):
                    break
                time.sleep(0.25)
            check("the job succeeded", j.get("rc") == 0, str(j.get("lines"))[-120:])
        after = git(p.clone, "rev-parse", "HEAD").stdout.strip()
        check("the checkout actually moved", before != after)
        check("and is now up to date",
              with_root(p.clone, updates.status)["behind"] == 0)
    finally:
        p.close()


def test_refuses_to_touch_work():
    print("\nit refuses anything that could lose work:")
    p = Pair()
    try:
        commit(p.origin, "README.md", "upstream\n", "upstream commit")

        # A modified tracked file.
        with open(p.clone / "README.md", "a") as fh:
            fh.write("my edit\n")
        d = with_root(p.clone, updates.plan)
        check("a dirty tree blocks", "uncommitted" in (d.get("blocked") or ""),
              str(d.get("blocked"))[:60])
        check("and names the file", "README.md" in d.get("dirty", []), str(d.get("dirty")))
        check("apply refuses too", not with_root(p.clone, updates.apply).get("ok"))
        git(p.clone, "checkout", "--", "README.md")

        # An untracked file must NOT block: a stray note is not a reason to
        # refuse every future update.
        (p.clone / "notes.txt").write_text("mine\n")
        d = with_root(p.clone, updates.plan)
        check("an untracked file does not block", "blocked" not in d,
              str(d.get("blocked")))
        os.remove(p.clone / "notes.txt")

        # A local commit — diverged, needs a human.
        commit(p.clone, "README.md", "local work\n", "a local commit")
        d = with_root(p.clone, updates.plan)
        check("a diverged checkout blocks", bool(d.get("blocked")),
              str(d.get("blocked"))[:70])
        check("apply refuses", not with_root(p.clone, updates.apply).get("ok"))
    finally:
        p.close()


def test_only_ever_fast_forwards():
    """No git subcommand that can lose work may appear in an executed command.

    Checked against the arguments actually passed to calls, not against the
    file's text: the first version grepped the whole source and failed on its
    own docstring, which says "never rebase, never reset". A test that cannot
    tell a command from a comment about commands is not checking anything.
    """
    print("\nthe only git command that can move HEAD is a fast-forward:")
    import ast
    source = (Path(__file__).resolve().parent.parent / "gui" / "updates.py").read_text()
    tree = ast.parse(source)

    # Every string that is passed as an argument to a call, or sits in a list
    # literal — which is how the step lists are written.
    words = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    words.append(a.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for e in node.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    words.append(e.value)

    check("some commands were found to check", len(words) > 5, str(len(words)))
    for forbidden in ("rebase", "reset", "clean", "--force", "-f",
                      "--hard", "checkout", "push"):
        hits = [w for w in words if w == forbidden]
        check(f"never runs git {forbidden}", not hits, str(hits))
    check("the merge is --ff-only", "--ff-only" in words)
    check("and it is a merge, not a pull",
          "merge" in words and "pull" not in words)


def main():
    test_restart_mapping()
    test_not_a_repo()
    test_sees_and_applies_an_update()
    test_refuses_to_touch_work()
    test_only_ever_fast_forwards()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
