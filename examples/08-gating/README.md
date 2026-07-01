# 08 · Gating — curating with a human in the loop

> A reviewer sets each candidate's status; a `filter` rule projects only the approved ones
> forward. Everything downstream globs the curated subset — never the rejects.

## What you'll learn

- **`select` feedback channels.** Beyond free-text notes, a channel can be a validated
  single choice (`pending` / `approve` / `reject`), seeded once as a YAML file the human sets.
- **`filter` rules.** A rule keeps or drops each binding based on an input's value —
  projecting a *selected subset* that downstream rules glob.
- **Reversible curation.** Flipping a status re-runs the gate: approve to advance an item,
  reject to drop it back out. The decision is just a value in a file.

## Prerequisites

None — fully offline (shell only), no model or key. The gate is backend-agnostic; put it in
front of whatever expensive generation a real project runs, so you only spend on approved
items.

## Project layout

```text
smoketree.yaml                              config + the graph (three rules)
sources/candidate/{candidate}/idea.md       the candidates to curate
# generated on run (gitignored):
work/candidate/{candidate}/card.md          the review card
review/candidate/{candidate}/status.yaml    YOUR decision: pending | approve | reject
review/candidate/{candidate}/notes.md       YOUR notes
approved/{candidate}/idea.md                only approved candidates land here
shortlist.md                                the assembled, curated output
```

## The pipeline

```yaml
rules:
  - name: review                    # surface each candidate + attach the human channels
    in:  { idea: "sources/candidate/{candidate}/idea.md" }
    out: { card: "work/candidate/{candidate}/card.md" }
    run: "cp {idea} {card}"
    feedback:
      - name: status
        path: "review/candidate/{candidate}/status.yaml"
        kind: select
        options: [pending, approve, reject]
        describe: "Approve to advance this candidate."
      - name: notes
        path: "review/candidate/{candidate}/notes.md"

  - name: gate                      # keep only status == approve; drop the rest
    in:
      idea:   "sources/candidate/{candidate}/idea.md"
      status: "review/candidate/{candidate}/status.yaml"
    out: { approved: "approved/{candidate}/idea.md" }
    filter: { input: status, field: status, equals: approve }
    run: "cp {idea} {approved}"

  - name: shortlist                 # downstream only ever globs approved/*
    in:  { approved: "approved/*/idea.md" }
    out: { list: "shortlist.md" }
    run: "cat {approved} > {list}"
```

- `review` **declares a `select` channel**. smoketree seeds
  `review/candidate/<candidate>/status.yaml` with `status: pending` (and the options as a
  comment) — once, and never clobbers your edit.
- `gate` carries a **`filter`**: `input: status` names the port, `field: status` reads the
  value, and only `equals: approve` passes. A passing binding writes `approved/<candidate>/`;
  a failing one has that output removed and kept out of the set.
- `shortlist` globs `approved/*/idea.md`, so it only ever sees approved candidates. In a real
  project this is where your costly work goes — it runs on the curated subset, not the rejects.

## Run it

```bash
cd examples/08-gating
uv run smoketree run
```

The first run seeds every status to `pending`, so the gate lets **nothing** through yet —
`approved/` is empty and `shortlist.md` isn't created. Now approve a couple and re-run:

```bash
echo "status: approve" > review/candidate/compliment/status.yaml
echo "status: approve" > review/candidate/sock/status.yaml
uv run smoketree plan     # gate(timer) DROP; gate(compliment,sock) RUN
uv run smoketree run
cat shortlist.md          # just the two approved candidates
```

`smoketree plan` labels each binding — `DROP` for the filtered-out ones, `RUN` for the
passing ones — so you can see the gate before committing to a run.

## Try this — reverse a decision

```bash
echo "status: reject" > review/candidate/compliment/status.yaml
uv run smoketree run
cat shortlist.md          # compliment dropped back out; sock remains
```

The run reports `drop approved/compliment/idea.md` and rebuilds the shortlist without it.
Curation is just data: flip the value, re-run, and the set updates.

## The workspace

`smoketree workspace` renders each review card with a **select control** for its status and
a notes box — so a reviewer approves and rejects by clicking, then hits run. The statuses
they set are the same `status.yaml` files this example edits by hand.

## Next

**09 · Working Files** *(planned)* — generated files a human hand-edits, kept in sync with
`author` + `reconcile`. See the [examples roadmap](../README.md).
