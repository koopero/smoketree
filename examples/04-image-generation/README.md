# 04 · Image Generation — prompt prebuilding + rendering

> Two stages: an LLM expands a short concept into a vivid image prompt, then an image
> model renders it. Local-first (Ollama + ComfyUI), swappable to OpenAI per stage.

## What you'll learn

- **Multi-stage pipelines.** One rule's output (a prompt) feeds the next (an image) — the
  DAG is inferred from the shared path, exactly like example 01, now spanning two models.
- **The ComfyUI backend.** A rule injects its inputs into a ComfyUI workflow JSON, submits
  it, and collects the rendered image — no glue code.
- **Per-stage backend swaps.** Each stage is a named model def, so you can run the prompt
  step on Ollama *or* OpenAI, and the render step on ComfyUI *or* OpenAI images.

## Prerequisites

Local-first, so the default run needs two local services:

- **[Ollama](https://ollama.com)** for the prompt step — `ollama serve` + `ollama pull gemma4`.
- **[ComfyUI](https://github.com/comfyanonymous/ComfyUI)** for the render step, running on
  `:8188` with an SD1.5 checkpoint installed. The bundled workflow expects
  `v1-5-pruned-emaonly-fp16.safetensors` — **edit `workflows/txt2img.json` (node `4`,
  `ckpt_name`) to match a checkpoint you have**, or drop one in ComfyUI's `models/checkpoints/`.

`validate` works with nothing running. To render in the cloud instead, see *Swapping
backends* below (needs `OPENAI_API_KEY`).

## Project layout

```text
smoketree.yaml                     config + the graph (two model defs, two rules)
workflows/txt2img.json             the ComfyUI workflow (API format)
sources/concept/harbor/idea.txt    a one-line concept
sources/concept/teahouse/idea.txt  a one-line concept
# generated on run (gitignored):
work/concept/{concept}/prompt.txt  the LLM-written image prompt
work/concept/{concept}/image.png   the rendered image
```

## The pipeline

```yaml
models:
  writer:                          # step 1: concept -> prompt (ollama; swappable to openai)
    backend: ollama
    model: gemma4:latest
  renderer:                        # step 2: prompt -> image (comfyui; swappable to openai_image)
    backend: comfyui
    workflow: workflows/txt2img.json
    seed_inject: { node: "3", field: seed }
    inputs:  { prompt: { node: "6", field: text } }
    outputs: { image: { node: "9" } }

rules:
  - name: prompt
    model: writer
    in:  { idea: "sources/concept/{concept}/idea.txt" }
    out: { prompt: "work/concept/{concept}/prompt.txt" }
    config:
      prompt: |
        Expand this concept into ONE vivid image-generation prompt ...
        Concept: {{ idea }}

  - name: render
    model: renderer
    in:  { prompt: "work/concept/{concept}/prompt.txt" }
    out: { image: "work/concept/{concept}/image.png" }
```

- `{concept}` fans both rules over every `sources/concept/<concept>/idea.txt`.
- `render`'s input (`work/concept/{concept}/prompt.txt`) is `prompt`'s output, so smoketree
  runs `prompt` first, then `render` — no edges declared.
- The `renderer` def tells the ComfyUI backend how to wire the workflow:
  `seed_inject` writes the per-cell seed into the KSampler (node `3`), `inputs` injects the
  prompt text into the positive `CLIPTextEncode` (node `6`), and `outputs` collects the
  image from `SaveImage` (node `9`). Those node ids match `workflows/txt2img.json`.

## Run it

```bash
cd examples/04-image-generation
uv run smoketree validate
uv run smoketree run            # ollama writes prompts, ComfyUI renders them
```

```
[run ] prompt(concept=harbor)  (new)
[run ] prompt(concept=teahouse)  (new)
[run ] render(concept=harbor)  (new)
[run ] render(concept=teahouse)  (new)
Done — 4 job(s) executed.
```

You get `work/concept/<concept>/prompt.txt` (a paragraph of visual detail) and
`work/concept/<concept>/image.png` (a 512×512 render). Re-running is a cached no-op; edit
one concept and only its prompt **and** image rebuild.

## Swapping backends

Each stage swaps independently by editing its def in `smoketree.yaml` — the rules never
change.

**Prompt step → OpenAI** (needs `OPENAI_API_KEY`):

```yaml
  writer:
    backend: openai
    model: gpt-5.1
```

**Render step → OpenAI images** (needs `OPENAI_API_KEY`): replace the ComfyUI `renderer`
block with the `openai_image` backend. It builds its prompt from the rule's input, so it
takes a `prompt` template instead of workflow node mappings:

```yaml
  renderer:
    backend: openai_image
    model: gpt-image-1
    size: "1024x1024"
    prompt: "{{ prompt }}"
```

`openai_image` is smoketree's OpenAI Images backend (added with this example): it generates
from the prompt, or — if the rule has image inputs — edits them as references (the basis for
example 05).

## Next

**05 · Image Editing** *(planned)* — feed reference images alongside the prompt to guide the
render. See the [examples roadmap](../README.md).
