# 05 · Image Editing — reference images + a prompt

> Hand a model an existing photo *and* an instruction, and it edits the photo instead of
> starting from scratch. Local ComfyUI + **Flux 2 Klein** by default, swappable to OpenAI
> image edits.

## What you'll learn

- **Multi-input rules.** One rule takes two inputs of different kinds — an image
  (`reference`) and text (`instruction`) — and the backend def routes each to the right place.
- **Broadcasting a shared input.** The instruction is a single keyless file applied to
  *every* reference — one edit, a whole set transformed.
- **A real edit model.** Flux 2 Klein VAE-encodes the reference and injects it as a
  *reference latent* alongside the instruction, so it follows ambitious prompts while holding
  the original composition — far beyond what SD img2img can do.
- **The same rule, two editors.** Swap the `editor` def from ComfyUI to `openai_image` and
  the reference image becomes the edit base for an OpenAI Images edit.

## Prerequisites

Local-first: **[ComfyUI](https://github.com/comfyanonymous/ComfyUI)** on `:8188` with the
Flux 2 Klein assets the workflow loads:

- diffusion model `flux-2-klein-4b.safetensors` (`models/diffusion_models/`)
- CLIP `qwen_3_4b.safetensors` (`models/text_encoders/`, loaded as `type: flux2`)
- VAE `full_encoder_small_decoder.safetensors` (`models/vae/`)

Flux 2 is heavier than SD — expect a couple of minutes per render (the rule sets
`timeout: 1200`). The OpenAI swap needs `OPENAI_API_KEY`.

The two reference photos under `sources/` are ordinary landscape stills, included so the
example is self-contained.

## Project layout

```text
smoketree.yaml                       config + the graph (one editor def, one rule)
workflows/flux2-edit.json            the ComfyUI Flux 2 Klein edit workflow (API format)
sources/edit.txt                     ONE instruction, applied to every subject
sources/subject/valley/ref.png       reference image (a mountain valley)
sources/subject/harbor/ref.png       reference image (a fishing harbor)
# generated on run (gitignored):
work/subject/{subject}/edited.png    the edited image
```

## The pipeline

```yaml
rules:
  - name: edit
    model: editor_comfyui_flux2                     # flip to editor_openai to swap
    # model: editor_openai
    in:
      reference:   "sources/subject/{subject}/ref.png"
      instruction: "sources/edit.txt"               # one instruction, broadcast to every subject
    out:
      image: "work/subject/{subject}/edited.png"

models:
  editor_comfyui_flux2:
    backend: comfyui
    workflow: workflows/flux2-edit.json
    timeout: 1200                                  # Flux 2 Klein is heavier than SD
    seed_inject: { node: "138", field: noise_seed }
    inputs:
      reference:   { node: "76",  field: image }   # the LoadImage node
      instruction: { node: "141", field: text }    # the positive prompt
    outputs: { image: { node: "9" } }
  editor_openai:                                   # needs OPENAI_API_KEY
    backend: openai_image
    model: gpt-image-1
    size: "1024x1024"
    prompt: "{{ instruction }}"                     # reference image is sent as the edit base
```

- The `edit` rule has **two inputs**. The ComfyUI backend injects each by media type: the
  `reference` image is uploaded and its name written into `LoadImage` (node `76`); the
  `instruction` text is written into the positive `CLIPTextEncode` (node `141`).
- `reference` carries the `{subject}` key, so the rule fans out once per subject.
  `instruction` has **no key**, so the single `sources/edit.txt` is **broadcast** into every
  binding — one instruction transforming the whole set. Give each subject its own instruction
  instead by keying the path (`sources/subject/{subject}/edit.txt`).
- Inside the workflow, the reference is `VAEEncode`d and fed through `ReferenceLatent` into
  both the positive and negative conditioning — *that* is what makes Flux 2 edit the photo
  rather than generate from noise. `seed_inject` writes the per-cell seed into `RandomNoise`
  (node `138`); the result is collected from `SaveImage` (node `9`).

## Run it

```bash
cd examples/05-image-editing
uv run smoketree validate
uv run smoketree run            # Flux 2 Klein; ~a few minutes per image
```

```
[run ] edit(subject=valley)  (new)
[run ] edit(subject=harbor)  (new)
Done — 2 job(s) executed.
```

With the bundled instruction — *"a colossal kaiju rises from the water… keep the original
foreground, composition, and framing intact"* — each photo comes back with a towering,
lightning-lit kaiju dropped into the scene while the boats / pines / framing stay put. Edit
`sources/edit.txt` and both subjects re-run with the new instruction.

> **Note on caching.** Inputs are tracked, so editing `sources/edit.txt` re-runs **every**
> subject (they share it), while changing one `ref.png` re-runs just that subject. The
> *workflow JSON* is not a tracked input, so if you tweak the workflow, re-run with `--force`.

## Swapping backends — OpenAI image edits

The `editor_openai` def is already defined alongside the Flux 2 one — to edit in the cloud
instead, flip which `model:` line is commented in the `edit` rule:

```yaml
  - name: edit
    # model: editor_comfyui_flux2
    model: editor_openai          # needs OPENAI_API_KEY
```

`editor_openai` uses `openai_image`, which notices the rule has an image input and calls the
Images **edit** endpoint (text + image → image) instead of plain generation — a hosted
alternative to the local Flux 2 path, no GPU required.

## Next

**06 · Feedback Loops** *(planned)* — record human notes on a rendered cell and fold them
back into the next pass. See the [examples roadmap](../README.md).
