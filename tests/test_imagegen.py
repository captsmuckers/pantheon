"""Image generation: filling a workflow in, and surviving the server.

Generation happens on another machine, so the two things that can go wrong
here are patching the wrong node and letting a remote failure reach the bot.
Both are tested against a stub ComfyUI on localhost — no GPU, no network.
"""

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import imagegen  # noqa: E402

PASS = []
PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
        PASS.append(True)
    else:
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")
        PASS.append(False)


def _workflow():
    return json.loads(
        (Path(__file__).resolve().parent.parent / "workflows/sdxl.json")
        .read_text(encoding="utf-8")
    )


def test_the_shipped_workflow_is_usable():
    print("\nthe shipped workflow is a valid API graph")
    g = _workflow()
    kinds = {v["class_type"] for v in g.values()}
    check("has a sampler", "KSampler" in kinds, True)
    check("has a save node", "SaveImage" in kinds, True)
    check("every node declares inputs",
          all("inputs" in v for v in g.values()), True)


def test_patch_follows_links_not_node_ids():
    print("\nprompts are placed by following the graph, not by node id")
    out = imagegen._patch(
        _workflow(), prompt="a fox", negative="ugly", seed=7, steps=30,
        cfg=6.5, width=768, height=512, checkpoint="my.safetensors")

    sampler = next(v for v in out.values() if v["class_type"] == "KSampler")
    pos_id = sampler["inputs"]["positive"][0]
    neg_id = sampler["inputs"]["negative"][0]
    check("positive text landed on the positive node",
          out[pos_id]["inputs"]["text"], "a fox")
    check("negative text landed on the negative node",
          out[neg_id]["inputs"]["text"], "ugly")
    check("seed set", sampler["inputs"]["seed"], 7)
    check("steps set", sampler["inputs"]["steps"], 30)
    check("cfg set", sampler["inputs"]["cfg"], 6.5)

    latent = next(v for v in out.values()
                  if v["class_type"] == "EmptyLatentImage")
    check("width set", latent["inputs"]["width"], 768)
    check("height set", latent["inputs"]["height"], 512)
    ckpt = next(v for v in out.values()
                if v["class_type"] == "CheckpointLoaderSimple")
    check("checkpoint set", ckpt["inputs"]["ckpt_name"], "my.safetensors")

    print("\n  and it survives a re-export that renumbers every node")
    # This is the whole reason _patch walks links. Same graph, ids shuffled and
    # the two CLIPTextEncode nodes swapped in document order, so anything that
    # assumed "the first text node is the positive one" now gets it backwards.
    renumbered, remap = {}, {"4": "90", "5": "91", "6": "93", "7": "92",
                             "3": "94", "8": "95", "9": "96"}
    for old_id, node in _workflow().items():
        fresh = json.loads(json.dumps(node))
        for field, val in fresh["inputs"].items():
            if isinstance(val, list) and val and val[0] in remap:
                fresh["inputs"][field] = [remap[val[0]], val[1]]
        renumbered[remap[old_id]] = fresh

    out2 = imagegen._patch(
        renumbered, prompt="a fox", negative="ugly", seed=1, steps=20,
        cfg=7.0, width=1024, height=1024, checkpoint="")
    s2 = next(v for v in out2.values() if v["class_type"] == "KSampler")
    check("positive still correct after renumbering",
          out2[s2["inputs"]["positive"][0]]["inputs"]["text"], "a fox")
    check("negative still correct after renumbering",
          out2[s2["inputs"]["negative"][0]]["inputs"]["text"], "ugly")
    check("blank checkpoint leaves the workflow's own choice alone",
          next(v for v in out2.values()
               if v["class_type"] == "CheckpointLoaderSimple"
               )["inputs"]["ckpt_name"],
          "sd_xl_base_1.0.safetensors")

    print("\n  a workflow with no sampler is refused rather than sent")
    try:
        imagegen._patch({"1": {"class_type": "SaveImage", "inputs": {}}},
                        prompt="x", negative="", seed=1, steps=1, cfg=1.0,
                        width=64, height=64, checkpoint="")
        check("raises on a graph with no KSampler", False, True)
    except ValueError:
        check("raises on a graph with no KSampler", True, True)


class _StubComfy(BaseHTTPRequestHandler):
    """Enough ComfyUI to exercise submit -> poll -> fetch."""

    polls = 0
    fail = False

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._json({"prompt_id": "job1"})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/history/job1":
            _StubComfy.polls += 1
            if _StubComfy.fail:
                return self._json({"job1": {"outputs": {},
                                            "status": {"status_str": "error"}}})
            # First poll is deliberately empty: a real server accepts the job
            # before it has run it, and the client must keep waiting.
            if _StubComfy.polls < 2:
                return self._json({})
            return self._json({"job1": {"outputs": {"9": {"images": [
                {"filename": "athena_001.png", "subfolder": "", "type": "output"}
            ]}}}})
        if path == "/view":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            return self.wfile.write(PNG)
        self.send_response(404)
        self.end_headers()


def _with_stub(fn):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubComfy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    was = (config.IMAGE_ENABLED, config.IMAGE_URL, config.IMAGE_TIMEOUT)
    config.IMAGE_ENABLED = True
    config.IMAGE_URL = f"http://127.0.0.1:{srv.server_address[1]}"
    config.IMAGE_TIMEOUT = 15.0
    try:
        return fn()
    finally:
        (config.IMAGE_ENABLED, config.IMAGE_URL, config.IMAGE_TIMEOUT) = was
        srv.shutdown()


def test_a_whole_generation():
    print("\nsubmit, poll, and fetch the finished image")
    _StubComfy.polls, _StubComfy.fail = 0, False

    result = _with_stub(lambda: asyncio.run(imagegen.generate("a fox")))
    check("returns a Picture", isinstance(result, imagegen.Picture), True)
    if isinstance(result, imagegen.Picture):
        check("with the server's bytes", result.data, PNG)
        check("and its filename", result.filename, "athena_001.png")
        check("remembers the prompt", result.prompt, "a fox")
    check("kept polling past the empty first answer",
          _StubComfy.polls >= 2, True)


def test_nothing_reaches_the_bot_as_an_exception():
    print("\nevery failure comes back as a sentence, not a traceback")
    was = config.IMAGE_ENABLED
    config.IMAGE_ENABLED = False
    off = asyncio.run(imagegen.generate("a fox"))
    config.IMAGE_ENABLED = was
    check("switched off says so", isinstance(off, str) and "off" in off, True)

    blank = asyncio.run(imagegen.generate("   "))
    check("an empty prompt is refused", isinstance(blank, str), True)

    # Nothing is listening on this port, so the connection is refused outright.
    was = (config.IMAGE_ENABLED, config.IMAGE_URL)
    config.IMAGE_ENABLED, config.IMAGE_URL = True, "http://127.0.0.1:9"
    dead = asyncio.run(imagegen.generate("a fox"))
    config.IMAGE_ENABLED, config.IMAGE_URL = was
    check("an unreachable server is a sentence", isinstance(dead, str), True)

    _StubComfy.polls, _StubComfy.fail = 0, True
    broke = _with_stub(lambda: asyncio.run(imagegen.generate("a fox")))
    check("a server-side error is a sentence", isinstance(broke, str), True)

    ok, _ = asyncio.run(imagegen.reachable())
    check("the health check never raises either", isinstance(ok, bool), True)


for fn in (test_the_shipped_workflow_is_usable,
           test_patch_follows_links_not_node_ids,
           test_a_whole_generation,
           test_nothing_reaches_the_bot_as_an_exception):
    fn()

print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
sys.exit(0 if all(PASS) else 1)
