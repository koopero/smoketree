"""Project scaffolding for ``smoketree init``.

Creates the directory layout, a project config, and a small set of example graphs and
transformers — including a fully offline-runnable ``demo`` graph (shell only) and the
canonical ``portrait`` AI pipeline from the design.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from .errors import SmoketreeError


def _template(name: str) -> str:
    """Read a packaged template file from ``smoketree/templates/``."""
    return resources.files("smoketree").joinpath("templates", name).read_text()

_SMOKETREE_YAML = """\
name: {name}

defaults:
  comfyui_url: http://localhost:8188
  ollama_url: http://localhost:11434
  take: 0

env:
  ANTHROPIC_API_KEY: ${{ANTHROPIC_API_KEY}}
  OPENAI_API_KEY: ${{OPENAI_API_KEY}}
"""

_GITIGNORE = """\
.smoketree/
.env
outputs/
__pycache__/
"""

_ENV = """\
# Filled in by you. Shell environment takes precedence over this file.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
"""

_HELLO = "the smoketree diffuses like pixels\n"

# --- demo graph (offline, shell only) -------------------------------------- #

_DEMO_GRAPH = """\
name: demo

nodes:
  text:
    type: source
    path: sources/hello.txt

  shout:
    type: transform
    transformer: shout
    inputs:
      input: text

  reverse:
    type: transform
    transformer: reverse
    inputs:
      input: shout

  stats:
    type: transform
    transformer: wordstats
    inputs:
      input: reverse
"""

_FANOUT_GRAPH = """\
name: fanout

# Demonstrates collection nodes + fan-out. `items` resolves to every matching file;
# `shout` runs once per item (expand: each). See INSTRUCTIONS.md for product/zip.
nodes:
  items:
    type: collection
    glob: sources/items/*.txt

  shout:
    type: transform
    transformer: shout
    inputs:
      input: items
    expand: each
"""

_SHELL_TRANSFORMER = """\
name: {name}
type: shell
command: python scripts/textproc.py --op {op} --in {{inputs.input}} --out {{outputs.text}}
inputs:
  input:
    type: file
    media: text
outputs:
  text:
    type: file
    media: text
    format: txt
"""

_TEXTPROC = '''\
"""Tiny text transformer used by the demo graph."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--op", required=True, choices=["upper", "reverse", "stats"])
    parser.add_argument("--in", dest="infile", required=True)
    parser.add_argument("--out", dest="outfile", required=True)
    args = parser.parse_args()

    with open(args.infile) as fh:
        text = fh.read()

    if args.op == "upper":
        result = text.upper()
    elif args.op == "reverse":
        result = text[::-1]
    else:  # stats
        words = text.split()
        result = f"chars={len(text)} words={len(words)} lines={text.count(chr(10))}\\n"

    with open(args.outfile, "w") as fh:
        fh.write(result)


if __name__ == "__main__":
    main()
'''

# --- portrait graph (canonical AI pipeline) -------------------------------- #

_PORTRAIT_GRAPH = """\
name: portrait

nodes:
  source:
    type: source
    path: sources/subject.jpg

  description:
    type: transform
    transformer: describe
    inputs:
      image: source

  prompt:
    type: transform
    transformer: description_to_prompt
    inputs:
      text: description

  generated:
    type: transform
    transformer: txt2img
    inputs:
      prompt: prompt

  upscaled:
    type: transform
    transformer: upscale
    inputs:
      image: generated
"""

_DESCRIBE = """\
# Milestone 2 shell stub. Swap for the claude version (see DESIGN.md) for real output.
name: describe
type: shell
command: python scripts/describe.py --image {inputs.image} --out {outputs.text}
inputs:
  image:
    type: file
    media: image
outputs:
  text:
    type: file
    media: text
    format: txt
env:
  OPENAI_API_KEY: ${OPENAI_API_KEY}
"""

_DESCRIBE_PY = '''\
"""Shell-stub image describer. Replace with a real vision model call."""

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    seed = os.environ.get("SMOKETREE_SEED", "0")
    name = Path(args.image).name
    description = (
        f"A placeholder description of {name} "
        f"(stub describer, seed {seed}). "
        "Replace transformers/describe.yaml with the claude version for real output."
    )
    Path(args.out).write_text(description + "\\n")


if __name__ == "__main__":
    main()
'''

_DESCRIPTION_TO_PROMPT = """\
name: description_to_prompt
type: claude
model: claude-sonnet-4-6
max_tokens: 1024
system: |
  You are an expert at writing image generation prompts.
  Be concise. Output only the prompt, nothing else.
prompt: |
  Convert this image description into a detailed image generation prompt:

  {inputs.text}
inputs:
  text:
    type: file
    media: text
outputs:
  prompt:
    type: file
    media: text
    format: txt
"""

_DESCRIPTION_TO_PROMPT_LOCAL = """\
# Local-first alternative to description_to_prompt.yaml. Point a graph at this
# transformer to run the prompt-rewriting step on a local Ollama model instead of
# the Anthropic API. Requires `ollama serve` and `ollama pull llama3.2`.
name: description_to_prompt_local
type: ollama
model: llama3.2
options:
  num_predict: 1024
system: |
  You are an expert at writing image generation prompts.
  Be concise. Output only the prompt, nothing else.
prompt: |
  Convert this image description into a detailed image generation prompt:

  {inputs.text}
inputs:
  text:
    type: file
    media: text
outputs:
  prompt:
    type: file
    media: text
    format: txt
"""

_TXT2IMG_YAML = """\
name: txt2img
type: comfyui
workflow: txt2img.json
inputs:
  prompt:
    type: file
    media: text
    inject:
      node_id: "6"          # positive prompt node in your workflow
      field: text
outputs:
  image:
    type: file
    media: image
    format: png
    collect:
      node_id: "9"          # SaveImage node in your workflow
      field: filename_prefix
"""

_TXT2IMG_JSON = """\
{
  "_comment": "Replace with your own ComfyUI API-format workflow export.",
  "6": {
    "class_type": "CLIPTextEncode",
    "inputs": {"text": "placeholder prompt"}
  },
  "9": {
    "class_type": "SaveImage",
    "inputs": {"filename_prefix": "smoketree_txt2img"}
  }
}
"""

_UPSCALE_YAML = """\
name: upscale
type: comfyui
workflow: upscale.json
inputs:
  image:
    type: file
    media: image
    inject:
      node_id: "12"
      field: image
outputs:
  image:
    type: file
    media: image
    format: png
    collect:
      node_id: "27"
      field: filename_prefix
"""

_UPSCALE_JSON = """\
{
  "_comment": "Replace with your own ComfyUI API-format workflow export.",
  "12": {
    "class_type": "LoadImage",
    "inputs": {"image": "placeholder.png"}
  },
  "27": {
    "class_type": "SaveImage",
    "inputs": {"filename_prefix": "smoketree_upscale"}
  }
}
"""


def init_project(root: Path, name: str, force: bool = False) -> list[Path]:
    """Scaffold a project at ``root``. Returns the list of created files."""
    config_path = root / "smoketree.yaml"
    if config_path.exists() and not force:
        raise SmoketreeError(
            f"{config_path} already exists. Use --force to overwrite scaffolding."
        )

    files: dict[str, str] = {
        "smoketree.yaml": _SMOKETREE_YAML.format(name=name),
        "INSTRUCTIONS.md": _template("INSTRUCTIONS.md"),
        ".gitignore": _GITIGNORE,
        ".env": _ENV,
        "sources/hello.txt": _HELLO,
        "sources/items/one.txt": "first item\n",
        "sources/items/two.txt": "second item\n",
        "sources/items/three.txt": "third item\n",
        "graphs/demo.yaml": _DEMO_GRAPH,
        "graphs/fanout.yaml": _FANOUT_GRAPH,
        "graphs/portrait.yaml": _PORTRAIT_GRAPH,
        "transformers/shout.yaml": _SHELL_TRANSFORMER.format(name="shout", op="upper"),
        "transformers/reverse.yaml": _SHELL_TRANSFORMER.format(
            name="reverse", op="reverse"
        ),
        "transformers/wordstats.yaml": _SHELL_TRANSFORMER.format(
            name="wordstats", op="stats"
        ),
        "transformers/describe.yaml": _DESCRIBE,
        "transformers/description_to_prompt.yaml": _DESCRIPTION_TO_PROMPT,
        "transformers/description_to_prompt_local.yaml": _DESCRIPTION_TO_PROMPT_LOCAL,
        "transformers/txt2img.yaml": _TXT2IMG_YAML,
        "transformers/txt2img.json": _TXT2IMG_JSON,
        "transformers/upscale.yaml": _UPSCALE_YAML,
        "transformers/upscale.json": _UPSCALE_JSON,
        "scripts/textproc.py": _TEXTPROC,
        "scripts/describe.py": _DESCRIBE_PY,
    }

    created: list[Path] = []
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(path)

    # Empty dirs that hold generated/user content.
    for rel in ("outputs",):
        (root / rel).mkdir(parents=True, exist_ok=True)

    return created
