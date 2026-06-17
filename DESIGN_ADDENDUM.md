# Smoketree Design Addendum

> Paste this after SMOKETREE_DESIGN.md. These decisions extend the core design
> and should be implemented after Milestone 1 is stable, likely as Milestone 2b
> or folded into Milestone 3.

---

## Collections and Fan-out

### Collection Nodes

A `collection` node resolves to multiple artifacts rather than one. It is the
entry point for glob-based multi-source workflows.

```yaml
nodes:
  characters:
    type: collection
    glob: sources/characters/*.jpg    # relative to project root

  poses:
    type: collection
    glob: sources/poses/*.jpg
```

- Glob is evaluated at execution time, not parse time.
- Each matched file is treated as an independent artifact with its own content hash.
- Collections are ordered (alphabetical by path) for determinism.
- An empty glob is a hard error.

### Expand Strategies

When a `transform` node receives one or more collection inputs, an `expand`
strategy must be declared. It determines how the collections are combined into
individual executions.

```yaml
expand: product    # cartesian product — every combination of all collection inputs
expand: zip        # positional pairing — A[0]↔B[0], A[1]↔B[1], etc.
expand: each       # single collection input — one execution per item
```

**Rules:**
- `zip` requires all collection inputs to have equal length — hard error otherwise.
- `each` requires exactly one collection input — hard error if multiple collections
  are provided.
- `product` works with any number of collection inputs.
- A node with no collection inputs does not declare `expand` (omit the field).
- A node consuming a collection output without declaring `expand` is a parse-time
  hard error.

### Expanded Node Identity

Each execution within an expanded node is identified by its **instance key** —
a tuple of the input artifact paths that produced it. This is used for:
- Cache key computation (hashed alongside transformer definition and take)
- Scratch and cache directory naming

Directory naming for expanded nodes uses a content-derived short hash of the
instance key to keep paths readable:

```
.smoketree/cache/{graph_id}/{node_id}/{instance_hash}/take_{n}/{output}.{ext}
.smoketree/scratch/{graph_id}/{node_id}/{instance_hash}/take_{n}/
```

The full instance key (input paths) is stored in a sidecar `.instance.json` file
in the cache dir for human inspection:

```json
{
  "inputs": {
    "character": "sources/characters/alice.jpg",
    "pose": "sources/poses/standing.jpg"
  }
}
```

### Downstream Composition

A node consuming a collection node's output is itself a collection node unless
it reduces (future). Expand strategies compose:

```yaml
nodes:
  characters:
    type: collection
    glob: sources/characters/*.jpg

  poses:
    type: collection
    glob: sources/poses/*.jpg

  controlnet:
    type: transform
    transformer: controlnet_prep
    inputs:
      image: characters
    expand: each                     # N executions, one per character

  generated:
    type: transform
    transformer: generate_with_controlnet
    inputs:
      controlnet_image: controlnet   # collection (N items)
      pose: poses                    # collection (M items)
    expand: product                  # N×M executions
```

**Cache invalidation behaviour for this graph:**
- Add a new pose file → only new `(character, pose)` combinations run
- Add a new character → `controlnet_prep` runs once for that character; then all
  pose combinations for it run
- Modify the `controlnet_prep` workflow JSON → all controlnet nodes invalidate;
  all downstream generations invalidate
- Modify the `generate_with_controlnet` workflow JSON → only generation nodes
  invalidate; controlnet nodes are untouched

---

## Split ComfyUI Pipelines

### Motivation

Some ComfyUI processing stages are expensive and reusable — ControlNet
preprocessing, VAE encoding, upscaling, etc. Splitting a monolithic ComfyUI
workflow into sequential transformer nodes allows Smoketree to cache each stage
independently.

### Intermediate Artifact Types

All intermediate artifacts are files on disk. No in-memory passing between
ComfyUI executions. This keeps the cache model identical to all other artifacts
and makes intermediates inspectable.

Extended media type list:

| Type | Notes |
|------|-------|
| `image` | jpg, png, webp, etc. |
| `audio` | wav, mp3, etc. |
| `video` | mp4, etc. |
| `text` | txt, json, etc. |
| `latent` | ComfyUI native .latent files (Save Latent / Load Latent nodes) |
| `data` | Generic binary blob — fallback for any serialised intermediate |

ComfyUI has native Save Latent and Load Latent nodes. Use these to serialise
latents between pipeline stages. Other intermediates (e.g. preprocessed
ControlNet images) are standard image files.

### Example: ControlNet Split

**Stage 1 — Preprocessing:**

```yaml
# transformers/controlnet_prep.yaml
name: controlnet_prep
type: comfyui
workflow: controlnet_prep.json
inputs:
  image:
    node_id: "3"
    field: image
    media: image
outputs:
  controlnet_image:
    node_id: "7"
    field: filename_prefix
    media: image
    format: png
```

**Stage 2 — Generation with saved ControlNet:**

```yaml
# transformers/generate_with_controlnet.yaml
name: generate_with_controlnet
type: comfyui
workflow: generate_with_controlnet.json
inputs:
  controlnet_image:
    node_id: "12"
    field: image
    media: image
  prompt:
    node_id: "6"
    field: text
    media: text
outputs:
  image:
    node_id: "19"
    field: filename_prefix
    media: image
    format: png
```

**Example: Latent caching split:**

```yaml
# transformers/encode_latent.yaml
name: encode_latent
type: comfyui
workflow: encode_latent.json
inputs:
  image:
    node_id: "4"
    field: image
    media: image
outputs:
  latent:
    node_id: "8"
    field: filename_prefix
    media: latent
    format: latent

# transformers/decode_and_refine.yaml
name: decode_and_refine
type: comfyui
workflow: decode_and_refine.json
inputs:
  latent:
    node_id: "5"
    field: latent        # Load Latent node input
    media: latent
  prompt:
    node_id: "3"
    field: text
    media: text
outputs:
  image:
    node_id: "21"
    field: filename_prefix
    media: image
    format: png
```

### Implementation Note

For `latent` media type outputs, the ComfyUI workflow must include a Save Latent
node. Smoketree collects the output by polling ComfyUI's output directory for
`.latent` files matching the filename prefix, exactly as it does for images.
No special handling is required beyond recognising the `latent` media type and
the `.latent` extension.

---

## Milestone Placement

These features should be implemented in order:

1. **Collection nodes + `each` expand** — simplest case; single collection fan-out.
   Implement after Milestone 2 shell execution is stable.

2. **`product` and `zip` expand** — multi-collection combinations. Requires
   instance key hashing and `.instance.json` sidecar.

3. **`latent` media type** — trivial addition once ComfyUI transformer is working
   (Milestone 3). Just a new enum value and extension handling.

Split ComfyUI pipelines require no new framework features beyond what Milestone 3
already implements — they are just two transformer nodes in sequence. The design
above is achievable with standard graph node composition.