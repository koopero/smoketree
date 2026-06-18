# Smoketree

A declarative pipeline tool for media transformation. Smoketree models a project as a DAG
of artifacts connected by transformations, caches generated artifacts by content-hashing
inputs, and rebuilds only what has changed.

`smoketree init` scaffolds a project from a starter template (`smoketree init --list` to
see them; the default `minimal` is a bare skeleton). Every project also includes a
project-local `INSTRUCTIONS.md` that documents the model in full (node types, transformers,
collections and fan-out, caching, and the CLI).

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
smoketree init --list              # list starter templates
smoketree init -t demo             # scaffold from a template (default: minimal)
smoketree validate demo            # validate a graph
smoketree plan demo                # show execution plan
smoketree run demo                 # run a graph
smoketree status demo              # show last-run state
smoketree inspect demo NODE        # show a node's scratch dir
smoketree purge demo               # clear cache/scratch
```
