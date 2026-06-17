# Smoketree Design Document

> **Handoff document for implementation in Claude Code.**
> This document is the source of truth for all design decisions. Implementation should follow
> the schemas, semantics, and milestone plan defined here without deviation unless a concrete
> technical constraint requires it — in which case, note the deviation.

---

## Overview

Smoketree is a declarative pipeline tool for media transformation. It models a project as a
DAG (directed acyclic graph) of artifacts connected by transformations. The framework caches
generated artifacts by content-hashing inputs, and rebuilds only what has changed.

**Primary use case:** Creative/AI image pipelines — e.g. source image → description → prompt
→ generated image → upscaled image — with support for audio, video, text, and data as
intermediary layers.

**Design philosophy:** Experimental first. Favour clarity and browsability over performance.
The cache and scratch layout should be inspectable by a human with a file browser.

---

## Name

**Smoketree** — smoke diffuses like pixels; tree provides structure. CLI command: `smoketree`.
Python package: `smoketree`. No aliases.

---

## Technology

- **Language:** Python 3.12+
- **Package manager:** uv
- **CLI framework:** Typer
- **YAML parsing:** PyYAML
- **Schema validation:** Pydantic v2
- **Async execution:** asyncio (for parallel independent nodes)
- **Hashing:** hashlib (SHA-256)

---

## Project Layout

```
my-project/
  smoketree.yaml              # project config (name, defaults, env)
  .smoketree/
    cache/                    # content-addressed output artifacts
      {graph_id}/
        {node_id}/
          take_{n}/
            {output_name}.{ext}
    scratch/                  # working dirs, persistent for inspection
      {graph_id}/
        {node_id}/
          take_{n}/
            ...               # whatever the transformer wrote
    state/
      {graph_id}.json         # last-run input hashes per node
  sources/                    # raw input artifacts (user-managed)
  graphs/                     # graph definition YAML files
    portrait.yaml
    batch.yaml
  transformers/               # reusable transformer definitions
    describe.yaml
    upscale.yaml
    upscale.json              # ComfyUI workflow (referenced by sidecar)
    txt2img.yaml
    txt2img.json
  outputs/                    # symlinks or copies of final node outputs
                              # named {graph_id}__{node_id}.{ext}
```

---

## Project Config

```yaml
# smoketree.yaml
name: my-project

defaults:
  comfyui_url: http://localhost:8188
  ollama_url: http://localhost:11434
  take: 0

env:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
  OPENAI_API_KEY: ${OPENAI_API_KEY}
```

- Environment variables are loaded from the shell environment and `.env` file at project root.
- `${VAR}` syntax in any YAML value triggers substitution at load time.
- Missing required env vars cause a hard error at startup.

---

## Graph Definition

Graphs live in `graphs/`. Each file defines a DAG of nodes.

```yaml
# graphs/portrait.yaml
name: portrait

nodes:
  source:
    type: source
    path: sources/subject.jpg          # relative to project root

  description:
    type: transform
    transformer: describe              # references transformers/describe.yaml
    inputs:
      image: source                    # {node_id} reference

  prompt:
    type: transform
    transformer: description_to_prompt
    inputs:
      text: description

  generated:
    type: transform
    transformer: txt2img
    inputs:
      prompt: prompt

  upscaled:
    type: transform
    transformer: upscale
    inputs:
      image: generated
```

### Node Types

| Type | Description |
|------|-------------|
| `source` | A raw input file. No transformer. Invalidated when file content changes. |
| `transform` | Applies a transformer to one or more inputs. |

### Input References

Inputs are references to other nodes by `node_id`. Smoketree resolves the output of that node
and injects it into the transformer. If a node has multiple outputs, use dot notation:
`node_id.output_name`. If unqualified, the first declared output is used.

---

## Transformer Definitions

Transformers live in `transformers/`. Each is a YAML file defining how to execute a
transformation. The `type` field determines the execution backend.

### Transformer Types

| Type | Description |
|------|-------------|
| `shell` | Arbitrary shell command |
| `claude` | Built-in Anthropic Claude API call |
| `ollama` | Local LLM inference via the Ollama HTTP API |
| `comfyui` | ComfyUI workflow via HTTP API |
| `http` | Generic HTTP request (future) |

---

### Shell Transformer

```yaml
# transformers/describe.yaml
name: describe
type: shell

command: >
  python scripts/describe.py
  --image {inputs.image}
  --out {outputs.text}
  --scratch {dirs.scratch}

inputs:
  image:
    type: file
    media: image                       # image | audio | video | text | data

outputs:
  text:
    type: file
    media: text
    format: txt                        # optional hint; validated at runtime

env:
  OPENAI_API_KEY: ${OPENAI_API_KEY}   # merged with project env; transformer wins
```

#### Injected Variables (shell)

All of the following are available for `{interpolation}` in `command`, and as environment
variables (uppercased, prefixed with `SMOKETREE_`):

| Template var | Env var | Description |
|---|---|---|
| `{inputs.NAME}` | — | Resolved path to input artifact |
| `{outputs.NAME}` | — | Target path for output artifact |
| `{dirs.scratch}` | `SMOKETREE_SCRATCH` | Per-execution scratch dir (persistent) |
| `{dirs.output}` | `SMOKETREE_OUTPUT` | Per-execution output dir |
| `{seed}` | `SMOKETREE_SEED` | Deterministic integer seed |
| `{take}` | `SMOKETREE_TAKE` | Take index (default 0) |
| `{node_id}` | `SMOKETREE_NODE_ID` | Current node ID |
| `{graph_id}` | `SMOKETREE_GRAPH_ID` | Current graph ID |

Scripts may use or ignore seed/take — they are always injected.

---

### Claude Transformer

```yaml
# transformers/description_to_prompt.yaml
name: description_to_prompt
type: claude

model: claude-sonnet-4-6               # optional; defaults to claude-sonnet-4-6
max_tokens: 1024

system: |
  You are an expert at writing image generation prompts.
  Be concise. Output only the prompt, nothing else.

prompt: |
  Convert this image description into a detailed image generation prompt:

  {inputs.text}

inputs:
  text:
    type: file
    media: text

outputs:
  prompt:
    type: file
    media: text
    format: txt
```

- The `prompt` field supports `{inputs.NAME}` interpolation. File inputs are read and their
  contents are substituted inline.
- Output is the raw text response from Claude, written to `outputs.prompt`.

---

### Ollama Transformer

A local-first counterpart to the Claude transformer. Calls a model served by a local
[Ollama](https://ollama.com) instance over its HTTP API — no API key, no network egress.

```yaml
# transformers/description_to_prompt_local.yaml
name: description_to_prompt_local
type: ollama

model: llama3.2                        # must be pulled locally (`ollama pull`)

options:                               # optional; passed through to Ollama
  num_predict: 1024                    # token cap
  temperature: 0.8

system: |
  You are an expert at writing image generation prompts.
  Be concise. Output only the prompt, nothing else.

prompt: |
  Convert this image description into a detailed image generation prompt:

  {inputs.text}

inputs:
  text:
    type: file
    media: text

outputs:
  prompt:
    type: file
    media: text
    format: txt
```

- The `prompt` field supports `{inputs.NAME}` interpolation. Text and data inputs are read
  and inlined; image inputs are attached as base64 in the request's `images` array (for
  vision models such as `llava`).
- The base URL is read from project config (`defaults.ollama_url`, default
  `http://localhost:11434`).
- The deterministic Smoketree seed is injected as `options.seed` unless the transformer
  sets one explicitly — so local runs are reproducible.
- Must declare exactly one output; the raw response text is written to it.

---

### ComfyUI Transformer

ComfyUI transformers consist of two files: a workflow JSON (exported from ComfyUI) and a YAML
sidecar that declares injection points and output locations.

```yaml
# transformers/upscale.yaml
name: upscale
type: comfyui

workflow: upscale.json                 # relative to transformers/

inputs:
  image:
    type: file
    media: image
    inject:
      node_id: "12"                    # ComfyUI node ID (string)
      field: image                     # field name on that node's inputs

outputs:
  image:
    type: file
    media: image
    format: png
    collect:
      node_id: "27"                    # ComfyUI node ID that produces output
      field: filename_prefix           # field that determines output filename
```

- Smoketree replaces the declared input fields in the workflow JSON before submitting.
- Output collection polls the ComfyUI output directory for files matching the expected prefix.
- The ComfyUI base URL is read from project config (`defaults.comfyui_url`).

---

## Validation

### Media Type Validation (parse time)

When the graph is loaded, Smoketree validates that connected nodes have compatible media types.
A `text` output cannot feed an `image` input. Mismatches are **hard errors** — execution does
not begin.

Media types: `image`, `audio`, `video`, `text`, `data`

### Format Validation (runtime)

`format` is an optional hint on outputs (e.g. `png`, `wav`, `mp4`, `txt`). If declared,
Smoketree checks the file extension of the produced artifact after execution. Mismatches are
**warnings**, not errors — transformers may produce varying formats.

---

## Caching

### Cache Key

A node's cache key is the SHA-256 hash of:
1. The content hash of each input artifact (recursively resolved)
2. The full text of the transformer YAML
3. For ComfyUI: the full text of the workflow JSON
4. The take index

If all inputs are unchanged and the transformer definition is unchanged, the node is a cache
hit and is skipped.

### Cache Storage

```
.smoketree/cache/{graph_id}/{node_id}/take_{n}/{output_name}.{ext}
```

Cache entries are never automatically deleted (use `smoketree purge`).

### State File

```
.smoketree/state/{graph_id}.json
```

Stores the input hash for each node at last successful execution. Used to detect invalidation
without re-running.

```json
{
  "nodes": {
    "description": {
      "input_hash": "abc123...",
      "take": 0,
      "completed_at": "2025-06-17T12:00:00Z"
    }
  }
}
```

---

## Seeding and Takes

### Deterministic Seeds

Every node execution is assigned a deterministic integer seed derived from:

```python
seed = int(sha256(f"{graph_id}:{node_id}:{take_index}".encode()).hexdigest(), 16) % (2**32)
```

This seed is injected as `SMOKETREE_SEED` / `{seed}` into shell transformers, passed to
Ollama as `options.seed`, and passed to Claude/ComfyUI transformers where applicable.

### Takes

A **take** is an independent execution of a node with a different seed. Takes are intended for
inspecting process determinism and exploring variation — not for branching the pipeline.

- Default take is `0`.
- Takes are specified at the CLI level: `smoketree run portrait --take 2`
- Each take has its own scratch dir and cache entry.
- Nodes default to take `0` when consuming another node's output, regardless of what take was
  run. Override with `--take` at the CLI.

---

## Scratch Directories

Each node execution gets a deterministic scratch directory:

```
.smoketree/scratch/{graph_id}/{node_id}/take_{n}/
```

- Created before execution, never automatically deleted.
- On reruns, the scratch dir is **cleared and recreated** (not accumulated).
- Failed runs leave scratch intact for inspection.
- `smoketree purge` clears scratch dirs.

---

## CLI

### Commands

```bash
# Initialise a new project in the current directory
smoketree init [--name NAME]

# Validate graph and transformer definitions (no execution)
smoketree validate [GRAPH]

# Show execution plan (dry run)
smoketree plan GRAPH [--take N] [--node NODE_ID]

# Run a graph
smoketree run GRAPH [--take N] [--node NODE_ID] [--force]

# Show status of last run
smoketree status [GRAPH]

# Inspect a node's scratch directory
smoketree inspect GRAPH NODE_ID [--take N]

# Clear cache and/or scratch for a graph or node
smoketree purge GRAPH [--node NODE_ID] [--take N] [--scratch] [--cache]
```

### Flags

| Flag | Description |
|------|-------------|
| `--take N` | Run/inspect take N (default: 0) |
| `--node NODE_ID` | Run only this node and its dependencies |
| `--force` | Ignore cache; re-run all nodes |
| `--dry-run` | Alias for `plan` |

### Output

`smoketree run` prints a per-node status line:

```
[SKIP]  source          (cached)
[RUN ]  description     ...
[DONE]  description     (2.3s)
[RUN ]  prompt          ...
[DONE]  prompt          (0.8s)
[RUN ]  generated       ...
[DONE]  generated       (14.2s)
[RUN ]  upscaled        ...
[DONE]  upscaled        (8.1s)
```

Errors print the node ID, transformer, and stderr/stdout tail.

---

## Internal Data Model

These are the core Python types Smoketree works with internally. Pydantic models should
match these shapes.

```python
# Artifact media types
MediaType = Literal["image", "audio", "video", "text", "data"]

# A resolved artifact on disk
@dataclass
class Artifact:
    path: Path
    media: MediaType
    format: str | None        # file extension, e.g. "png"
    content_hash: str         # SHA-256 of file contents

# A node in the graph
@dataclass
class Node:
    id: str
    type: Literal["source", "transform"]
    # source nodes:
    path: Path | None
    # transform nodes:
    transformer: str | None   # transformer name
    inputs: dict[str, str]    # input_name -> node_id (or node_id.output_name)

# A resolved graph (after parsing + validation)
@dataclass
class Graph:
    id: str                   # derived from filename, e.g. "portrait"
    nodes: dict[str, Node]
    execution_order: list[str]  # topological sort

# A transformer definition
@dataclass
class Transformer:
    name: str
    type: Literal["shell", "claude", "comfyui", "http"]
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    # type-specific fields stored as raw dict for now

@dataclass
class InputSpec:
    type: Literal["file"]
    media: MediaType

@dataclass
class OutputSpec:
    type: Literal["file"]
    media: MediaType
    format: str | None
```

---

## Execution Flow

For each node in topological order:

1. **Resolve inputs** — find the cached artifact for each input node.
2. **Compute cache key** — hash inputs + transformer definition + take index.
3. **Check cache** — if cache hit and not `--force`, emit `[SKIP]` and continue.
4. **Prepare dirs** — create/clear scratch dir; create output dir.
5. **Inject seed** — compute deterministic seed from `(graph_id, node_id, take)`.
6. **Execute transformer** — dispatch to the appropriate backend.
7. **Validate outputs** — check files exist; warn on format mismatch.
8. **Write cache** — copy outputs to cache dir; update state file.
9. **Emit status** — print `[DONE]` with elapsed time.

On failure at step 6: print error, leave scratch intact, abort (or continue with `--continue-on-error` future flag).

---

## Milestone Plan

### Milestone 1 — Skeleton (no execution)

**Goal:** A working CLI that can parse and validate a project, and print an execution plan.

Tasks:
- [ ] `uv` project setup, `pyproject.toml`, entry point
- [ ] `smoketree init` — scaffold project directory structure and `smoketree.yaml`
- [ ] Pydantic models for project config, graph, transformer
- [ ] YAML loader with `${ENV_VAR}` substitution
- [ ] Graph parser: node resolution, input reference parsing
- [ ] DAG construction and topological sort (detect cycles)
- [ ] Media type validation (parse-time, cross-node)
- [ ] `smoketree validate` — load and validate, print errors
- [ ] `smoketree plan` — print execution order with cache status (all PENDING at this stage)

**Acceptance criteria:** `smoketree plan portrait` prints a valid execution order for the
example graph without errors. Invalid graphs (cycles, type mismatches, missing transformers)
produce clear error messages.

---

### Milestone 2 — Shell Execution + Caching

**Goal:** A working pipeline that can execute shell transformers with full caching semantics.

Tasks:
- [ ] Scratch dir management (create, clear on rerun)
- [ ] Output dir management
- [ ] Content hashing for source files and artifacts
- [ ] Cache key computation
- [ ] Cache hit/miss logic
- [ ] State file read/write
- [ ] Shell transformer execution (subprocess, env injection, template interpolation)
- [ ] Seed computation and injection
- [ ] Output validation (existence check, format warning)
- [ ] `smoketree run` — full execution loop with `[SKIP]`/`[RUN]`/`[DONE]` output
- [ ] `smoketree run --node` — partial execution
- [ ] `smoketree run --force` — bypass cache
- [ ] `smoketree status` — show last run state
- [ ] `smoketree inspect` — print scratch dir path (and open if possible)
- [ ] `smoketree purge` — clear scratch/cache

**Acceptance criteria:** A linear graph with 3 shell transformer nodes runs end-to-end.
Re-running skips unchanged nodes. Modifying a source file invalidates all downstream nodes.
Scratch dirs are predictable and inspectable.

---

### Milestone 3 — Claude + ComfyUI Transformers

**Goal:** Full support for the primary AI backends.

Tasks:
- [ ] `claude` transformer backend (Anthropic SDK, prompt interpolation, file content injection)
- [ ] `comfyui` transformer backend (HTTP API, workflow JSON injection, output polling)
- [ ] ComfyUI output collection (poll output dir, match by filename prefix)
- [ ] `comfyui_url` config resolution
- [ ] End-to-end test: source → describe (claude) → prompt rewrite (claude) → generate (comfyui) → upscale (comfyui)

**Acceptance criteria:** The full portrait pipeline runs end-to-end against a live ComfyUI
instance and Anthropic API, with correct caching behaviour.

---

## Example: Full Portrait Pipeline

This is the reference example for testing. All three milestones should converge on this
working.

### Project structure
```
portrait-project/
  smoketree.yaml
  sources/
    subject.jpg
  graphs/
    portrait.yaml
  transformers/
    describe.yaml
    description_to_prompt.yaml
    txt2img.yaml
    txt2img.json
    upscale.yaml
    upscale.json
  scripts/
    describe.py                  # used in Milestone 2 shell stub
```

### `graphs/portrait.yaml`
```yaml
name: portrait

nodes:
  source:
    type: source
    path: sources/subject.jpg

  description:
    type: transform
    transformer: describe
    inputs:
      image: source

  prompt:
    type: transform
    transformer: description_to_prompt
    inputs:
      text: description

  generated:
    type: transform
    transformer: txt2img
    inputs:
      prompt: prompt

  upscaled:
    type: transform
    transformer: upscale
    inputs:
      image: generated
```

### `transformers/describe.yaml` (Milestone 2 shell stub)
```yaml
name: describe
type: shell
command: python scripts/describe.py --image {inputs.image} --out {outputs.text}
inputs:
  image:
    type: file
    media: image
outputs:
  text:
    type: file
    media: text
    format: txt
env:
  OPENAI_API_KEY: ${OPENAI_API_KEY}
```

### `transformers/describe.yaml` (Milestone 3 claude version)
```yaml
name: describe
type: claude
model: claude-sonnet-4-6
max_tokens: 1024
system: You are a precise visual analyst. Describe images in rich, accurate detail.
prompt: |
  Describe this image in detail, covering: subject, lighting, composition, mood, colours, and any notable visual elements.

  {inputs.image}
inputs:
  image:
    type: file
    media: image
outputs:
  text:
    type: file
    media: text
    format: txt
```

---

## Notes and Open Questions

- **Parallel execution:** The DAG supports parallel execution of independent nodes. Milestone 2
  may execute serially for simplicity; parallelism can be added in a later pass using asyncio.

- **Multiple outputs:** Transformers may declare multiple outputs. Graph nodes consuming them
  use dot notation (`node_id.output_name`). This should be supported from Milestone 1 in the
  parser, even if not exercised until Milestone 3.

- **Fan-out:** A node's output may be consumed by multiple downstream nodes. This is supported
  naturally by the DAG model — the cache entry is shared.

- **`.env` file:** Project root `.env` is loaded automatically at startup (python-dotenv or
  manual load). Shell environment takes precedence over `.env`.

- **`outputs/` symlinks:** After a successful run, Smoketree should create/update a symlink
  (or copy on Windows) in `outputs/` pointing to the final cache entry for each terminal node.
  Named `{graph_id}__{node_id}.{ext}`. Deferred to Milestone 2.