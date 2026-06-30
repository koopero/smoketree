# Smoketree Examples

A progression of small, self-contained projects that each add **one** concept. Work
through them in order, or jump to the one that matches what you're building.

Every example is a complete smoketree project — its own directory with a `smoketree.yaml`
and a `README.md` walkthrough. Since a project **is** a single graph, you run an example
by `cd`-ing into it and calling `smoketree run`:

```bash
cd examples/01-file-transform
uv run smoketree run
```

## The ladder

| # | Example | Adds | Backend | Status |
|---|---------|------|---------|--------|
| 01 | [File transform](01-file-transform/) | the path model: map, pool, caching | `shell` | ✅ built |
| 02 | [Batching prompts](02-batching-prompts/) | one prompt template fanned across inputs | `ollama` | ✅ built |
| 03 | Using schema | structured output constrained + validated by JSON Schema | LLM | 🚧 planned |
| 04 | Image generation | prompt prebuilding → image model | `replicate` | 🚧 planned |
| 05 | Image editing | combine reference images + a prompt | `replicate`/`comfyui` | 🚧 planned |
| 06 | Feedback loops | refine artifacts from human notes | LLM + `feedback` | 🚧 planned |
| 07 | Brainstorm loop | recurring idea engines without self-feeding | `context` + `trigger` | 🚧 planned |
| 08 | Gating | curate artifacts with a human in the loop | `filter` + `select` | 🚧 planned |
| 09 | Working files | human-edited files inside the system | `author` + reconcile | 🚧 planned |

## Conventions these examples follow

- **Offline-first.** Wherever possible an example runs with no API key and no cost. The
  canonical LLM backend is **Ollama** (local). Each LLM example's README shows the
  one-line `config:` diff to swap to a hosted `claude`/`openai` model.
- **One canonical version per concept.** Backends differ in capability for image work
  (04–05), so those may ship parallel variants; the earlier examples stay single and show
  backend swaps as a documented diff rather than a forked project.
- **Each README is the same shape:** what you'll learn · prerequisites · project layout ·
  the pipeline (annotated) · run it · try this (an edit-then-rerun caching exercise) ·
  swapping backends (LLM examples) · next.

## See also

- The repo [`README.md`](../README.md) — the mental model, caching, backends, and the
  human-in-the-loop tools (`feedback`, `filter`, `reroll`, `trigger`, `context`,
  `author`, `schema`).
- `INSTRUCTIONS.md`, generated into every new project by `smoketree init`, is the
  full local reference.
