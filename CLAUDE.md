# Smoketree — project notes for Claude

Smoketree is "Make for human-guided media generation": a file-based workflow runner where
rules over paths form an inferred DAG, and only stale work re-runs (content-hash caching).

## Core invariant: project == graph
A project is a single graph. `smoketree.yaml` holds `name`, `defaults`, and the graph itself
(`models:` + `rules:`). There is **no** `graphs/` directory and **no** graph argument on the
CLI. Commands operate on the one project graph.

## Commands
- Run / inspect (from a project dir, no graph arg):
  `uv run smoketree run | validate | plan | status | reroll | reconcile | purge`
- Tests: `uv run pytest` (all live in `tests/test_smoketree.py`).
- Scaffold: `uv run smoketree init -t demo` (offline shell demo) or `-t minimal`.

## Layout
- `src/smoketree/` — core. `models.py` (Pydantic: `ProjectConfig` carries the graph raw,
  validated lazily via `load_pipeline`), `bind.py` (globbing/binding), `engine.py` (fixpoint,
  staleness, feedback seeding), `backends/` (one file per backend), `workspace/` (review UI).
- `examples/` — the documented example ladder (see below).

## Backends
`shell`, `ollama`, `openai`, `openai_image`, `claude`, `replicate`, `comfyui`, `explode`,
`blender`.
- `openai` is **chat/vision only — it cannot generate images**; use `openai_image`
  (Images API, gpt-image-1) for that.
- LLM structured output goes through `serde.write_structured`, which `raw_decode`s the first
  JSON value (tolerates a local model's trailing junk).
- `blender` runs a bpy script headless (`--background --python <script>`); inputs/outputs/
  keys/args are handed over via a job JSON, its path in the `SMOKETREE_JOB` env var (the
  script side is `json.load(open(os.environ["SMOKETREE_JOB"]))`) — no argv parsing. A `data`
  input (`.yaml`/`.json`) is pre-parsed by smoketree and embedded as a structure, since
  Blender's bundled Python has no pyyaml. Blender executable resolves `BLENDER_PATH` env >
  `defaults.blender_path` > `blender` on PATH.

## Authoring examples (the active workstream)
- Each example is a standalone project: `examples/<nn>-<slug>/` with `smoketree.yaml`,
  `README.md`, `sources/`, and its own `.gitignore` (ignore `work/`, `.smoketree/`, `.env`,
  and any seeded/generated state like `feedback/`, `pool/`, `approved/`, `directory.md`).
- **Offline-first**: canonical LLM backend is local `ollama` (`gemma4:latest`); each
  swappable example defines one named def per backend and toggles with a commented `model:`
  line in the rule (`writer_ollama` / `writer_claude` / `writer_openai`), `rules:` before
  `models:`. Frontier models mainly matter for 07 (brainstorm diversity).
- **Verify live before writing the README**, then clean generated artifacts with **absolute
  paths** (the Bash tool's cwd resets between calls — a relative `rm` can hit the repo root).
- READMEs share a shape: what you'll learn · prerequisites · layout · pipeline · run it ·
  try this · swapping backends · next.

## Gotchas
- A ComfyUI rule's external **workflow JSON is not tracked for staleness** — editing it
  (e.g. `denoise`) needs `smoketree run --force`. Only rule config + inputs + schema are hashed.
  Same gap for a **blender rule's script** — editing it needs `--force` too.
- SD 1.5 is too weak for image editing; example 05 uses **Flux 2 Klein** (ComfyUI:
  `flux-2-klein-4b` + `qwen_3_4b` clip + ReferenceLatent edit workflow).

## Working agreements
- **Commit only when asked.** Branch off `main`; never commit to it directly.
- End commit messages with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
- Don't commit `.claude/`.
