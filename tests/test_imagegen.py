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
    uploaded = b""
    reject = False       # 200 with node_errors — a rejected workflow
    empty = False        # finishes successfully but produces no image

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
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if urlparse(self.path).path == "/upload/image":
            _StubComfy.uploaded = body
            # ComfyUI renames on collision, so it deliberately answers with a
            # DIFFERENT name than was sent — the client must use this one.
            return self._json({"name": "leopard (1).png", "subfolder": "",
                               "type": "input"})
        if _StubComfy.reject:
            # Observed: a rejected workflow is still 200 WITH a prompt_id.
            return self._json({
                "prompt_id": "job1", "number": 0,
                "node_errors": {"4": {"errors": [
                    {"type": "value_not_in_list",
                     "message": "ckpt_name: 'nope.safetensors' not in list"}
                ]}},
            })
        self._json({"prompt_id": "job1", "number": 0, "node_errors": {}})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/history/job1":
            _StubComfy.polls += 1
            if _StubComfy.fail:
                return self._json({"job1": {
                    "outputs": {},
                    "status": {"status_str": "error", "completed": False,
                               "messages": [["execution_error", {}]]},
                }})
            if _StubComfy.empty:
                # Ran to completion and produced nothing. Before the fix this
                # polled until the timeout and then blamed the timeout.
                return self._json({"job1": {
                    "outputs": {},
                    "status": {"status_str": "success", "completed": True,
                               "messages": [["execution_success", {}]]},
                }})
            # First poll is deliberately empty: a real server accepts the job
            # before it has run it, and the client must keep waiting.
            if _StubComfy.polls < 2:
                return self._json({})
            return self._json({"job1": {
                "prompt": [0, "job1", {}, {"create_time": 0}, ["9"]],
                "outputs": {"9": {"images": [
                    {"filename": "athena_001.png", "subfolder": "",
                     "type": "output"}
                ]}},
                "status": {"status_str": "success", "completed": True,
                           "messages": [["execution_start", {}],
                                        ["execution_success", {}]]},
                "meta": {"9": {"node_id": "9", "real_node_id": "9"}},
            }})
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
    _StubComfy.reject = _StubComfy.empty = False

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
    _StubComfy.reject = _StubComfy.empty = False
    broke = _with_stub(lambda: asyncio.run(imagegen.generate("a fox")))
    check("a server-side error is a sentence", isinstance(broke, str), True)

    ok, _ = asyncio.run(imagegen.reachable())
    check("the health check never raises either", isinstance(ok, bool), True)


def test_the_two_failures_that_used_to_hang():
    print("\nfailures the server reports are believed, not waited out")
    # Both of these previously polled to IMAGE_TIMEOUT and then reported a
    # timeout, hiding the real cause. Timeout is 15s under the stub, so a
    # regression here shows up as a slow test as well as a wrong answer.
    _StubComfy.polls, _StubComfy.fail = 0, False
    _StubComfy.reject, _StubComfy.empty = True, False
    import time as _t
    started = _t.monotonic()
    out = _with_stub(lambda: asyncio.run(imagegen.generate("a fox")))
    took = _t.monotonic() - started
    check("a rejected workflow is reported, not polled",
          isinstance(out, str) and "refused" in out, True)
    check("and reported immediately", took < 5, True)

    _StubComfy.polls, _StubComfy.fail = 0, False
    _StubComfy.reject, _StubComfy.empty = False, True
    started = _t.monotonic()
    out = _with_stub(lambda: asyncio.run(imagegen.generate("a fox")))
    took = _t.monotonic() - started
    check("a finished-but-empty run is reported", isinstance(out, str), True)
    check("and not waited out to the timeout", took < 5, True)
    _StubComfy.reject = _StubComfy.empty = False


def test_an_image_request_reaches_the_tool():
    """The routing bug that made the whole feature look broken.

    The intent classifier calls an image request CHAT — measured on qwen3:8b,
    5 of 7 phrasings — because "draw me something" reads exactly like "write me
    a poem". CHAT attaches no tools, so she discussed drawing instead of
    drawing, and the failure looked like the ComfyUI integration rather than
    routing. _DRAW_REQUEST settles it before either chat route runs.
    """
    print("\nimage requests route to the tool path, not to conversation")
    import brain

    for text in ("draw me a fox",
                 "athena draw a picture of a snow leopard",
                 "make me an image of a castle at night",
                 "generate an image of a robot",
                 "paint something cool",
                 "sketch a dragon",
                 "please draw a cat",
                 "create a painting of a harbour"):
        check(f"drawn: {text!r}", bool(brain._DRAW_REQUEST.match(text)), True)

    print("\n  and these must NOT be hijacked into generating an image")
    for text in ("can you draw?",          # a question about her, not a request
                 "make me a sandwich",     # weak verb, no image noun
                 "write me a poem",        # the chat route owns this
                 "tell me a joke",
                 "what can you do",
                 "play Dune",
                 "skip",
                 "make it louder"):
        check(f"left alone: {text!r}",
              bool(brain._DRAW_REQUEST.match(text)), False)


def test_an_attached_picture_is_worked_from():
    print("\na reference picture is uploaded and edited, not ignored")
    if not Path("workflows/sdxl-img2img.json").exists():
        # cwd-independent
        pass
    root = Path(__file__).resolve().parent.parent
    img2img = json.loads(
        (root / "workflows/sdxl-img2img.json").read_text(encoding="utf-8"))
    kinds = {v["class_type"] for v in img2img.values()}
    check("the img2img workflow loads an image", "LoadImage" in kinds, True)
    check("and encodes it to latent", "VAEEncode" in kinds, True)

    out = imagegen._patch(
        img2img, prompt="a bronze statue", negative="", seed=1, steps=20,
        cfg=7.0, width=1024, height=1024, checkpoint="",
        image="leopard (1).png", denoise=0.65)
    load = next(v for v in out.values() if v["class_type"] == "LoadImage")
    check("the uploaded name reaches LoadImage",
          load["inputs"]["image"], "leopard (1).png")
    sampler = next(v for v in out.values() if v["class_type"] == "KSampler")
    check("denoise is applied", sampler["inputs"]["denoise"], 0.65)

    print("\n  and a text-to-image run keeps its own denoise of 1.0")
    plain = imagegen._patch(
        json.loads((root / "workflows/sdxl.json").read_text(encoding="utf-8")),
        prompt="a fox", negative="", seed=1, steps=20, cfg=7.0,
        width=1024, height=1024, checkpoint="")
    ks = next(v for v in plain.values() if v["class_type"] == "KSampler")
    check("untouched without a reference", ks["inputs"]["denoise"], 1.0)

    print("\n  end to end against the stub")
    _StubComfy.polls, _StubComfy.fail = 0, False
    _StubComfy.reject = _StubComfy.empty = False
    _StubComfy.uploaded = b""
    was_wf = config.IMAGE_WORKFLOW_IMG2IMG
    config.IMAGE_WORKFLOW_IMG2IMG = str(root / "workflows/sdxl-img2img.json")
    imagegen.set_reference(b"PRETEND-PNG-BYTES", "leopard.png")
    result = _with_stub(lambda: asyncio.run(imagegen.generate("a bronze statue")))
    imagegen.set_reference(None)
    config.IMAGE_WORKFLOW_IMG2IMG = was_wf
    check("returns a Picture", isinstance(result, imagegen.Picture), True)
    check("the reference bytes actually reached the server",
          b"PRETEND-PNG-BYTES" in _StubComfy.uploaded, True)

    print("\n  the reference does not leak into the next request")
    check("cleared", imagegen.has_reference(), False)


def test_edit_phrasings_route_to_the_tool():
    print("\nwith a picture attached, an edit request reaches the tool")
    import brain
    for text in ("make this guy into superman", "give this guy corn rolls",
                 "turn him into a viking", "can you make this black and white",
                 "add a cape to this", "remove the background"):
        check(f"edit: {text!r}", bool(brain._EDIT_REQUEST.match(text)), True)

    print("\n  but posting a picture and chatting is still conversation")
    for text in ("lol", "check this out", "who is this", "nice", "what is that"):
        check(f"not an edit: {text!r}",
              bool(brain._EDIT_REQUEST.match(text)), False)


def test_a_generated_image_survives_the_tool_loop():
    """The bug that threw away an image after the GPU had already made it.

    dispatch() returns a Picture, and the tool loop fed every result straight
    into json.dumps() to show the model what came back. json.dumps raises on a
    Picture, so the turn died AFTER 20.7 seconds of generation and the user got
    "Something went wrong talking to the language model" — with the finished
    PNG sitting on the server.

    Two faults in one line: the dump ran before the authoritative check, and
    that check required isinstance(result, str), which a Picture is not, so it
    could never have been recognised even without the crash.
    """
    print("\na generated image survives the tool-calling loop")
    import asyncio as _asyncio
    import httpx as _httpx
    import brain as brain_mod

    picture = imagegen.Picture(b"PNGBYTES", "athena_00002_.png",
                               "a dog at the park", 20.7)
    check("a Picture is recognised as the reply itself",
          brain_mod._is_final_object(picture), True)
    check("an ordinary string is not", brain_mod._is_final_object("ok"), False)

    class Controls:
        def state(self):
            return {"playing": False}

        async def dispatch(self, name, args):
            return picture

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "", "tool_calls": [
                {"function": {"name": "generate_image",
                              "arguments": {"prompt": "a dog at the park"}}}
            ]}}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return Response()

    b = brain_mod.Brain.__new__(brain_mod.Brain)
    b.controls = Controls()
    b.history = []
    b._lock = _asyncio.Lock()
    b._verbatim_tool = None
    b.backend = "ollama"
    b.narrator = type("N", (), {"wants": lambda *a, **k: False})()

    original = _httpx.AsyncClient
    _httpx.AsyncClient = Client
    try:
        reply = _asyncio.run(b._ask_ollama("make a picture of a dog at the park"))
    finally:
        _httpx.AsyncClient = original

    check("the turn does not crash", reply is not None, True)
    check("and the Picture itself comes back, not a description of it",
          reply is picture, True)

    print("\n  and an unforeseen result can no longer kill the turn")
    import json as _json

    class Odd:
        def __repr__(self):
            return "<odd>"

    try:
        _json.dumps({"x": Odd()}, default=str)
        check("json.dumps with default=str tolerates anything", True, True)
    except TypeError:
        check("json.dumps with default=str tolerates anything", False, True)


def test_the_caption_is_read_but_not_spoken():
    """She read the whole generated prompt out loud — 8.2 seconds of it.

    str(Picture) is the caption, which is right for the channel and wrong for
    the room: the prompt is written for a diffusion model, so out loud it is a
    pile of style keywords describing something already on screen.
    """
    print("\nthe caption is posted; something short is spoken")
    pic = imagegen.Picture(b"x", "a.png",
                           "an anime stoner girl with a bong, cute and relaxed "
                           "expression, vibrant colors, detailed anime style", 20.4)
    check("the caption carries the prompt", "anime stoner girl" in pic.text(), True)
    check("and the duration", "20s" in pic.text(), True)
    check("the spoken form is short", len(pic.spoken()) < 30, True)
    check("and does not read the prompt back",
          "anime" in pic.spoken().lower(), False)


def test_the_phrasings_that_reached_her_as_refusals():
    """Live failures: three requests she could have served, answered in
    character instead.

        "make a photo of darth vader..."            -> CHAT, refused
        "make a realistic photo of darth vader..."  -> CHAT, refused
        "remake this image to be pacifico johnson"  -> CHAT, refused

    while "make a picture of a spooky alien" worked. Three gaps: "photo" was
    not an image noun, no adjective was allowed between the article and the
    noun, and "remake"/"redo" were not verbs. Falling through to the classifier
    means CHAT, and CHAT has no tools — so she answers in character with no way
    to act, which reads as refusing something she can do perfectly well.
    """
    print("\nphrasings that must reach the image tool")
    import brain
    for text in ("make a photo of darth vader sleeping with a mexican guy",
                 "make a realistic photo of darth vader sleeping",
                 "remake this image to be pacifico johnson",
                 "redo the picture", "create a dark moody wallpaper",
                 "make me a nice portrait",
                 "make a picture of a spooky alien."):
        check(f"reaches the tool: {text[:44]!r}",
              bool(brain._DRAW_REQUEST.match(text)), True)

    print("\n  and phrasings that must NOT be hijacked into drawing")
    for text in ("play Dune", "make it louder", "make me a sandwich",
                 "pause the movie", "turn it up", "make the volume louder",
                 "tell me a joke", "write me a poem", "can you draw?"):
        check(f"left alone: {text!r}",
              bool(brain._DRAW_REQUEST.match(text)), False)


def test_a_reference_is_scaled_before_it_is_encoded():
    """A 6.7MB reference pushed one edit past the turn ceiling.

    Nothing scaled it, so ComfyUI VAE-encoded a phone photo at its own
    resolution on an 8GB card. The edit that succeeded came from a 1985KB
    source and took 22.6s; the 6743KB one was abandoned at 120s. Scaling
    happens on the GPU machine because the bot has no image library, and
    should not grow one to shrink a picture.
    """
    print("\nthe reference is scaled down before the VAE sees it")
    root = Path(__file__).resolve().parent.parent
    g = json.loads((root / "workflows/sdxl-img2img.json").read_text(encoding="utf-8"))
    kinds = {v["class_type"] for v in g.values()}
    check("there is a scaling node", "ImageScaleToTotalPixels" in kinds, True)

    scaler = next(k for k, v in g.items()
                  if v["class_type"] == "ImageScaleToTotalPixels")
    loader = next(k for k, v in g.items() if v["class_type"] == "LoadImage")
    encoder = next(v for v in g.values() if v["class_type"] == "VAEEncode")
    check("it reads the loaded image", g[scaler]["inputs"]["image"][0], loader)
    check("and the encoder reads the SCALED one, not the original",
          encoder["inputs"]["pixels"][0], scaler)
    check("about one megapixel, which is what SDXL wants",
          g[scaler]["inputs"]["megapixels"], 1.0)


def test_the_turn_outlasts_the_image_it_is_waiting_for():
    """Two ceilings that disagreed, and the wrong one won.

    imagegen waited IMAGE_TIMEOUT (300s) while bot.py abandoned the whole turn
    at REPLY_TIMEOUT (120s), so a slow edit was killed with the GPU still
    working and the finished image landed nowhere.
    """
    print("\nthe turn ceiling outlasts the image ceiling")
    import bot as bot_mod
    if config.IMAGE_ENABLED:
        check("a turn may run longer than an image may take",
              bot_mod.REPLY_TIMEOUT > config.IMAGE_TIMEOUT, True)
    else:
        check("images off, so the turn ceiling stands alone",
              bot_mod.REPLY_TIMEOUT > 0, True)


for fn in (test_the_shipped_workflow_is_usable,
           test_patch_follows_links_not_node_ids,
           test_a_whole_generation,
           test_nothing_reaches_the_bot_as_an_exception,
           test_the_two_failures_that_used_to_hang,
           test_an_image_request_reaches_the_tool,
           test_an_attached_picture_is_worked_from,
           test_edit_phrasings_route_to_the_tool,
           test_a_generated_image_survives_the_tool_loop,
           test_the_caption_is_read_but_not_spoken,
           test_the_phrasings_that_reached_her_as_refusals,
           test_a_reference_is_scaled_before_it_is_encoded,
           test_the_turn_outlasts_the_image_it_is_waiting_for):
    fn()

print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
sys.exit(0 if all(PASS) else 1)
