"""Image generation, done on another machine.

Athena has no GPU worth generating on: the Mac's Metal stack is busy with
Whisper and Kokoro, and diffusion on top of that starves the audio callbacks
that make her voice work at all. So this is a client, not an engine. It talks
to a ComfyUI server over HTTP the same way speech.py talks to the TTS server,
and everything heavy happens somewhere else.

Nothing here may raise into the bot. A generation that fails, times out, or is
simply switched off returns a sentence a person can read. Losing an image is a
disappointment; losing the bot is an outage.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import random
import time
from pathlib import Path

import config

log = logging.getLogger("athena.imagegen")

# The image arrives on the Discord message, but the prompt arrives from the
# model several layers down, and the tool-call signature in between is the
# model's to write. A ContextVar carries it across without every function on
# the path growing a parameter it does not use — and unlike a module global it
# is per-task, so two people posting pictures at once cannot swap references.
_reference: contextvars.ContextVar = contextvars.ContextVar(
    "athena_reference_image", default=None)


def set_reference(data: bytes | None, filename: str = "input.png") -> None:
    """Hand the next generation a picture to work from. None clears it."""
    _reference.set((data, filename) if data else None)


def has_reference() -> bool:
    return _reference.get() is not None

# ComfyUI hands back a job id and does the work in the background, so the only
# way to know an image is ready is to ask repeatedly. Half a second is short
# enough that a fast job is not left waiting on the poll and long enough that a
# three-minute job costs a few hundred requests rather than tens of thousands.
POLL_SECONDS = 0.5

# Discord rejects attachments over its limit with an error that reads like the
# bot is broken. A 1024x1024 PNG is 1-2MB, so this is only ever hit by a
# workflow returning something enormous, but it is cheaper to say so plainly.
MAX_ATTACHMENT_BYTES = 9 * 1024 * 1024


class Picture:
    """A finished image on its way to the channel.

    A result object rather than bytes so bot.py can attach it, following the
    same shape as brain.Choice: the front end decides how to render it, and
    anything that sends this as plain text still says something sensible.
    """

    def __init__(self, data: bytes, filename: str, prompt: str, seconds: float):
        self.data = data
        self.filename = filename
        self.prompt = prompt
        self.seconds = seconds

    def text(self) -> str:
        """The caption posted beside the image. Worth reading, not hearing."""
        return f"“{self.prompt}” — {self.seconds:.0f}s"

    def spoken(self) -> str:
        """What to say out loud, which is not the caption.

        The prompt is written for a diffusion model, not for a person: it is a
        comma-separated pile of style words. Read aloud it took 8.2 seconds to
        tell the room something already on their screen.
        """
        return "Done."

    def __str__(self) -> str:
        return self.text()


def _patch(graph: dict, *, prompt: str, negative: str, seed: int,
           steps: int, cfg: float, width: int, height: int,
           checkpoint: str, image: str = "", denoise: float = 0.0) -> dict:
    """Fill a ComfyUI API graph in, by structure rather than by node id.

    Workflows are exported as {node_id: {class_type, inputs}} and the ids are
    whatever the editor happened to assign. Hardcoding "node 6 is the positive
    prompt" breaks the moment someone re-exports the workflow, and breaks
    silently — the graph still runs, it just ignores what the user asked for.

    So the positive and negative prompts are found by following KSampler's own
    'positive' and 'negative' links to whichever nodes they point at. That is
    the graph's own definition of which is which, so it survives a re-export.
    """
    graph = copy.deepcopy(graph)

    def nodes_of(*kinds):
        return [k for k, v in graph.items() if v.get("class_type") in kinds]

    samplers = nodes_of("KSampler", "KSamplerAdvanced")
    if not samplers:
        raise ValueError("workflow has no KSampler node")
    sampler = graph[samplers[0]]["inputs"]

    # Follow the links rather than guessing. inputs["positive"] is ["6", 0].
    for role, text in (("positive", prompt), ("negative", negative)):
        link = sampler.get(role)
        if isinstance(link, list) and link and link[0] in graph:
            target = graph[link[0]]["inputs"]
            if "text" in target:
                target["text"] = text

    if "seed" in sampler:
        sampler["seed"] = seed
    if "noise_seed" in sampler:          # KSamplerAdvanced spells it differently
        sampler["noise_seed"] = seed
    if "steps" in sampler:
        sampler["steps"] = steps
    if "cfg" in sampler:
        sampler["cfg"] = cfg

    for key in nodes_of("EmptyLatentImage", "EmptySD3LatentImage"):
        graph[key]["inputs"]["width"] = width
        graph[key]["inputs"]["height"] = height

    if image:
        # The uploaded name, as the server chose to store it. LoadImage is what
        # every img2img, ControlNet and inpainting graph reads its input from,
        # so patching it here covers all three without special cases.
        for key in nodes_of("LoadImage", "LoadImageMask"):
            graph[key]["inputs"]["image"] = image

    if denoise and "denoise" in sampler:
        # How far to travel from the source. 1.0 ignores it completely; too low
        # and the prompt has no room to change anything. Only set on the
        # img2img path — a text-to-image graph must keep its own 1.0.
        sampler["denoise"] = denoise

    if checkpoint:
        for key in nodes_of("CheckpointLoaderSimple", "UNETLoader"):
            inputs = graph[key]["inputs"]
            for field in ("ckpt_name", "unet_name"):
                if field in inputs:
                    inputs[field] = checkpoint

    return graph


def _load_workflow(name: str = "") -> dict:
    path = Path(name or config.IMAGE_WORKFLOW)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return json.loads(path.read_text(encoding="utf-8"))


async def _upload(client, base: str, data: bytes, filename: str) -> str:
    """Put a picture on the server and return the name it stored it under.

    ComfyUI renames on collision, so the name it answers with is the only one
    LoadImage will find — using the name we sent silently loads whatever was
    already there under that name, which looks like the wrong picture being
    edited rather than an upload problem.
    """
    posted = await client.post(
        f"{base}/upload/image",
        files={"image": (filename, data, "application/octet-stream")},
        data={"overwrite": "false"},
    )
    posted.raise_for_status()
    body = posted.json()
    stored = body.get("name") or ""
    folder = body.get("subfolder") or ""
    return f"{folder}/{stored}" if folder else stored


async def generate(prompt: str, *, negative: str = "", seed: int | None = None,
                   width: int = 0, height: int = 0) -> Picture | str:
    """Render one image. Returns a Picture, or a sentence explaining why not."""
    import httpx

    prompt = (prompt or "").strip()
    if not prompt:
        return "Tell me what to draw."
    if not config.IMAGE_ENABLED:
        return "Image generation is switched off."

    base = config.IMAGE_URL.rstrip("/")
    started = time.monotonic()
    reference = _reference.get()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # A picture came with the request, so this is an edit, not a fresh
            # image: send it over first, then run the graph that starts from it.
            stored, denoise = "", 0.0
            if reference:
                data, filename = reference
                stored = await _upload(client, base, data, filename)
                if not stored:
                    return "I couldn't send that picture to the image server."
                denoise = config.IMAGE_DENOISE
                log.info("working from %s (%.0f KB)", stored, len(data) / 1024)

            graph = _patch(
                _load_workflow(
                    config.IMAGE_WORKFLOW_IMG2IMG if stored else ""),
                prompt=prompt,
                negative=negative or config.IMAGE_NEGATIVE,
                seed=random.randint(0, 2**31 - 1) if seed is None else seed,
                steps=config.IMAGE_STEPS,
                cfg=config.IMAGE_CFG,
                width=width or config.IMAGE_WIDTH,
                height=height or config.IMAGE_HEIGHT,
                checkpoint=config.IMAGE_CHECKPOINT,
                image=stored,
                denoise=denoise,
            )
            queued = await client.post(f"{base}/prompt", json={"prompt": graph})
            queued.raise_for_status()
            accepted = queued.json()

            # A rejected workflow still comes back 200 with a prompt_id, and
            # node_errors is the only thing that says otherwise. Missing this
            # meant polling a job that was never going to run, right up to the
            # timeout, and then blaming the timeout.
            broken = accepted.get("node_errors") or {}
            if broken:
                log.warning("Workflow rejected by the server: %s", broken)
                return ("The image server refused that workflow — it usually "
                        "means the checkpoint name is wrong.")

            job = accepted.get("prompt_id")
            if not job:
                return "The image server took the job but gave me no id for it."

            # Poll rather than hold one long request open: a socket parked for
            # three minutes across a LAN is the thing most likely to be killed
            # by something in the middle, and losing it loses the whole job.
            deadline = started + config.IMAGE_TIMEOUT
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_SECONDS)
                done = await client.get(f"{base}/history/{job}")
                if done.status_code != 200:
                    continue
                entry = done.json().get(job)
                if not entry:
                    continue
                images = [
                    img
                    for out in entry.get("outputs", {}).values()
                    for img in out.get("images", [])
                ]
                if not images:
                    # status_str is "success" or "error"; completed says the run
                    # is over either way. Finished with nothing to show is a
                    # failure, and waiting out the timeout only delays saying so.
                    status = entry.get("status", {})
                    if (status.get("status_str") == "error"
                            or status.get("completed")):
                        log.warning("Run finished with no image: %s", status)
                        return "The image server failed on that one."
                    continue

                shot = images[-1]
                blob = await client.get(f"{base}/view", params={
                    "filename": shot.get("filename", ""),
                    "subfolder": shot.get("subfolder", ""),
                    "type": shot.get("type", "output"),
                })
                blob.raise_for_status()
                if len(blob.content) > MAX_ATTACHMENT_BYTES:
                    return "That came out too large to post."
                took = time.monotonic() - started
                log.info("generated %.1fs: %r", took, prompt[:70])
                return Picture(blob.content, shot.get("filename") or "image.png",
                               prompt, took)

            return (f"That took longer than {config.IMAGE_TIMEOUT:.0f}s, so I gave up. "
                    "It may still finish on the server.")

    except Exception as exc:
        # Same reasoning as speech.py: the machine being off or unreachable is
        # an ordinary condition here, not a bug worth a traceback in the log.
        log.warning("Image generation failed (%s)", exc.__class__.__name__)
        return "I couldn't reach the image server."


async def reachable() -> tuple[bool, str]:
    """Cheap health check for the control panel. Never raises."""
    import httpx

    if not config.IMAGE_ENABLED:
        return False, "switched off"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{config.IMAGE_URL.rstrip('/')}/system_stats")
            r.raise_for_status()
            devices = r.json().get("devices") or [{}]
            # Observed: "cuda:0 NVIDIA GeForce RTX 2060 SUPER : cudaMallocAsync".
            # Strip the device prefix and the allocator suffix, leaving the card.
            import re as _re
            raw = devices[0].get("name", "unknown GPU")
            name = _re.sub(r"^\w+:\d+\s*", "", raw).split(" : ")[0].strip() or raw
            free = devices[0].get("vram_free")
            if isinstance(free, int):
                return True, f"{name}, {free / 1024**3:.1f}GB free"
            return True, name
    except Exception as exc:
        return False, f"unreachable ({exc.__class__.__name__})"
