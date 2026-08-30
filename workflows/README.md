# Workflows

ComfyUI graphs in **API format**, which is not the same as the format the
editor saves by default. In ComfyUI: Settings → enable *Dev mode options*, then
*Save (API Format)*. A graph saved the ordinary way will not run here.

`sdxl.json` is a plain SDXL text-to-image graph and the default. Point
`IMAGE_WORKFLOW` at another file to use your own.

Values are not substituted by node id. `imagegen._patch` finds the KSampler and
follows its own `positive` and `negative` links to whichever nodes they point
at, so re-exporting a workflow and getting different ids does not silently send
your prompt to the wrong input. What it patches:

| setting            | node                                      |
|--------------------|-------------------------------------------|
| the prompt         | whatever KSampler's `positive` points at   |
| `IMAGE_NEGATIVE`   | whatever KSampler's `negative` points at   |
| `IMAGE_STEPS`      | KSampler `steps`                           |
| `IMAGE_CFG`        | KSampler `cfg`                             |
| `IMAGE_WIDTH/HEIGHT` | EmptyLatentImage                         |
| `IMAGE_CHECKPOINT` | CheckpointLoaderSimple / UNETLoader        |

A custom workflow needs a KSampler (or KSamplerAdvanced) and a SaveImage. Any
node type not in that table is left exactly as exported, so LoRAs, ControlNets
and upscalers all work — they just are not adjustable from the panel.
