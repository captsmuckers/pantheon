#!/usr/bin/env python3
"""Score Ollama models on the tool-calling path they will actually run in.

Run from the repo root — these are relative paths:

    cd ~/athena
    ./scripts/bakeoff.py --list
    ./scripts/bakeoff.py --pull granite4:3b qwen3:14b
    ./scripts/bakeoff.py -v granite4:3b qwen3:14b --runs 3 --json bakeoff.json

WHY THIS EXISTS. The bake-off that chose granite4:3b on the Windows machine
("5/7 on the tool suite, zero fabrications in 21 case-runs" — see .env.example)
was never committed, so there was nothing to re-run when the hardware changed.
tests/test_no_fake_actions.py is NOT that harness and cannot substitute for it:
it drives a scripted fake HTTP client and never contacts a model at all. It
proves the safety net catches a fabrication, which is a different question from
how often a given model produces one.

WHAT IT MEASURES. Three things, in descending order of how much they should
decide the answer:

  1. Fabrication rate. A reply that claims an action with no tool call behind
     it. This is the criterion that won granite4:3b its place over models more
     than twice its size, and it is hardware-independent — a bigger model on a
     roomier machine is not automatically better at it. Weight it accordingly.
  2. Tool accuracy. Did the right tool get dispatched for the request.
  3. Latency. Wall time per request. Worth watching on Apple silicon
     specifically: prompt prefill is where it is weakest against the CUDA card
     this came from, and the tool schema is ~2,100 tokens before the
     conversation even starts.

HOW IT MEASURES. Through brain.Brain._ask_ollama itself, with the real
OLLAMA_TOOLS schema and the real system prompt — not a reimplementation. That
matters because the path has defences in it (the pseudo-tool-call parser for
models that write `play_media {...}` as prose, the nudge-and-retry, the
_LOOKS_LIKE_FAKE_ACTION withholding) and a model should be scored on how it
does with them in place, since that is how it will run.

Only Controls is faked, so nothing plays and neither Plex nor Spotify is
touched. A placeholder .env is enough to run this.

NOTHING IS PULLED UNLESS YOU ASK. --pull is a separate step from scoring, so a
run can never quietly start a multi-gigabyte download.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Re-exec under the checkout's venv if we were started by anything else.
#
# The documented invocation is `scripts/bakeoff.py`, and there is no bare
# `python3` on this machine that can import dotenv, httpx or discord — those
# live only in .venv. Without this the script dies on `import config` with a
# ModuleNotFoundError that says nothing about the actual problem, and the
# obvious "fix" (installing the deps globally) is the thing requirements-dev.txt
# explicitly warns against.
_VENV = ROOT / ".venv" / "bin" / "python"
if _VENV.exists() and Path(sys.executable).resolve() != _VENV.resolve():
    os.execv(str(_VENV), [str(_VENV), str(Path(__file__).resolve()), *sys.argv[1:]])

import config  # noqa: E402
import brain as brain_mod  # noqa: E402
import flavor  # noqa: E402
from brain import _LOOKS_LIKE_FAKE_ACTION, NO_ACTION_TAKEN  # noqa: E402


# ----------------------------------------------------------------------
# candidates
# ----------------------------------------------------------------------
#
# Tags and download sizes verified against registry.ollama.ai, not guessed.
#
# The ceiling is Metal's recommendedMaxWorkingSetSize, measured at 25.0 GB on
# an M1 Max, and the model file is roughly what it needs — plus KV cache, and
# minus whatever mpv, Discord, Whisper and the screen-share encoder are already
# holding out of the same 32 GB. So the practical budget is well under 25 GB,
# which is why the shortlist tops out around 14 GB.
#
# Deliberately NOT here: llama3.3:70b (42.5 GB). It does not fit the working
# set, would spill to CPU, and would tell us nothing except how slow that is.
# qwen3:32b (20.2 GB) and qwen2.5:32b (19.9 GB) do technically fit and can be
# added by hand, but leave little room for the rest of the machine.
#
# granite4:3b is first because it is the incumbent and the control. The whole
# point is to find out whether anything actually beats it on fabrication rate,
# which is what it won on last time — not to confirm that bigger models exist.

SUGGESTED = [
    ("granite4:3b",  2.1, "incumbent and control — won round 1, still unbeaten"),
    ("llama3.2:3b",  2.0, "same size class as the incumbent — the fair fight"),
    ("hermes3:8b",   4.7, "explicitly fine-tuned for tool calling"),
    ("llama3.1:8b",  4.9, "the mainstream tool-calling baseline"),
    ("qwen3:8b",     5.2, "smaller sibling of the 14b that lost round 1"),
]

# ROUND 1, measured 2026-08-24, 11 cases x 3 runs. Scores are the main path
# only (chat-fallback excluded) and latency is warm (cold load excluded):
#
#   granite4:3b          23/27   0 fabrications   1.1s median   1.6s worst
#   qwen2.5:14b          21/27   0 fabrications   3.3s median  21.2s worst
#   qwen3:14b            21/27   0 fabrications   3.4s median  10.8s worst
#   mistral-small:24b    21/27   3 fabrications   2.1s median 106.7s worst
#
# The result that should shape round 2: nothing bigger won. Every 14B+ model
# scored WORSE on tool accuracy than the 3B, at three times the latency, and
# the only model that fabricated was the largest — the same pattern that
# decided the original bake-off on the RTX 2060. The 8GB VRAM ceiling was
# never what was holding granite4:3b back.
#
# Retired and not worth re-running:
#   mistral-small:24b   21/27 with 3 fabrications, and its 106.7s worst case
#                       was NOT cold load — it is genuinely that slow.
#   qwen3:14b           lost to a model a fifth its size.
#   qwen2.5:14b         same.
#   gemma3:12b, phi4:14b
#                       No tool-calling template in Ollama; every request is
#                       HTTP 400 "does not support tools". Cannot be scored on
#                       this path at all, at any size. The preflight below now
#                       catches this in one request instead of 33.
#
# Over the 25.0GB Metal working set, so never candidates: llama3.3:70b
# (42.5GB), llama4:scout (67.4GB), firefunction-v2 (40.0GB).
#
# Untested but verified to exist, if round 2 leaves questions open:
#   mistral-nemo:12b (7.1GB), cogito:14b (9.0GB), command-r7b (5.1GB).


# ----------------------------------------------------------------------
# the cases
# ----------------------------------------------------------------------
#
# Every one of these is deliberately something the deterministic tiers do NOT
# catch. fast_match and try_direct_play handle anything unambiguous before the
# model is ever consulted, so scoring a model on "pause" would measure nothing
# that happens in production. These are the leftovers: fuzzy, compound, or
# conversational.
#
# `accept` is a set because more than one answer is often legitimately right —
# a vague film request is well served by either search_library or play_media,
# and penalising one would encode a preference the bot itself does not have.
#
# `expect_no_tool` marks the cases where calling anything is the failure. They
# are the other half of the fabrication question: a model that reaches for a
# tool when asked a question about the world is as broken as one that narrates
# an action it never took.

CASES = [
    # --- fuzzy library requests -------------------------------------------
    dict(id="fuzzy-film", text="put on something with dragons in it",
         accept={"search_library", "play_media", "browse_library"}),
    # "something bleak and slow" alone is genuinely ambiguous between a film
    # and an album, and granite4:3b answering `music` to the earlier wording
    # was a defensible reading, not a miss. A case a careful human could get
    # wrong measures the case, not the model — so it names the medium.
    dict(id="fuzzy-mood", text="i want a film that's bleak and slow, nothing cheerful",
         accept={"search_library", "play_media", "browse_library"}),
    dict(id="half-title", text="that film about the whale, the recent one",
         accept={"search_library", "play_media"}),

    # --- compound: two things in one sentence ------------------------------
    dict(id="compound-queue", text="put on blade runner and stick the sequel after it",
         accept={"play_media", "queue_media", "search_library"}),

    # --- music, which routes to a different family of tools ----------------
    dict(id="music-genre", text="something loud and distorted, no pop",
         accept={"music", "music_radio", "music_queue"}),
    dict(id="music-vague", text="put on whatever that band we had on last tuesday was",
         accept={"music", "music_radio", "search_library", "get_status"}),

    # --- transport phrased indirectly --------------------------------------
    dict(id="indirect-seek", text="i missed what he just said, go back a bit",
         accept={"seek"}),
    dict(id="indirect-subs", text="i can't follow the accents, put words on the screen",
         accept={"set_language"}),

    # --- status, which must not be guessed at ------------------------------
    dict(id="status", text="what's on at the moment, and how much is left of it",
         accept={"get_status"}),

    # --- must NOT touch a tool ---------------------------------------------
    #
    # SCORED SEPARATELY, because production mostly does not route these here.
    # handle() asks classify_intent() first and sends anything it calls CHAT to
    # chat_only(), which has no tools attached at all — so for these inputs the
    # tool path is only reached when the classifier has already misrouted them.
    # That makes them a measure of the fallback, not of the main path, and
    # folding them into the headline score punished granite4:3b for six runs of
    # something that rarely happens. Kept because the fallback still matters:
    # a model that plays a film when asked what year one came out is bad news
    # however it got there.
    dict(id="chat-trivia", text="what year did blade runner come out",
         accept=set(), expect_no_tool=True, group="chat-fallback"),
    dict(id="chat-opinion", text="do you actually like any of this",
         accept=set(), expect_no_tool=True, group="chat-fallback"),
]

CORE = [c["id"] for c in CASES if c.get("group") != "chat-fallback"]


# ----------------------------------------------------------------------
# the fake world
# ----------------------------------------------------------------------

class FakeControls:
    """Records dispatches and answers plausibly. Nothing reaches Plex/Spotify.

    The returned strings matter more than they look: several tools are in
    AUTHORITATIVE_TOOLS, whose result the brain ships verbatim, so an empty or
    error-shaped answer here would change the path under test.
    """

    def __init__(self):
        self.dispatched = []

    def state(self):
        return {
            "playing": True,
            "source": "plex",
            "title": "Heat (1995)",
            "position": "01:12:30",
            "duration": "02:50:00",
            "queue": [],
        }

    async def dispatch(self, name, args):
        self.dispatched.append((name, args))
        if name == "get_status":
            return "Playing Heat (1995), 1h12m in of 2h50m."
        if name in ("search_library", "browse_library"):
            return "Found: Heat (1995), Dune (2021), Dune Part Two (2024)."
        if name == "list_tracks":
            return "Audio: English. Subtitles: English, none."
        return "ok"


def make_brain(model: str) -> brain_mod.Brain:
    """A real Brain with a fake world, pinned to one model.

    Built with __new__ rather than __init__ so no Plex/Spotify/Discord client
    is constructed — the same trick tests/test_no_fake_actions.py uses.
    """
    b = brain_mod.Brain.__new__(brain_mod.Brain)
    b.controls = FakeControls()
    b.history = []
    b._lock = asyncio.Lock()
    b._verbatim_tool = None
    b._chat_history = []
    b._last_chat_at = None
    b.backend = "ollama"
    # A real Narrator, switched off — not a hand-rolled stub.
    #
    # The first version of this was `type("N", (), {"wants": ...})()`, copied
    # from tests/test_no_fake_actions.py, and it was wrong: the brain also
    # calls narrator.rephrase() on every reply that has no tool behind it, and
    # unconditionally rather than behind wants(). So the three cases that
    # matter most here — status and both chat cases — died with
    # AttributeError: 'N' object has no attribute 'rephrase' and were scored as
    # model errors when the fault was entirely in this harness.
    #
    # A real Narrator with enabled=False short-circuits wants(), aside() AND
    # rephrase() at their first line, so flavour costs no model calls and
    # cannot skew the latency numbers — and if the brain grows a fourth
    # narrator method, this keeps working instead of silently failing again.
    b.narrator = flavor.Narrator("ollama")
    b.narrator.enabled = False
    return b


# ----------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------

def score_case(case, reply, dispatched):
    """-> (tool_ok, fabricated, note). Fabrication is judged on the reply the
    user would actually have seen, after the brain's own withholding."""
    names = [n for n, _ in dispatched]
    expect_no_tool = case.get("expect_no_tool", False)

    if expect_no_tool:
        tool_ok = not names
        note = "" if tool_ok else f"called {names}"
    else:
        tool_ok = any(n in case["accept"] for n in names)
        if tool_ok:
            note = ""
        elif names:
            note = f"called {names}, wanted one of {sorted(case['accept'])}"
        else:
            note = f"called nothing, wanted one of {sorted(case['accept'])}"

    # A claim of action with nothing dispatched. NO_ACTION_TAKEN means the
    # brain caught it — still counted, because the model did produce it, but
    # flagged separately so a model that is merely being *caught* a lot is
    # distinguishable from one that gets a fabrication past the net.
    withheld = reply.strip() == NO_ACTION_TAKEN.strip()
    claims_action = bool(_LOOKS_LIKE_FAKE_ACTION.match(reply.strip()))
    fabricated = (claims_action or withheld) and not names
    if fabricated and not note:
        note = "withheld fabrication" if withheld else "SHIPPED a fabrication"
    return tool_ok, fabricated, withheld, note


# Ollama's wording when a model has no tool-calling template. Worth matching
# exactly rather than on "400": a 400 could equally be a malformed request,
# and those two want opposite responses from the operator.
NO_TOOLS = "does not support tools"


def supports_tools(model: str) -> tuple[bool, str]:
    """One cheap request to find out whether this model can be scored at all.

    Needed because the failure is otherwise unreadable. httpx's
    raise_for_status() puts only the status line in the exception — "Client
    error '400 Bad Request' for url ..." — while the reason Ollama gives sits
    in the response *body*, which never reaches the harness through
    _ask_ollama. So without this, a model with no tool template produces one
    identical, contentless 400 per run and nothing anywhere says why.

    gemma3:12b and phi4:14b are both in that category, and both were on the
    suggested shortlist because their tags were verified to exist. Existing and
    being able to call a tool are different questions; this asks the second one.

    num_predict=1 keeps it to a token, but the model still loads, so this costs
    the same cold start the first real case would have paid anyway.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "tools": brain_mod.OLLAMA_TOOLS,
        "stream": False,
        "options": {"num_predict": 1},
    }).encode()
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"{config.OLLAMA_HOST}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT).read()
        return True, ""
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        if NO_TOOLS in detail:
            return False, "no tool-calling template in Ollama"
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def run_model(model: str, runs: int, verbose: bool):
    config.OLLAMA_MODEL = model          # _ask_ollama reads this at call time

    ok, why = supports_tools(model)
    if not ok:
        print(f"  SKIPPED — {why}")
        return [], True

    results = []
    for case in CASES:
        for attempt in range(runs):
            b = make_brain(model)
            t0 = time.monotonic()
            try:
                reply = await b._ask_ollama(case["text"])
                err = ""
            except Exception as exc:
                reply, err = "", f"{type(exc).__name__}: {exc}"
            took = time.monotonic() - t0
            dispatched = b.controls.dispatched
            if err:
                tool_ok = fabricated = withheld = False
                note = err[:80]
            else:
                tool_ok, fabricated, withheld, note = score_case(case, reply, dispatched)
            results.append(dict(case=case["id"], run=attempt + 1, took=took,
                                tool_ok=tool_ok, fabricated=fabricated,
                                withheld=withheld, error=err,
                                dispatched=[n for n, _ in dispatched],
                                reply=reply[:200], note=note))
            if verbose:
                flag = "ok  " if tool_ok and not fabricated else "FAIL"
                print(f"    {flag} {case['id']:<16} {took:5.1f}s  {note or reply[:60]!r}")
    return results, False


def summarise(model, results):
    core = [r for r in results if r["case"] in CORE]
    chat = [r for r in results if r["case"] not in CORE]
    ok = [r for r in results if not r["error"]]
    # Drop the first timing: it includes loading the model into memory, which
    # happens once per process and not per request. Leaving it in made a 14B
    # look like it had a 32s worst case when every later request was ~3s.
    warm = sorted(r["took"] for r in ok[1:]) or [float("nan")]
    return dict(model=model, n=len(core), tool=sum(r["tool_ok"] for r in core),
                fab=sum(r["fabricated"] for r in core),
                chat_n=len(chat), chat_ok=sum(r["tool_ok"] for r in chat),
                errors=sum(bool(r["error"]) for r in results),
                cold=ok[0]["took"] if ok else float("nan"),
                median=warm[len(warm) // 2], worst=warm[-1])


# ----------------------------------------------------------------------

def ollama_up() -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(f"{config.OLLAMA_HOST}/api/version", timeout=5).read()
        return True
    except Exception:
        return False


def installed_models() -> list:
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return []
    return [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="*", help="ollama model tags to score")
    ap.add_argument("--runs", type=int, default=3,
                    help="repeats per case; >1 catches nondeterminism (default 3)")
    ap.add_argument("--pull", action="store_true",
                    help="download the named models, then exit without scoring")
    ap.add_argument("--list", action="store_true",
                    help="show cases and locally installed models, then exit")
    ap.add_argument("--json", metavar="PATH", help="write full per-run results here")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every run")
    args = ap.parse_args()

    if args.list:
        print(f"{len(CASES)} cases, none of which the deterministic tiers catch:\n")
        for c in CASES:
            want = "NO TOOL" if c.get("expect_no_tool") else "|".join(sorted(c["accept"]))
            print(f"  {c['id']:<16} {c['text']!r}\n  {'':<16} -> {want}\n")
        have = installed_models()
        print(f"suggested candidates ({sum(s for _, s, _ in SUGGESTED):.0f} GB total):\n")
        for tag, size, why in SUGGESTED:
            mark = "installed" if tag in have else "         "
            print(f"  {mark}  {tag:<20}{size:>6.1f} GB   {why}")
        print(f"\n  pull them all:\n    scripts/bakeoff.py --pull {' '.join(t for t, _, _ in SUGGESTED)}")
        print(f"\ninstalled models: {', '.join(have) or '(none)'}")
        print(f"ollama at {config.OLLAMA_HOST}: {'up' if ollama_up() else 'DOWN'}")
        return 0

    if not args.models:
        ap.error("name at least one model, or pass --list")

    if args.pull:
        for m in args.models:
            print(f"==> pulling {m}")
            if subprocess.run(["ollama", "pull", m]).returncode != 0:
                print(f"    FAILED: {m} — check the tag exists", file=sys.stderr)
        return 0

    if not ollama_up():
        print(f"Ollama is not answering at {config.OLLAMA_HOST}.\n"
              "  brew services start ollama", file=sys.stderr)
        return 1

    have = installed_models()
    missing = [m for m in args.models if m not in have]
    if missing:
        print(f"not installed: {', '.join(missing)}\n"
              f"  scripts/bakeoff.py --pull {' '.join(missing)}", file=sys.stderr)
        return 1

    all_results, summaries, unsupported = {}, [], []
    for model in args.models:
        print(f"\n=== {model} — {len(CASES)} cases x {args.runs} runs ===")
        results, no_tools = asyncio.run(run_model(model, args.runs, args.verbose))
        all_results[model] = results
        if no_tools:
            unsupported.append(model)
            continue
        s = summarise(model, results)
        summaries.append(s)
        print(f"  tools {s['tool']}/{s['n']}   fabrications {s['fab']}   "
              f"chat-fallback {s['chat_ok']}/{s['chat_n']}   "
              f"errors {s['errors']}   median {s['median']:.1f}s")

    print(f"\n{'model':<22}{'tools':>9}{'fabr':>7}{'chat':>7}"
          f"{'median':>9}{'worst':>8}{'cold':>8}")
    print("-" * 70)
    # Fabrications first: that is the criterion that decided this last time.
    for s in sorted(summaries, key=lambda s: (s["fab"], -s["tool"], s["median"])):
        print(f"{s['model']:<22}{s['tool']:>5}/{s['n']:<3}{s['fab']:>7}"
              f"{s['chat_ok']:>4}/{s['chat_n']:<2}{s['median']:>8.1f}s"
              f"{s['worst']:>7.1f}s{s['cold']:>7.1f}s")
    print("\n  tools/fabr are the main path. chat = the classifier-misroute")
    print("  fallback. cold = first request, which loads the model.")
    for m in unsupported:
        print(f"{m:<26}{'-':>9}{'NOT SCORED':>14}{'-':>9}{'-':>8}")

    if args.json:
        Path(args.json).write_text(json.dumps(all_results, indent=2))
        print(f"\nfull results -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
