# 02 · Batching Prompts — Basic LLM integration

> One prompt template, many inputs. Each item under `sources/` becomes its own LLM call —
> and caching means re-running never re-spends a call you've already made.

## What you'll learn

- **Batching.** A single rule fans one prompt template across every `{item}` discovered
  on disk — the same path-model fan-out as example 1, now driving a model.
- **Named models.** Each backend is a named def in the `models:` block; the rule references
  one with `model:`. Switching Ollama → Claude → OpenAI is a one-line change.
- **Prompt templating.** Inputs are exposed to the prompt as Jinja2 variables
  (`{{ topic }}`).
- **Caching applies to model calls too.** An unchanged input never re-calls the model;
  edit one item and only that one regenerates.

## Prerequisites

A local [Ollama](https://ollama.com) server and one pulled model — **no API key, no cost.**

```bash
ollama serve            # if it isn't already running
ollama pull gemma4      # the model this example calls (any small chat model works)
```

`validate` works without the server; the live `run` needs it.

## Project layout

```text
smoketree.yaml                  the project = config + the graph (one ollama rule)
sources/item/teacup/topic.txt   authored input
sources/item/lantern/topic.txt  authored input
# generated on run (gitignored):
work/item/{item}/blurb.txt      one blurb per item
```

## The pipeline

```yaml
rules:
  - name: blurb
    model: writer_ollama          # pick a backend def; the others are commented below
    # model: writer_claude
    # model: writer_openai
    in:
      topic: "sources/item/{item}/topic.txt"
    out:
      blurb: "work/item/{item}/blurb.txt"
    config:
      prompt: |
        Write a single-sentence product blurb for the following item.
        Reply with only the sentence, no preamble.

        Item: {{ topic }}

models:
  writer_ollama: { backend: ollama, model: gemma4:latest }
  writer_claude: { backend: claude, model: claude-opus-4-8 }
  writer_openai: { backend: openai, model: gpt-5.1 }
```

- `{item}` is the fan-out **key**: one model call per `sources/item/<item>/topic.txt`.
- Each backend is its own named def under `models:`. The rule points at one with
  `model: writer_ollama` (which names the backend — `ollama`, routed to the local server via
  `ollama_url` in `defaults` — and the model) instead of carrying its own `backend:`.
- A rule's own `config:` merges **under** the def, so per-rule bits — here the `prompt` —
  live on the rule while the shared backend/model lives in the def. (A rule sets *either*
  `model:` *or* `backend:`, never both.)
- The prompt is a **Jinja2 template**. Each `in:` port is available by name: `topic` is a
  text file, so `{{ topic }}` inlines its contents. (Data inputs — `.yaml`/`.json` — would
  inline as structured objects you can index, e.g. `{{ topic.name }}`.)

## Run it

```bash
cd examples/02-batching-prompts
uv run smoketree validate     # parses offline; prints the single rule: blurb
uv run smoketree run          # needs Ollama up
```

```
[run ] blurb(item=teacup)  (new)
[run ] blurb(item=lantern)  (new)
Done — 2 job(s) executed.

$ cat work/item/teacup/blurb.txt
Elevate your quiet ritual with this beautifully substantial teacup, featuring a
tactile hand-throw and the grounding depth of matte slate glaze.
```

(Model wording will vary run-to-run; that's the model, not smoketree.)

## Try this — caching is the point

Run again — no model calls happen, because every input is content-current:

```bash
uv run smoketree run
# Nothing to do — all outputs up to date.
```

Add or edit one item and only it regenerates:

```bash
mkdir -p sources/item/kettle
echo "a cast-iron kettle with a bamboo handle" > sources/item/kettle/topic.txt
uv run smoketree run          # calls the model for `kettle` only; teacup/lantern stay cached
```

This is what makes batching affordable at scale: a 500-item run that fails on item 300
resumes from 300, and tweaking one source re-spends exactly one call.

## Swapping backends

Each backend is its own def, so switching providers is a **one-line change** — flip which
`model:` line is commented in the `blurb` rule. The rule, `in`, `out`, and `prompt` never
change:

```yaml
rules:
  - name: blurb
    # model: writer_ollama
    model: writer_claude          # needs ANTHROPIC_API_KEY
    # model: writer_openai
```

Set the matching API key (in a `.env` beside `smoketree.yaml`, or the environment) and
re-run. That's the payoff of named defs: the alternatives stay valid and ready, so swapping
is a reference change, not a rewrite. The switch re-stales the affected cells, so the next
`run` regenerates them.

## Next

**[03 · Using Schema](../03-using-schema/)** — constrain the model to emit structured data
(JSON Schema) and validate it on the way out.
