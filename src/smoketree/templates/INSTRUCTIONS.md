# Smoketree (PathTree core)

Smoketree models a project as a set of **rules** over **paths**. The path is the
identity: you declare where artifacts live with named `{key}` axes, and smoketree infers
the dependency graph, fans out over the keys it discovers on disk, and rebuilds only what
changed (content-hash staleness).

> This build ships the **shell** backend only. The LLM/diffusion backends
> (claude/ollama/comfyui/replicate) are not wired in this slice.

## Project layout

```
smoketree.yaml          project config (name, defaults)
graphs/<id>.yaml        a pipeline: a name + a list of rules
scripts/                helper scripts your rule commands call
sources/                your authored inputs
.smoketree/state/       recorded input hashes (the cache); safe to delete
```

Run a pipeline named `demo` (file `graphs/demo.yaml`) with `smoketree run demo`.

## Rules

A pipeline file is `{ name, rules: [...] }`. Each rule:

```yaml
- name: shout
  in:
    line: "work/episode/{episode}/segment/{segment}/line.txt"
  out:
    loud: "work/episode/{episode}/segment/{segment}/loud.txt"
  run: "tr a-z A-Z < {line} > {loud}"
  prune: false        # optional (scatter GC, see below)
```

- **`in`** / **`out`** map a name to a **path pattern**. **Quote any pattern containing
  `{braces}`** so YAML doesn't read it as a flow mapping.
- **`run`** is a shell command. `{name}` substitutes a resolved input/output path,
  `{key}` substitutes a key value. A list input expands to space-separated paths.

### Patterns: keys vs globs
- `{key}` — a named axis occupying exactly one path segment (`[^/]+`). It binds a value
  and is also a command variable.
- `*` / `**` — plain globs (not keys). An `in` pattern containing a glob makes that input
  a **list**; a pattern with only `{key}`s is a **scalar**.

### The DAG is inferred
There are no explicit edges. A rule runs once its inputs exist on disk; smoketree
re-globs the tree until nothing new is produced (a fixpoint). A rule whose inputs are
another rule's outputs simply fires on a later pass.

## Fan-out falls out of binding

- **map** — every input pattern binds a key → one job per key-tuple.
- **pool / reduce** — an input globs a key axis (`segment/*/loud.txt`) while `out` drops
  it → that input becomes a list, one job per remaining bound tuple.
- **group** — bind some keys, glob the rest → one list-job per bound combo.
- **scatter** — an `out` pattern carries a key no `in` binds (e.g. `segment/{segment}/…`).
  smoketree can't know the count, so `{outname}` resolves to the **owned directory
  prefix** (`work/episode/{episode}/segment/`); your command writes the runtime set under
  it, and the next pass discovers the new keys.

Across a rule's inputs: **same key name = natural join (align)**, **distinct key names =
cross-product**.

## Caching & rebuilds

A job is stale (and re-runs) when it has no record, an output is missing, or its inputs,
command, or **schema** changed. Staleness is content-hashed (gated by a cheap mtime+size
fingerprint, so big media isn't re-read needlessly); identical content never rebuilds.
`--force` ignores the cache.

`prune: true` on a scatter rule deletes managed, key-scoped children under its owned prefix
that vanish from the regenerated set (e.g. an episode re-splitting 7→5 segments removes the
2 stale ones), scoped to the binding so siblings are untouched. It never deletes authored
sources.

## Schemas & typed data

A rule can attach a **JSON Schema (authored in YAML)** to any of its ports — the engine
validates that port's data and, for LLM backends, *constrains* the model's output to it:

```yaml
- name: cast
  in:   { brief: "sources/brief.txt" }
  out:  { cast: "work/cast.yaml" }
  backend: claude
  schema:
    cast: "schema/cast.yaml"      # port name -> schema file (itself YAML)
  config: { prompt: "Build the ensemble cast for: {brief}" }
```

- **Constraint** — when a `claude`/`ollama` rule's (single) output port has a schema, the
  call is constrained to emit JSON matching it. The response is written in the output's
  format **by extension**: `work/cast.yaml` lands as YAML, not JSON. JSON only ever exists
  on the wire to the model — every artifact on disk stays YAML (a `.json` port opts into
  JSON deliberately).
- **Boundary validation (any backend)** — a schema'd **input** is validated before the rule
  runs; a schema'd **output** after. A mismatch is a hard error naming the port and the
  failing field. This catches malformed generations *and* bad hand-edits to data files.
- **Dependency** — schema files are content-hashed into staleness: edit a schema and the
  rule re-runs and re-validates. A missing schema file is an error.

Schemas and data files are read format-agnostically (YAML is a superset of JSON, so either
parses), and a `.yaml` / `.json` / `.csv` output is treated as `data` — its text inlines
into a downstream `{port}` prompt reference like any other input.

## Human-in-the-loop feedback

A rule can declare one or more **feedback channels** attached to its output. Each is an
authored file smoketree seeds once per discovered key-tuple and never clobbers:

```yaml
- name: idea
  in:  { brief: "sources/brief.txt" }
  out: { draft: "brainstorm/ideas/{idea}/draft.md" }
  backend: claude
  feedback:
    - name: content                                  # free-text log (default kind)
      path: "brainstorm/ideas/{idea}/notes.md"
      describe: "Notes on this idea's direction."
    - name: status                                   # a single choice
      path: "brainstorm/ideas/{idea}/status.yaml"
      kind: select
      options: [pending, approve, ignore, recycle]   # default = options[0]
      describe: "Triage this idea."
  config: { ... }
```

- **`notes`** (the default kind) seeds `(no feedback yet)` and accumulates a human's text.
- **`select`** seeds `{name}: {default}` (with the options in a comment) — a single choice,
  validated against `options`. The file is YAML data downstream rules can read and gate on.

`name` (defaulting to the kind) must be unique within a rule; the `path` is keyed by a
subset of the rule's keys. Channels are **plain files consumed downstream as ordinary
inputs** — this block only governs seeding and the workspace UI. A separate rule typically
compiles notes into a directive the prompt consumes, closing the loop:

```
.../notes.md  ──►  fb (ollama: notes -> directive)  ──►  prompt -> render
      ▲                                                          │
      └──────────── human edits the note (workspace or editor) ◄─┘
```

`smoketree workspace <id>` serves a local gallery of every rendered output that has any
`feedback:` channel — a notes box or a select control per channel (with its `describe`),
saving to the channel file.

## Selecting / gating with `filter`

A rule can carry a **`filter`** — a declarative keep/drop predicate over one of its input
data files. The rule emits its output only for bindings that pass, and **drops** the
managed output of bindings that fail. So it *projects a selected subset* that downstream
globs — the data-driven counterpart to the `select` feedback channel above:

```yaml
- name: approved
  in:
    seed:   "brainstorm/ideas/{idea}/seed.yaml"
    status: "brainstorm/ideas/{idea}/status.yaml"   # written by a `select` channel
  out:
    sel: "brainstorm/approved/{idea}/seed.yaml"
  filter: { input: status, field: status, equals: approve }   # or: among: [approve, recycle]
  run: "cp {seed} {sel}"        # any backend; cp/ln projects the selection
```

- `input` names the input port whose data file holds the value; `field` picks a key from
  it (omit for a bare scalar). Keep when the value `equals` a string **or** is `among` a
  list — exactly one of the two (no expression language).
- A passing binding runs normally; a failing one has its output removed and kept out of
  the set. Flip `status` from `approve` to `ignore` and the next run drops
  `approved/{idea}/` back out; flip it back and the entry returns.

Downstream rules glob `brainstorm/approved/{idea}/…`, so they only ever see selected
items. (`recycle` is just its own filtered set — `filter: { …, equals: recycle }` into a
`recycle/` prefix a regen rule consumes.)

## Reading your own output without a cycle (`context`)

A generator that should *consider* existing artifacts — an ignore list, prior results —
but not depend on them needs `context`. Context inputs are globbed and exposed to prompts
and commands as `{name}`, but are **excluded from staleness, the inputs-present gate, and
dependency inference**. So producing more output never re-triggers the rule:

```yaml
- name: brainstorm
  in:      { brief: "brainstorm/runs/{run}/go.yaml" }   # a marker = a deliberate trigger
  context: { ignore: "brainstorm/done/*/lede.yaml" }     # read, but doesn't gate
  out:     { idea: "brainstorm/ideas/{idea}/seed.yaml" } # scatter (no prune; accumulate)
  backend: claude
  config:
    prompt: |
      {brief}
      Do NOT repeat these existing ideas:
      {ignore}
```

If `ignore` were an ordinary `in:`, this would be a self-feeding rule — each new idea
grows the glob, restales `brainstorm`, and the max-iteration breaker fires. As `context`
it doesn't gate, so `brainstorm`'s only staleness driver is its `{run}` marker (and the
prompt). "Brainstorm again" = drop a new `runs/{run}/` marker; each marker runs it once,
appending to the flat `ideas/` pool. Pair it with a `filter` that projects the
"already handled" set into `done/` (excluding `recycle`, so recycled ideas regenerate).

## Authoring generated files (`author`)

By default a rule's output is *managed* — smoketree owns it and overwrites it on the next
run, so hand-edits are lost. To hand-tune a generated file and keep your edits, mark its
output port `author`:

```yaml
- name: brief
  in:  { idea: "ideas/{idea}/seed.yaml" }
  out: { brief: "ideas/{idea}/brief.md" }
  author: [brief]
  backend: claude
  config: { prompt: "Write a brief for {idea}" }
```

The generator writes **`brief.template.md`** (managed — refreshed whenever its inputs
change, never hand-edited). The engine courtesy-copies it to **`brief.md`** once, when
absent; that copy is **yours** — never clobbered, and the file downstream consumes. So:

- editing `brief.md` flows forward (it gates downstream) and survives regeneration;
- when inputs change, the template refreshes but your copy is left alone;
- deleting `brief.md` re-seeds it from the current template (a clean "reset to generated");
- `purge` removes the template, not your copy.

**Reconcile.** When the generator's inputs change, its template moves away from the
fork-base (the template content when you forked). `smoketree reconcile <id>` lists those
*drifted* copies; resolve each with `--merge` (3-way merge the generated changes into your
copy — text, conflict markers on overlap), `--take-generated`, or `--keep-mine`. Each
clears the drift by advancing the fork-base. The workspace shows drift with a diff and the
same three buttons.

## Re-rolling a generative cell (`reroll`)

Staleness is content-addressed, so re-running a cell with unchanged inputs is a no-op — by
design. When you want a *fresh take* of one generated cell (the model didn't comply, or a
seedless model just rolled badly), set `reroll: true`:

```yaml
- name: portrait
  in:  { prompt: "work/{m}/prompt.txt" }
  out: { image: "work/{m}/portrait.png" }
  backend: replicate
  reroll: true
  config: { model: "...", seed_field: seed }
```

The engine keeps a per-cell counter beside the primary output (`portrait.png.roll`). Its
value folds into **both** the staleness hash (so bumping it re-runs just that cell) and the
**seed** (so a seeded backend produces a *different* result, not the same image). Roll `0`
(the default) reproduces today's seed exactly — nothing changes until you actually re-roll.

- `smoketree reroll <id> -w m=alice` bumps the matched cells and re-renders them.
- In the workspace, reviewable outputs of a `reroll` rule get a **🎲 re-roll** button.
- Seedless backends (nano-banana, claude) need no seed — the counter still forces the
  re-run, and the model's own nondeterminism supplies the variation.
- Gotcha: a seed pinned outside the `ctx.seed` path (ollama `options.seed`, or a replicate
  `params` seed with no `seed_field`) wins, so the cell re-runs but reproduces — drive the
  seed via `seed_field` / `seed_inject` for re-roll to vary it.

## CLI

```
smoketree init -t demo        # scaffold a runnable example
smoketree validate <id>       # parse + show inferred order
smoketree plan <id>           # dry run: what would build now
smoketree run <id>            # run to fixpoint
smoketree run <id> --force    # rebuild everything
smoketree run <id> -r brainstorm           # only this rule (repeatable)
smoketree run <id> -w run=r2 -w idea=sunset # only bindings matching these keys
smoketree status <id>         # last-run state
smoketree reconcile <id>      # list authored copies whose template drifted
smoketree reconcile <id> --merge   # 3-way merge generated changes into your copies
smoketree reroll <id> -w m=alice   # fresh take of a generative cell (reroll: true rules)
smoketree workspace <id>      # human-in-the-loop feedback + reconcile UI (needs [workspace] extra)
smoketree purge <id>          # delete managed outputs + state
```
