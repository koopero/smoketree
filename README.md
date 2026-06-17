# Smoketree

A declarative pipeline tool for media transformation. Smoketree models a project as a DAG
of artifacts connected by transformations, caches generated artifacts by content-hashing
inputs, and rebuilds only what has changed.

`smoketree init` scaffolds a project with example graphs and a project-local
`INSTRUCTIONS.md` that documents the model in full (node types, transformers, collections
and fan-out, caching, and the CLI).

Transformer backends: `shell`, `claude` (Anthropic API), `ollama` (local LLM inference),
and `comfyui`. For local-first pipelines, use `ollama` transformers — they call a local
[Ollama](https://ollama.com) server (`defaults.ollama_url`), need no API key, and have the
deterministic seed injected as `options.seed`.

Graphs can fan out over **collections** (a glob or a tagged `sources` list); transforms
combine collection inputs with `expand: each | zip | product`, and inputs can select
tagged items with `references[tag]`. Each expanded execution caches independently.

## Install

```bash
uv sync
```

## Usage

```bash
smoketree init --name my-project   # scaffold a project
smoketree validate portrait        # validate a graph
smoketree plan portrait            # show execution plan
smoketree run portrait             # run a graph
smoketree status portrait          # show last-run state
smoketree inspect portrait NODE    # show a node's scratch dir
smoketree purge portrait           # clear cache/scratch
```
