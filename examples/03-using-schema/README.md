# 03 · Using Schema — Structured output for LLMs

> Attach a JSON Schema to a rule's output and the model stops returning prose — it returns
> typed data, constrained on the way out and validated on the way in.

## What you'll learn

- **Schema-constrained generation.** A `schema:` on an LLM output port constrains the model
  to emit JSON matching that shape — no "respond with only JSON" prompt-wrangling.
- **Format follows the path.** The model returns JSON; smoketree writes it in the output's
  on-disk format. A `.yaml` port lands as YAML — JSON only ever exists on the wire.
- **Schemas are validated and cached.** The result is validated against the schema; the
  schema file is a staleness dependency, so editing it regenerates the affected cells.

## Prerequisites

A local [Ollama](https://ollama.com) server and a pulled model — no API key, no cost.

```bash
ollama serve
ollama pull gemma4
```

`validate` works without the server; the live `run` needs it.

## Project layout

```text
smoketree.yaml                  config + the graph (one schema'd rule)
schema/listing.yaml             the JSON Schema (authored in YAML)
sources/item/teacup/topic.txt   authored input
sources/item/lantern/topic.txt  authored input
# generated on run (gitignored):
work/item/{item}/listing.yaml   typed, schema-valid product data
```

## The pipeline

```yaml
rules:
  - name: listing
    model: writer_ollama                   # named backend def; swap by flipping model: (see example 02)
    in:
      topic: "sources/item/{item}/topic.txt"
    out:
      listing: "work/item/{item}/listing.yaml"
    schema:
      listing: "schema/listing.yaml"        # port name -> schema file
    config:
      prompt: |
        Create a product listing for the item described below.

        Item: {{ topic }}
```

The schema is an ordinary JSON Schema, written in YAML:

```yaml
type: object
additionalProperties: false
required: [name, tagline, price_band, keywords]
properties:
  name:       { type: string }
  tagline:    { type: string }
  price_band: { type: string, enum: [budget, mid, premium] }
  keywords:   { type: array, items: { type: string } }
```

- `schema:` maps an **output port** to a schema file. Because the port belongs to an LLM
  rule, smoketree passes the schema to the model as a structured-output constraint *and*
  validates the response against it.
- The output port is `listing.yaml`, so the validated data is written as **YAML** —
  `price_band` is a real enum-checked string, `keywords` a real list. (Name the port
  `.json` to write JSON instead.)
- The schema file is content-hashed into staleness: change the schema and every `listing`
  cell re-runs and re-validates.

## Run it

```bash
cd examples/03-using-schema
uv run smoketree validate
uv run smoketree run
cat work/item/teacup/listing.yaml
```

```yaml
name: 'Whispers of Slate: Hand-Thrown Ceramic Teacup'
tagline: A subtle blend of artisan craft and modern minimalism.
price_band: mid
keywords:
- ceramic teacup
- hand-thrown pottery
- matte glaze
- minimalist tea cup
- artisan drinkware
```

Every field is present and typed — `price_band` is one of the three allowed values, not
free text. (The exact wording varies by model and seed.)

## Try this — the schema drives regeneration

The schema is part of each cell's identity. Add a field and watch only this rule re-run:

```bash
# add `audience` to schema/listing.yaml:
#   audience: { type: string, enum: [home, outdoor, gift] }
# and add it to `required`
uv run smoketree plan      # every `listing` cell is stale — the schema changed
uv run smoketree run       # regenerates with the new field, validated
```

This is the structured-output payoff: the schema is the contract. Evolve the contract and
the data follows; the validation guarantees every artifact on disk matches it — catching
both malformed generations and bad hand-edits before any downstream rule reads them.

## Swapping backends

Identical to [example 02](../02-batching-prompts/#swapping-backends): one named def per
backend (`writer_ollama` / `writer_claude` / `writer_openai`), and you switch by flipping
which `model:` line is commented in the `listing` rule. Hosted models honor JSON Schema
natively, so the `schema:` block is unchanged when you swap.

## Next

**[04 · Image Generation](../04-image-generation/)** — use an LLM to prebuild a prompt, then
hand it to an image model (local ComfyUI, swappable to OpenAI).
