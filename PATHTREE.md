# PathTree — a path-based core for smoketree (v2 design)

> Status: design, not built. A clean-break successor to the current content-addressed,
> auto-layout core. Captured from a design thread; the rule grammar below is illustrative,
> not final.

## Why

The current core owns artifact layout: outputs live in opaque hash dirs
(`.smoketree/cache/<graph>/<node>/<inst_hash>/take_N/…`), identity is an instance hash, and
fan-out comes from globbing **source** files only. That bought content-addressed caching but
cost us:

- **Unreadable layout** — hash dirs, hash-suffixed `outputs/` names; we kept hand-building
  human-readable trees (`feedback/characters/<name>.md`) and fighting labels/routing.
- **No dynamic one-to-many** — a node can't explode one input into a runtime-determined set
  of outputs and fan out over them (episode → N segments). This is the core need for media
  breakdown (scatter → map → reduce).

PathTree makes **the path the identity**. The user declares where artifacts live with named
keys; smoketree infers the graph, fans out over keys discovered on disk, and keeps
content-hash staleness. Scatter/pool/group stop being special — they're consequences of which
keys a rule binds, introduces, or globs.

Prior art: Make pattern rules and **Snakemake** (wildcard paths, `expand`, aggregate-by-glob,
checkpoints). This is that model, tuned for expensive AI transforms + human-in-the-loop.

## Core model

### Rules, not nodes
A **rule** = input path-pattern(s) + output path-pattern(s) + a transform (a backend +
command/prompt). There are no explicit node→node edges: the **DAG is inferred** by unifying a
consumer's input pattern with some producer's output pattern.

### Keys and globs
- `{key}` is a **global named axis** occupying exactly one path segment (no `/`).
- `*` / `**` are plain globs (not keys).
- A rule is **parameterized by the keys in its patterns**: the engine runs one job per
  key-tuple discovered by globbing the tree.
- Keys are also **variables** — injectable into commands/prompts, not just paths.

```
# illustrative
rule transcribe:
  in:  work/episode/{episode}/segment/{segment}/audio.wav
  out: work/episode/{episode}/segment/{segment}/transcript.txt
  run: whisper {in} > {out}        # {episode}/{segment} also available as values
```

### Composition: shared name = join, distinct = product
When a rule binds keys from more than one input:
- **same key name → natural join / align** (the old `zip`),
- **distinct key names → cross-product** (the old `product`).

`{episode}` on both inputs pairs them; `{city}` × `{kaiju}` is the full grid. This single rule
subsumes `zip` / `product` / `each`.

### map / pool / group = bind vs glob a key
- **map**: bind every key → one job per tuple.
- **pool / reduce**: glob a key (`segment/*/…`) → that axis collapses into a **list** input →
  one job per remaining tuple.
- **group**: bind some keys, glob the rest → one list-job per bound combo.

### Everything is files (incl. non-file axes)
A parameter sweep, a `take`, or a seed is **not special** — it's an axis grounded in a set of
marker/param files (`take/{take}/seed.txt`, `model/{model}.yaml`). Uniform with episodes and
segments; no separate "parameter" concept.

## Evaluation: a fixpoint loop

```
loop:
  glob the tree, bind keys, instantiate jobs
  run every job that is runnable (inputs present) and stale
  if a full pass produced nothing new -> done
```

- **Scatter and "checkpoints" disappear as concepts.** A scatter is just a rule whose outputs
  the *next* glob discovers; the loop re-plans automatically. Re-planning is cheap relative to
  the transforms, so brute-force re-resolution is fine.
- **Staleness** (build-system style): an `mtime`+`size` fingerprint gates a **content-hash**
  confirm. State records `output_path → input_hash`; the transform text and the path template
  are part of the key. Robust and machine-independent, but cheap on big media.
- **Safety:** ambiguity is tolerated (most-specific-wins is a nicety, not required; collisions
  are the author's problem). The one guard kept is a **max-iteration circuit breaker** that
  errors and names the offending rule — because a runaway loop re-invokes *paid* transformers,
  not just Python.

## Path classes

Two classes, mostly inferred:
- **managed** — produced by a rule. Freely regenerated when inputs change; rebuildable and
  GC-able.
- **authored** — human-owned. Never created from scratch, never clobbered, never deleted.
  Downstream consumes it. A **source** is just an authored file with no upstream; an
  `author`ed output (below) is an authored file *seeded* from a managed template.

`source` vs `managed` is "is it produced by a rule?"; the only flag is `author` (the old
`materialize`, redesigned below).

## Authoring & reconcile (the redesigned `materialize`)

The old `materialize` was a dead end: once you edited the owned file you lost any view of
"what would the generator produce now?", and `--force` was the only escape (which destroys
your edits). `author: true` splits that one frozen file into a triplet:

- **`name.template.ext` — the generated template.** A *plain managed output*: regenerated
  whenever its inputs change, freely overwritten, never hand-edited. (No special freeze logic
  — generation has none anymore.)
- **`name.ext` — the authored copy.** Courtesy-copied from the template **once**, when absent.
  Thereafter human-owned, never clobbered, and **the file downstream consumes** — so edits
  flow forward and editing it is what gates downstream (same fixed-point as the feedback loop).
- **reconcile / backport** — pull template changes *into* the authored copy without losing
  edits.

This **dissolves the "materialized" class**: the generator is ordinary `managed`, the human's
file is `authored`, and all the ownership logic lives in one place instead of being entangled
with "don't regenerate."

Mechanics:
- **Reconcile is media-dependent.** Text → a real **3-way merge**, so the engine stores the
  **fork-base** (template content at courtesy-copy time, like a git merge-base):
  base / theirs (current template) / mine (authored). Binary (`.blend`, images) can't merge →
  reconcile degrades to *replace / keep-mine / re-fork*; the value there is just *seeing the
  template moved*.
- **Drift is actionable.** Because the template is always fresh, `diff template author` always
  works — replacing the old useless `(inputs changed; --force)` note with "the generator moved
  N lines since you forked." The **workspace is the reconcile UI** (show the diff; offer
  take-generated / keep-mine / merge per instance) — HITL, like the selector.
- **Delete-to-reseed:** removing the authored file re-copies the current template next run — a
  clean "reset to generated."
- **Cost:** the template now re-runs the generator on *actual input change* (it's a normal
  managed output), where the old frozen `materialize` never re-ran. Downstream stays gated on
  the authored copy, so nothing churns — but an expensive generator (Blender/LLM/Replicate)
  does pay to refresh its template. Intended: you *want* to know it moved.

## Orphans: `prune`

The fixpoint loop only moves *forward* — it never deletes. When a key disappears (an episode
re-splits 7 → 5 segments), the vanished keys' artifacts become stale orphans.

`prune: true` on a (scatter) rule deletes **managed, key-scoped** children that aren't in the
new output set, **scoped to the binding** (re-splitting episode A never touches B).

- **Nest key-scoped artifacts under the key's path prefix** and prune cascades for free: one
  `rmtree` of `…/segment/{old}/` removes that segment's transcript, frames, and analysis too.
  This is the layout philosophy paying off.
- Never prunes `authored` files (incl. `source`) — only `managed`.
- Caveat: a `{segment}`-keyed file parked *outside* the segment subtree
  (`transcripts/{ep}-{seg}.txt`) won't be caught — the documented price of not nesting.

## Selector: human-in-the-loop curation + gate

A **selector** turns a glob or group into a **managed directory of symlinks** — the selection —
which downstream globs. It reads and writes human feedback (the workspace is its UI).

- **Symlinks keep content-addressing**: staleness resolves to the *target's* hash. Reuse the
  existing symlink-or-copy fallback for Windows.
- **Hand-editable**: the selection is just a dir of links — curate in the workspace or with
  `rm` / `ln -s`. Same philosophy as the feedback files.
- **Two granularities**: over a glob it picks a **subset** (sampling, keep/reject); over a
  group it picks **within each group** (best shot per scene, best-of-N take).
- **Default pass-all**, with optional **`gate: true`** to block downstream until a human
  selects — recovering the original "review gate" idea from one node.
- **Orthogonal to notes**: the selector handles keep/reject (symlinks); the feedback
  accumulator handles text notes. They compose on the same instance — reject a clip *or*
  annotate it.

## Human-in-the-loop, re-expressed

`prune` (GC) + `selector` (curate / gate) + the **feedback accumulator** (notes) re-express
the entire HITL story on the path core — and paths-as-identity make the things we hand-built
(instance-hash labels, `review_notes` routing, the hash-dir `feedback_state/`) trivial or
unnecessary, because the path *is* the key.

## Worked sketch — media breakdown

```
sources:   sources/episode/{episode}/episode.mkv
scatter:   episode/{episode} -> work/episode/{episode}/segment/{segment}/video.mp4   (prune: true)
map:       …/segment/{segment}/{audio -> transcript.txt}
scatter:   …/segment/{segment} -> …/segment/{segment}/shot/{shot}/frame.jpg          (prune: true)
group:     …/segment/{segment}/shot/*/frame.jpg          -> …/segment/{segment}/scene.md   (vision LLM)
pool:      work/episode/{episode}/segment/*/transcript.txt -> work/episode/{episode}/summary.md
pool²:     work/episode/*/summary.md                       -> season/report.md
select:    (human) keep/reject segments -> selected/episode/{episode}/segment/…  (symlinks)
```

The same model runs the generative side: `portrait/{character}/image.png`,
`feedback/{character}/notes.md`, takes as `take/{take}/…` — no hash dirs, paths as labels.

## Deferred / open (lower stakes)

- Ambiguity resolution beyond "tolerated" (Make-style specificity if we want it later).
- Selector predicate sources beyond human: LLM judge, tag/metadata filter, `limit`/sample.
- Key grammar niceties: regex constraints on a key to avoid greedy matches.
- Targeting by path on the CLI (`run 'episode/s01e01/**'` or `--where episode=s01e01`).
- First-class **ffmpeg** op set (probe/split/concat/frames) — the natural scatter/pool
  workhorse; intentionally out of scope here.

## Relationship to current smoketree

Clean break (the project is days old). What carries over: backends still write to engine-
provided target paths (transparent to them); content-hash staleness is kept; `author` (the
redesigned `materialize`), the feedback accumulator, and the workspace re-express on the new
core — generally simpler, since identity is now a readable path instead of a hash.
