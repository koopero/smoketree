# Working on this Smoketree project

This is a **Smoketree** project. Smoketree is a declarative pipeline tool for media
transformation: a project is a DAG (directed acyclic graph) of *artifacts* connected by
*transformers*. Smoketree content-hashes inputs and rebuilds only what changed.

This file tells an AI coding tool how to make changes here correctly. Read it before
editing graphs or transformers. Everything is declarative YAML — you rarely write
orchestration code; you describe nodes and how they connect.

---

## Project layout

```
smoketree.yaml          # project config (name, defaults, env)
graphs/                 # graph definitions — one DAG per file (graph_id = filename)
transformers/           # reusable transformer definitions (+ ComfyUI workflow JSON)
sources/                # raw input files (user-managed)
scripts/                # helper scripts called by shell transformers
outputs/                # symlinks to final artifacts: {graph_id}__{node_id}.{ext}
.smoketree/             # generated; do not edit by hand
  cache/                #   content-addressed outputs
  scratch/              #   per-node working dirs (inspectable)
  state/                #   last-run input hashes
.env                    # secrets / env vars (gitignored)
```

Do **not** edit anything under `.smoketree/` — it is regenerated. Use `smoketree purge`
to clear it.

---

## Core concepts

- **Graph** (`graphs/<id>.yaml`): a named DAG. The `graph_id` is the filename stem.
- **Node**: a `source` (a raw file), a `collection` (a glob of files → many artifacts),
  or a `transform` (applies a transformer).
- **Input reference**: a transform node names its inputs by the *node id* that produces
  them — `inputs: { image: source }`. For a node with multiple outputs, use dot notation:
  `node_id.output_name`. Unqualified means the first declared output.
- **Transformer** (`transformers/<name>.yaml`): how to execute one step. Declares typed
  `inputs` and `outputs`. Reusable across graphs.
- **Media types**: `image | audio | video | text | data | latent`. Connections are
  validated at parse time — a `text` output cannot feed an `image` input (hard error).
  (`latent` is for ComfyUI `.latent` files passed between split workflow stages.)
- **Caching**: a node is skipped if its inputs, transformer definition, and take are
  unchanged. Editing a transformer YAML invalidates that node and everything downstream.
- **Seeds / takes**: every node execution gets a deterministic seed from
  `(graph_id, node_id, take)`. A *take* is an independent run at a different seed for
  exploring variation — not for branching the pipeline. Default take is `0`.

---

## How to make common changes

### Add a source file
1. Drop the file in `sources/`.
2. Reference it from a graph node:
   ```yaml
   my_source:
     type: source
     path: sources/myfile.jpg
   ```

### Add a transform node to a graph
```yaml
my_node:
  type: transform
  transformer: my_transformer        # references transformers/my_transformer.yaml
  inputs:
    image: my_source                 # input_name: producing_node_id
```
The keys under `inputs` **must exactly match** the transformer's declared input names —
no missing, no extras. The referenced media type must match too.

### Create a transformer
Create `transformers/<name>.yaml`. The `name:` field must equal the filename stem. Pick a
`type` (see reference below). Always re-run `smoketree validate` afterward.

---

## Transformer reference

### `shell` — run any command
Injected template vars (also available as `SMOKETREE_*` env vars): `{inputs.NAME}`,
`{outputs.NAME}`, `{dirs.scratch}`, `{dirs.output}`, `{seed}`, `{take}`, `{node_id}`,
`{graph_id}`. Write each declared output to its `{outputs.NAME}` path.

```yaml
name: describe
type: shell
command: python scripts/describe.py --image {inputs.image} --out {outputs.text}
inputs:
  image: { type: file, media: image }
outputs:
  text: { type: file, media: text, format: txt }
env:
  OPENAI_API_KEY: ${OPENAI_API_KEY}   # merged with project env; transformer wins
```

### `ollama` — local LLM inference (preferred for local-first work)
Calls a local Ollama server (`defaults.ollama_url`). No API key. The deterministic seed is
injected as `options.seed`, so runs are reproducible. Text/data inputs are inlined into the
prompt; image inputs are sent as base64 (use a vision model like `llava`). Declare exactly
one output. The model must be pulled locally (`ollama pull <model>`).

```yaml
name: description_to_prompt_local
type: ollama
model: llama3.2
options: { num_predict: 1024, temperature: 0.8 }
system: Output only the prompt, nothing else.
prompt: |
  Rewrite this description as an image-generation prompt:

  {inputs.text}
inputs:
  text: { type: file, media: text }
outputs:
  prompt: { type: file, media: text, format: txt }
```

### `claude` — Anthropic API
Same shape as `ollama` but uses the Anthropic API (`ANTHROPIC_API_KEY` required). Defaults
to `claude-sonnet-4-6`. Prefer `ollama` when the goal is to stay local.

```yaml
name: description_to_prompt
type: claude
model: claude-sonnet-4-6
max_tokens: 1024
system: Output only the prompt, nothing else.
prompt: |
  Rewrite this description as an image-generation prompt:

  {inputs.text}
inputs:
  text: { type: file, media: text }
outputs:
  prompt: { type: file, media: text, format: txt }
```

### `comfyui` — ComfyUI workflow
Two files: an API-format workflow JSON (export from ComfyUI) and this YAML sidecar.
`inject` declares where each input goes in the workflow; `collect` declares which workflow
node produces each output. ComfyUI base URL comes from `defaults.comfyui_url`.

```yaml
name: txt2img
type: comfyui
workflow: txt2img.json
inputs:
  prompt:
    type: file
    media: text
    inject: { node_id: "6", field: text }     # ComfyUI node id + input field
outputs:
  image:
    type: file
    media: image
    format: png
    collect: { node_id: "9", field: filename_prefix }
```

---

## Collections and fan-out

A **collection node** resolves to many artifacts via a glob (evaluated at run time,
ordered alphabetically; an empty glob is a hard error):

```yaml
characters:
  type: collection
  glob: sources/characters/*.jpg
```

A transform that consumes a collection input becomes a collection itself (it produces one
artifact per execution), and **must declare an `expand` strategy**:

| `expand` | Behaviour | Constraint |
|----------|-----------|------------|
| `each`    | one execution per item | exactly one collection input |
| `zip`     | positional pairing `A[i]↔B[i]` | all collection inputs equal length (runtime) |
| `product` | every combination | any number of collection inputs |

```yaml
controlnet:
  type: transform
  transformer: controlnet_prep
  inputs: { image: characters }     # collection
  expand: each                      # N executions

generated:
  type: transform
  transformer: generate
  inputs:
    controlnet_image: controlnet    # collection (N)
    pose: poses                     # collection (M)
  expand: product                   # N×M executions
```

Rules (all hard errors): a transform with a collection input but no `expand`; an `expand`
on a node with no collection inputs; `each` with more than one collection input.

Each execution of a fanned-out node is an **instance**, identified by a hash of its input
paths. Instances cache independently:
`.smoketree/cache/{graph}/{node}/{instance_hash}/take_{n}/`, with a `.instance.json`
sidecar recording which inputs produced it. Adding one source file rebuilds only the new
combinations; everything else stays cached.

---

## CLI workflow

```bash
smoketree validate [GRAPH]          # parse + validate; run this after every edit
smoketree plan GRAPH                # dry run: show execution order + cache status
smoketree run GRAPH                 # execute (cached nodes are skipped)
smoketree run GRAPH --node NODE     # run only NODE and its dependencies
smoketree run GRAPH --force         # ignore cache; rebuild
smoketree run GRAPH --take N        # run at take N (different seed)
smoketree status GRAPH              # last-run state per node
smoketree inspect GRAPH NODE        # show a node's scratch + cache dirs
smoketree purge GRAPH [--scratch] [--cache]
```

Final outputs land in `outputs/` as `{graph_id}__{node_id}.{ext}`.

---

## Rules & gotchas

- **Always run `smoketree validate` after editing** a graph or transformer. Fix errors
  before running.
- **Media types are enforced at parse time.** Mismatched connections are hard errors.
  Format hints (`format: png`) are runtime warnings only.
- **Node inputs must match the transformer's inputs exactly** — same names, no extras.
- **Editing a transformer YAML invalidates that node and everything downstream** (its text
  is part of the cache key). This is expected; re-run to rebuild.
- **Secrets go in `.env`**, referenced as `${VAR}` in YAML. A missing required var is a
  hard error at startup. Never hardcode keys.
- **Prefer local backends** (`ollama`, `shell`) over `claude` when the intent is to keep
  the project local-first.
- **Ollama empty responses fail** the run with an actionable error. If you hit one, try a
  different `--take`, raise `options.num_predict`, or switch models.
- **Inputs resolve from the producer's take 0** (falling back to the run's take). Build
  take 0 first; use other takes to explore variation on top of a stable base.
- **Failed runs leave `scratch/` intact** for inspection; reruns clear and recreate it.
