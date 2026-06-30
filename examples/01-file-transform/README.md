# 01 · Simple Workflow — File transform

> Two shell rules turn source notes into headlines and a combined index. The smallest
> possible introduction to the smoketree path model — and to caching.

## What you'll learn

- **The path model.** You declare *where* artifacts live with `{key}` axes; smoketree
  infers the dependency graph from those paths — there are no explicit edges.
- **map** (one job per key) and **pool** (a `*` glob collapses an axis into a list).
- **Content-hash caching.** Re-running is a no-op until a source actually changes, and
  then only the affected branch rebuilds.

## Prerequisites

None. This example is **fully offline** — pure shell, no API keys, no services.

## Project layout

```text
smoketree.yaml                 the project = config + the graph (two rules)
sources/note/kaiju/raw.md      authored input
sources/note/harbor/raw.md     authored input
# generated on run (gitignored):
work/note/{note}/headline.txt  per-note output of the `headline` rule
index.txt                      combined output of the `index` rule
```

A smoketree project **is** a single graph: `smoketree.yaml` holds `name`, `defaults`,
and the `rules`, and you run it with `smoketree run`.

## The pipeline

```yaml
rules:
  # map: one job per {note}. Take the first line of each source note and shout it.
  - name: headline
    in:
      raw: "sources/note/{note}/raw.md"
    out:
      headline: "work/note/{note}/headline.txt"
    run: "head -n1 {raw} | tr a-z A-Z > {headline}"

  # pool: the * glob collapses the {note} axis into a list — one combined index.
  - name: index
    in:
      all: "work/note/*/headline.txt"
    out:
      index: "index.txt"
    run: "cat {all} > {index}"
```

- `{note}` is a **key** — one path segment. It appears in both `in` and `out` of
  `headline`, so that rule **fans out**: one job per `sources/note/<note>/raw.md` found
  on disk (`kaiju`, `harbor`).
- In `index`, the input path uses `*` instead of `{note}`. A glob makes that input a
  **list**, collapsing the `{note}` axis — so `index` runs once over *all* headlines.
- `{raw}`, `{headline}`, `{all}`, `{index}` in the `run` command are replaced with the
  resolved paths. smoketree creates output parent directories before the command runs.
- smoketree sees that `headline`'s output (`work/note/{note}/headline.txt`) feeds
  `index`'s input (`work/note/*/headline.txt`) and orders them automatically.

## Run it

```bash
cd examples/01-file-transform
uv run smoketree validate     # parses; prints the inferred order: headline -> index
uv run smoketree run
```

Expected:

```
[run ] headline(note=harbor)  (new)
[run ] headline(note=kaiju)  (new)
[run ] index()  (new)
Done — 3 job(s) executed.
```

```bash
cat index.txt
# DAWN LIGHT ON WET ROOFTOPS
# THE SMOKETREE DIFFUSES LIKE PIXELS
```

## Try this — selective caching

Run again. Nothing happens — every output is content-current:

```bash
uv run smoketree run
# Nothing to do — all outputs up to date.
```

Now edit just one source and ask what *would* run:

```bash
echo "a tidal hush before the roar" > sources/note/kaiju/raw.md
uv run smoketree plan
```

```
[SKIP   ] headline(note=harbor)  (up to date)
[RUN    ] headline(note=kaiju)  (inputs changed)
[SKIP   ] index()  (up to date)
```

Only `kaiju`'s branch is stale — `harbor` is untouched. (`plan` is a single-pass dry
run, so `index` still reads as up-to-date; on the actual `run`, `headline(kaiju)`
rebuilds first and `index` then rebuilds because its input changed.) `uv run smoketree
run` applies exactly that, and `uv run smoketree purge` removes all generated outputs.

## Next

**[02 · Batching Prompts](../02-batching-prompts/)** — swap the shell command for a
local LLM and fan one prompt template across many inputs.
