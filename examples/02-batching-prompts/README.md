# 02 · Batching Prompts — Basic LLM integration

> One prompt template, many inputs. Each item under `sources/` becomes its own LLM call —
> and caching means re-running never re-spends a call you've already made.

## What you'll learn

- **Batching.** A single rule fans one prompt template across every `{item}` discovered
  on disk — the same path-model fan-out as example 1, now driving a model.
- **Named models.** The backend + model are defined once in a top-level `models:` block;
  the rule just references it. Switching Ollama → Claude → OpenAI is a one-block edit.
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
models:
  writer:
    backend: ollama
    model: gemma4:latest          # the one place the backend/model is chosen

rules:
  - name: blurb
    model: writer                 # backend + model come from the def above
    in:
      topic: "sources/item/{item}/topic.txt"
    out:
      blurb: "work/item/{item}/blurb.txt"
    config:
      prompt: |
        Write a single-sentence product blurb for the following item.
        Reply with only the sentence, no preamble.

        Item: {{ topic }}
```

- `{item}` is the fan-out **key**: one model call per `sources/item/<item>/topic.txt`.
- The `writer` def under `models:` names the backend (`ollama`, routed to the local server
  via `ollama_url` in `defaults`) and the model. The rule points at it with `model: writer`
  instead of carrying its own `backend:`.
- A rule's own `config:` merges **under** the def, so per-rule bits — here the `prompt` —
  live on the rule while the shared backend/model lives in one place. (A rule sets *either*
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

Because the backend lives in the `writer` def, switching providers is a one-block edit —
the rule, `in`, `out`, and `prompt` never change. Edit `models.writer` in `smoketree.yaml`:

```yaml
models:
  writer:
    # backend: ollama
    # model: gemma4:latest        # local, no key, no cost

    backend: claude
    model: claude-opus-4-8        # needs ANTHROPIC_API_KEY

    # backend: openai
    # model: gpt-5.1              # needs OPENAI_API_KEY
```

Set an API key (in a `.env` beside `smoketree.yaml`, or the environment) and re-run —
every rule that references `writer` now calls the new provider. That's the payoff of named
models: one switch point no matter how many rules share it. (The change to the def re-stales
the affected cells, so the next `run` regenerates them.)

## Next

**03 · Using Schema** *(planned)* — constrain the model to emit structured data
(JSON Schema) and validate it on the way out. See the
[examples roadmap](../README.md).
