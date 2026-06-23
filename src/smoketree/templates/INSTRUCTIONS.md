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

A rule can declare a **feedback channel** attached to its output:

```yaml
- name: portrait
  in:  { prompt: "work/{m}/prompt.txt" }
  out: { image: "work/{m}/portrait.png" }
  backend: replicate
  feedback:
    append: "feedback/{m}/portrait.md"   # keyed by a subset of the rule's keys
  config: { ... }
```

smoketree **seeds** that notes file once per discovered key-tuple (`(no feedback yet)`) and
never clobbers it — so a human (the feedback *generator*) can write notes there. A separate
rule typically compiles the notes into a directive the prompt consumes, closing the loop:

```
feedback/{m}/portrait.md  ──►  fb (ollama: notes -> directive)  ──►  prompt -> render
        ▲                                                                     │
        └──────────────── human edits the note (workspace or editor) ◄────────┘
```

`smoketree workspace <id>` serves a local gallery of every rendered output that has a
`feedback:` channel; type a note on a card and it saves to that output's channel file.

## CLI

```
smoketree init -t demo        # scaffold a runnable example
smoketree validate <id>       # parse + show inferred order
smoketree plan <id>           # dry run: what would build now
smoketree run <id>            # run to fixpoint
smoketree run <id> --force    # rebuild everything
smoketree status <id>         # last-run state
smoketree workspace <id>      # human-in-the-loop feedback gallery (needs [workspace] extra)
smoketree purge <id>          # delete managed outputs + state
```
