# Smoketree

A declarative pipeline tool for media transformation. Smoketree models a project as a DAG
of artifacts connected by transformations, caches generated artifacts by content-hashing
inputs, and rebuilds only what has changed.

See [DESIGN.md](DESIGN.md) for the full design.

Transformer backends: `shell`, `claude` (Anthropic API), `ollama` (local LLM inference),
and `comfyui`. For local-first pipelines, use `ollama` transformers — they call a local
[Ollama](https://ollama.com) server (`defaults.ollama_url`), need no API key, and have the
deterministic seed injected as `options.seed`.

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
