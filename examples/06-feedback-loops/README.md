# 06 · Feedback Loops — refining artifacts

> Generate a draft, leave a human note beside it, and the next run folds that note back in —
> refining only the cells you touched. Your notes are plain files smoketree never overwrites.

## What you'll learn

- **Feedback channels.** A rule can declare a `feedback:` channel — a human-owned file
  smoketree seeds once beside each output and then never clobbers.
- **Closing the loop without a cycle.** A second rule reads those notes into a *directive*
  that feeds the generator, so human input steers the next generation.
- **Selective refinement.** Editing one note re-runs only that item's branch; everything
  else stays cached.

## Prerequisites

A local [Ollama](https://ollama.com) server + a pulled model (`ollama pull gemma4`). No key,
no cost. `validate` works without it.

## Project layout

```text
smoketree.yaml                           config + the graph (two rules)
sources/product/gadget/brief.txt         a product brief
sources/product/candle/brief.txt         a product brief
# generated on run (gitignored):
feedback/product/{product}/notes.md      YOUR notes — seeded once, then hand-edited
work/product/{product}/directive.txt     the note distilled into a directive
work/product/{product}/tagline.txt       the tagline, following the directive
```

## The pipeline

```yaml
rules:
  # 1. distill the human's notes into one directive (just the placeholder on the first run)
  - name: distill
    model: writer_ollama
    in:  { notes: "feedback/product/{product}/notes.md" }
    out: { directive: "work/product/{product}/directive.txt" }
    config:
      prompt: |
        Turn these review notes into ONE concise directive. If there is no real
        feedback, reply with exactly "(no direction)".
        Notes: {{ notes }}

  # 2. write the tagline from brief + directive; THIS rule owns the notes channel
  - name: tagline
    model: writer_ollama
    in:
      brief:     "sources/product/{product}/brief.txt"
      directive: "work/product/{product}/directive.txt"
    out:
      tagline: "work/product/{product}/tagline.txt"
    feedback:
      - name: notes
        path: "feedback/product/{product}/notes.md"
        describe: "How should this tagline change?"
    config:
      prompt: |
        Write a single punchy tagline (under 8 words), following the art direction.
        Product: {{ brief }}
        Art direction: {{ directive }}
```

- The `tagline` rule **declares** the feedback channel. smoketree discovers `{product}` from
  the briefs and seeds `feedback/product/<product>/notes.md` with `(no feedback yet)` — once.
  It never overwrites a note you've edited.
- The `distill` rule **reads** those notes and turns them into `directive.txt`, which the
  `tagline` rule consumes. Notes → directive → tagline is a chain, not a cycle: the generator
  never depends on its own output, so producing a tagline doesn't re-trigger itself.
- Because `notes.md` is a tracked input to `distill`, editing it re-stales exactly that
  product's `distill` **and** `tagline` — nothing else.

## Run it

```bash
cd examples/06-feedback-loops
uv run smoketree run
```

The first run seeds the notes, distills them to `(no direction)`, and writes a first-pass
tagline for each product:

```
  seed   feedback/product/candle/notes.md
  seed   feedback/product/gadget/notes.md
[run ] distill(product=candle)  (new)
[run ] distill(product=gadget)  (new)
[run ] tagline(product=candle)  (new)
[run ] tagline(product=gadget)  (new)

$ cat work/product/gadget/tagline.txt
Sunlight charges your soundtrack.
```

## Try this — leave feedback, refine

Write a note into one product's channel and re-run:

```bash
echo "Too clever. Make it plain and benefit-first: emphasize never needing a cable." \
  > feedback/product/gadget/notes.md
uv run smoketree plan      # only gadget's distill + tagline are stale
uv run smoketree run
```

```
$ cat work/product/gadget/directive.txt
Keep the language plain and focused on the benefit: freedom from charging cables.

$ cat work/product/gadget/tagline.txt
Charge your sound, anywhere you sit.
```

The candle branch never re-ran, and your note is still there — smoketree seeds a channel
once and then treats it as yours. Keep editing the note and re-running to iterate.

## The workspace

Editing files by hand is the mechanism; `smoketree workspace` is the UI for it. It serves a
local gallery of every output whose rule has a `feedback:` channel, with a notes box per
cell and a run button — so a reviewer refines cells without touching the shell. The notes
they write are the same plain files this example edits.

## Swapping backends

Both rules use the `writer_ollama` def; flip their `model:` lines to `writer_claude` /
`writer_openai` (see [example 02](../02-batching-prompts/#swapping-backends)).

## Next

**[07 · Brainstorm Loop](../07-brainstorm-loop/)** — a recurring idea engine that reads its own
past output as `context` without re-triggering itself.
