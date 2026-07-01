# 07 · Brainstorm Loop — a recurring idea engine

> Fire it once per "round" and it adds a fresh batch of ideas to a growing pool — reading
> everything it has already produced so it never repeats itself, and never runs away.

## What you'll learn

- **`context` inputs.** A rule can *read* files (here, the whole idea pool) without those
  files being staleness dependencies — so producing more output never re-triggers the rule.
  This is what makes a self-referential loop safe instead of infinite.
- **`trigger` markers.** Generation is gated by a per-round marker file; "brainstorm again"
  means minting a new marker, not re-running the old ones.
- **`explode` into a pool.** One LLM call returns a batch (schema-constrained); the `explode`
  backend fans it into one file per idea, and `protect` keeps the pool append-only.

## Prerequisites

A local [Ollama](https://ollama.com) server + `ollama pull gemma4`. No key, no cost.

> **This is the example where a frontier model earns its keep.** gemma runs the loop fine,
> but recurring ideation lives on *diversity across many rounds* — staying novel as the
> ignore-list grows. That's exactly where a small local model starts repeating itself and a
> frontier model pulls ahead. Flip `ideate` to `writer_claude` / `writer_openai` (with the
> matching API key) and run several rounds to feel the difference.

## Project layout

```text
smoketree.yaml                     config + the graph (two rules)
schema/batch.yaml                  shape of one brainstormed batch
rounds/round-1/go.txt              the seed marker (a round = a trigger)
# generated on run (gitignored):
work/rounds/{round}/batch.yaml     the batch this round produced
pool/{slug}/idea.yaml              the accumulating idea pool (append-only)
```

## The pipeline

```yaml
rules:
  - name: ideate
    model: writer_ollama
    in:      { go:   "rounds/{round}/go.txt" }      # a round marker = a deliberate trigger
    context: { seen: "pool/*/idea.yaml" }            # read the pool, but DON'T gate on it
    out:     { batch: "work/rounds/{round}/batch.yaml" }
    schema:  { batch: "schema/batch.yaml" }
    trigger:
      marker: "rounds/{round}/go.txt"
      describe: "Brainstorm another batch"
      content: "go\n"
    config:
      prompt: |
        Invent 4 fresh, distinct product ideas ...
        Do NOT repeat any of these existing ideas:
        {% for s in seen %}- {{ s.title }}
        {% endfor %}

  - name: collect
    backend: explode
    in:  { batch: "work/rounds/{round}/batch.yaml" }
    out: { idea:  "pool/{slug}/idea.yaml" }
    config: { items: ideas, key: title, protect: "pool/{slug}/idea.yaml" }
```

- `seen` is a **`context`** input: the prompt loops over the pool to avoid repeats, but
  because context is excluded from staleness, the pool growing does **not** re-stale
  `ideate`. Without this, every new idea would enlarge the glob, re-trigger `ideate`, and the
  max-iterations breaker would fire. (Try changing `context:` to `in:` to watch that happen.)
- `{round}` comes from the marker, so `ideate` runs exactly once per round marker. The
  `trigger` block gives the workspace a "generate more" button that mints the next marker;
  from the shell you just create one (below).
- `collect` uses **`explode`** to fan the batch's `ideas` array into `pool/<slug>/idea.yaml`,
  slugging each `title`. `protect` skips any idea whose pool file already exists, so re-runs
  and overlapping titles never clobber the pool — it only grows.

## Run it

```bash
cd examples/07-brainstorm-loop
uv run smoketree run                 # round-1 marker is already here
ls pool/                             # 4 ideas
```

Brainstorm another batch — mint a new round marker and run again:

```bash
mkdir -p rounds/round-2 && echo go > rounds/round-2/go.txt
uv run smoketree plan                # only round-2 is stale; round-1 stays put
uv run smoketree run
ls pool/                             # ~8 ideas — round 2 avoided round 1's
```

Each new `rounds/<round>/go.txt` adds one batch; the pool accumulates and the model steers
around everything already in it. Re-running **without** a new marker does nothing at all —
that's the `context`/`trigger` split doing its job.

## The workspace

`smoketree workspace` renders the `trigger` as a **"Brainstorm another batch"** button, so a
non-technical reviewer can keep the engine going with a click instead of `mkdir`. Pair this
with [example 08 · Gating](../README.md) to let a human approve which pooled ideas advance.

## Swapping backends

`ideate` uses the `writer_ollama` def; flip its `model:` line to `writer_claude` /
`writer_openai` (see [example 02](../02-batching-prompts/#swapping-backends)). As noted
above, this is the example where that swap most changes the *quality* of the result.

## Next

**08 · Gating** *(planned)* — a human approves which pooled ideas advance, using a `select`
feedback channel and a `filter`. See the [examples roadmap](../README.md).
