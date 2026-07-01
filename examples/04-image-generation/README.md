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
rules:
  - name: prompt
    model: writer_ollama           # step 1: concept -> prompt; flip to writer_openai to swap
    # model: writer_openai
    in:  { idea: "sources/concept/{concept}/idea.txt" }
    out: { prompt: "work/concept/{concept}/prompt.txt" }
    config:
      prompt: |
        Expand this concept into ONE vivid image-generation prompt ...
        Concept: {{ idea }}

  - name: render
    model: renderer_comfyui        # step 2: prompt -> image; flip to renderer_openai to swap
    # model: renderer_openai
    in:  { prompt: "work/concept/{concept}/prompt.txt" }
    out: { image: "work/concept/{concept}/image.png" }

models:
  writer_ollama:   { backend: ollama, model: gemma4:latest }
  writer_openai:   { backend: openai, model: gpt-5.1 }        # needs OPENAI_API_KEY
  renderer_comfyui:
    backend: comfyui
    workflow: workflows/txt2img.json
    seed_inject: { node: "3", field: seed }
    inputs:  { prompt: { node: "6", field: text } }
    outputs: { image: { node: "9" } }
  renderer_openai:                                            # needs OPENAI_API_KEY
    backend: openai_image
    model: gpt-image-1
    size: "1024x1024"
    prompt: "{{ prompt }}"
```

- `{concept}` fans both rules over every `sources/concept/<concept>/idea.txt`.
- `render`'s input (`work/concept/{concept}/prompt.txt`) is `prompt`'s output, so smoketree
  runs `prompt` first, then `render` — no edges declared.
- Each step has one named def per backend; the rule picks one with `model:`. The
  `renderer_comfyui` def tells the ComfyUI backend how to wire the workflow:
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

Each step's backends are already defined as named defs (`writer_ollama`/`writer_openai`,
`renderer_comfyui`/`renderer_openai`). Swap a step by flipping which `model:` line is
commented in its rule — the two steps swap independently, and the rules are otherwise
unchanged. To run entirely on OpenAI (needs `OPENAI_API_KEY`):

```yaml
  - name: prompt
    # model: writer_ollama
    model: writer_openai
    ...
  - name: render
    # model: renderer_comfyui
    model: renderer_openai
```

`renderer_openai` uses `openai_image`, smoketree's OpenAI Images backend (added with this
example): it generates from the prompt, or — if the rule has image inputs — edits them as
references (the basis for example 05). Note it takes a `prompt: "{{ prompt }}"` template
rather than workflow node mappings, which is why it's a separate def.

## Next

**[05 · Image Editing](../05-image-editing/)** — feed a reference image alongside the prompt
to guide the render (local ComfyUI img2img, swappable to OpenAI edits).
