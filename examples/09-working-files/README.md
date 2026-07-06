# 09 · Working Files — human-edited files in the system

> A generated file you hand-polish, without losing your edits when the source changes.
> `author` splits it into a managed template and a copy that's yours; `reconcile` merges
> upstream changes back in.

## What you'll learn

- **`author` outputs.** Marking an output `author` makes the generator write a
  `*.template` file (managed) and courtesy-copy it once to the real path (yours). Downstream
  reads your copy, so hand-edits flow through and survive regeneration.
- **Delete-to-reseed.** Deleting your copy re-seeds it from the current template — a clean
  "start over from the generated version".
- **`reconcile`.** When the source changes, the template drifts from your copy.
  `smoketree reconcile` lists the drift and 3-way-merges the new generated content into your
  edited copy (or takes one side).

## Prerequisites

None — fully offline. A tiny stdlib Python script (`scripts/draft.py`) renders the draft, so
the generated content is deterministic and merges cleanly.

## Project layout

```text
smoketree.yaml                          config + the graph (two rules)
scripts/draft.py                        facts.txt -> a markdown bio (deterministic)
sources/profile/{person}/facts.txt      the source facts
# generated on run (gitignored):
work/profile/{person}/bio.template.md   the MANAGED draft (regenerated from facts)
work/profile/{person}/bio.md            YOUR copy — seeded once, then hand-editable
directory.md                            all bios assembled from the human copies
```

## The pipeline

```yaml
rules:
  - name: draft
    in:  { facts: "sources/profile/{person}/facts.txt" }
    out: { bio:   "work/profile/{person}/bio.md" }
    author: [bio]                                    # <- split into template + your copy
    run: "python scripts/draft.py {facts} {bio}"

  - name: directory
    in:  { bios: "work/profile/*/bio.md" }           # globs the copy, not the template
    out: { page: "directory.md" }
    run: "cat {bios} > {page}"
```

- `author: [bio]` makes `draft` write **`bio.template.md`** (managed) and seed
  **`bio.md`** from it once. After that, `bio.md` is yours — the engine never overwrites it.
- `directory` reads `bio.md` (your copy), so whatever you write there is what gets assembled.
- `smoketree purge` removes the template, never your copy.

## Run it

```bash
cd examples/09-working-files
uv run smoketree run
```

You get, for each person, a `bio.template.md` and an identical `bio.md`, plus `directory.md`.
Now hand-edit a copy and re-run — only downstream rebuilds, and your edit is kept:

```bash
# tweak Alice's role in YOUR copy
sed -i '' 's/\*\*trail ecologist\*\*/**Trail ecologist \& educator**/' work/profile/alice/bio.md
uv run smoketree run
grep educator directory.md        # your edit flowed into the assembled page
```

## Try this — the source changes, reconcile merges

A fact changes upstream. The template refreshes, but your polished copy is left untouched —
so smoketree flags the *drift* and lets you merge:

```bash
echo "named 2026 conservationist of the year" >> sources/profile/alice/facts.txt
uv run smoketree run                # bio.template.md gains the new line; bio.md unchanged

uv run smoketree reconcile          # -> "alice/bio.md (you edited it)" has drifted
uv run smoketree reconcile --merge  # 3-way merge: your edit + the new highlight
cat work/profile/alice/bio.md       # keeps "& educator" AND gains the new bullet
```

`reconcile` also offers `--take-generated` (discard your copy for the fresh template) and
`--keep-mine` (dismiss the drift). The workspace shows the same drift with a diff and buttons.

## The whole ladder

This is the last stage. Together the examples cover smoketree end to end: the path model and
caching (01), LLM batching and schemas (02–03), image generation and editing (04–05), and
the human-in-the-loop tools — feedback (06), recurring generation (07), gating (08), and now
authored working files (09). See the [examples roadmap](../README.md).
