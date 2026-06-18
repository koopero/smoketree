"""Project scaffolding for ``smoketree init``.

`init` creates a project from a chosen **starter template** (see ``TEMPLATES``). Every
project also gets the shared ``INSTRUCTIONS.md`` guide, a ``.gitignore``, and the standard
directory skeleton. The default template, ``minimal``, is just the bare skeleton.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from .errors import SmoketreeError

_NAME_TOKEN = "__PROJECT_NAME__"
_STANDARD_DIRS = ("graphs", "transformers", "sources", "outputs")


def _instructions() -> str:
    return resources.files("smoketree").joinpath("templates", "INSTRUCTIONS.md").read_text()


# --------------------------------------------------------------------------- #
# Shared files (written into every project)
# --------------------------------------------------------------------------- #

_GITIGNORE = """\
.smoketree/
.env
outputs/
__pycache__/
"""

# --------------------------------------------------------------------------- #
# Project config variants
# --------------------------------------------------------------------------- #

_CONFIG_MINIMAL = f"""\
name: {_NAME_TOKEN}

defaults:
  take: 0
"""

_CONFIG_AI = f"""\
name: {_NAME_TOKEN}

defaults:
  comfyui_url: http://localhost:8188
  ollama_url: http://localhost:11434
  take: 0
"""

# --------------------------------------------------------------------------- #
# Shared building blocks (shell text-processing example)
# --------------------------------------------------------------------------- #

_TEXTPROC = '''\
"""Tiny text transformer used by the shell example graphs."""

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


def _shell_transformer(name: str, op: str) -> str:
    return (
        f"name: {name}\n"
        "type: shell\n"
        f"command: python scripts/textproc.py --op {op} "
        "--in {inputs.input} --out {outputs.text}\n"
        "inputs:\n"
        "  input:\n"
        "    type: file\n"
        "    media: text\n"
        "outputs:\n"
        "  text:\n"
        "    type: file\n"
        "    media: text\n"
        "    format: txt\n"
    )


_ITEMS = {
    "sources/items/one.txt": "first item\n",
    "sources/items/two.txt": "second item\n",
    "sources/items/three.txt": "third item\n",
}

# --------------------------------------------------------------------------- #
# Graph definitions
# --------------------------------------------------------------------------- #

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

# Collection fan-out: `items` resolves to every matching file; `shout` runs once per
# item (expand: each). See INSTRUCTIONS.md for product/zip.
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

_TAGGED_GRAPH = """\
name: tagged

# Tagged collection + filter_tag. `refs` items carry role tags; `loud` filters to the
# 'shout' tag (2 items -> fan-out), `quiet` filters to 'whisper' (1 item -> scalar).
nodes:
  refs:
    type: collection
    sources:
      - {path: sources/items/one.txt, tags: [shout, primary]}
      - {path: sources/items/two.txt, tags: [shout]}
      - {path: sources/items/three.txt, tags: [whisper]}
  loud:
    type: transform
    transformer: shout
    inputs:
      input: refs[shout]
    expand: each
  quiet:
    type: transform
    transformer: shout
    inputs:
      input: refs[whisper]
"""

# --- portrait (local-first AI pipeline) ------------------------------------ #

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

_DESCRIBE_OLLAMA = """\
# Local image description via Ollama. Pull a vision model first, e.g. `ollama pull llava`.
# For thinking-capable vision models (e.g. gemma3), add a `think: false` line.
name: describe
type: ollama
model: llava
options:
  num_predict: 300
system: You are a precise visual analyst. Describe images in rich, accurate detail.
prompt: |
  Describe this image in detail: subject, colors, lighting, composition, and mood.

  {inputs.image}
inputs:
  image:
    type: file
    media: image
outputs:
  text:
    type: file
    media: text
    format: txt
"""

_PROMPT_OLLAMA = """\
# Local prompt rewriting via Ollama. Pull a text model first, e.g. `ollama pull llama3.2`.
# (For a thinking-capable model like gemma3, add a `think: false` line.)
name: description_to_prompt
type: ollama
model: llama3.2
options:
  num_predict: 1024
system: |
  You convert image descriptions into prompts for an image-generation model.

  Your goal is FAITHFUL COMPLETENESS, not brevity. Carry over EVERY concrete detail
  the description mentions, including:
    - the main subject and its identity/pose/expression/age
    - clothing, accessories, materials, patterns, and textures
    - all named colors
    - lighting (direction, quality, time of day) and mood/atmosphere
    - setting/background and any secondary objects
    - composition, framing, camera angle, depth of field
    - any artistic style, medium, or rendering cues
  Do not summarize, generalize, merge, or drop details. Do not invent details that
  are not present or implied in the description.

  Output ONLY the finished prompt — one dense block of comma-separated descriptive
  phrases (most specific subject details first). No preamble, headings, quotes, or
  explanation.
prompt: |
  Rewrite the following image description as a single detailed image-generation
  prompt that captures everything it mentions:

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
# Inject the per-take seed so different --take values produce different images.
seed_inject:
  node_id: "3"          # KSampler node in your workflow
  field: seed
inputs:
  prompt:
    type: file
    media: text
    inject:
      node_id: "6"        # positive prompt (CLIPTextEncode) node
      field: text
outputs:
  image:
    type: file
    media: image
    format: png
    collect:
      node_id: "9"        # SaveImage node
      field: filename_prefix
"""

_TXT2IMG_JSON = """\
{
  "_comment": "Replace with your own ComfyUI API-format workflow export (Save (API Format)).",
  "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20}},
  "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "placeholder prompt"}},
  "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "smoketree_txt2img"}}
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
  "_comment": "Replace with your own ComfyUI API-format workflow export (Save (API Format)).",
  "12": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
  "27": {"class_type": "SaveImage", "inputs": {"filename_prefix": "smoketree_upscale"}}
}
"""

_PORTRAIT_SOURCES_NOTE = (
    "Put your input image here as `subject.jpg` "
    "(referenced by graphs/portrait.yaml).\n"
)


# --------------------------------------------------------------------------- #
# Template catalog
# --------------------------------------------------------------------------- #


class Template:
    def __init__(self, description: str, files: dict[str, str]):
        self.description = description
        self.files = files


TEMPLATES: dict[str, Template] = {
    "minimal": Template(
        "Bare skeleton — just smoketree.yaml and empty dirs.",
        {"smoketree.yaml": _CONFIG_MINIMAL},
    ),
    "demo": Template(
        "Offline shell pipeline: source -> shout -> reverse -> stats.",
        {
            "smoketree.yaml": _CONFIG_MINIMAL,
            "sources/hello.txt": "the smoketree diffuses like pixels\n",
            "graphs/demo.yaml": _DEMO_GRAPH,
            "transformers/shout.yaml": _shell_transformer("shout", "upper"),
            "transformers/reverse.yaml": _shell_transformer("reverse", "reverse"),
            "transformers/wordstats.yaml": _shell_transformer("wordstats", "stats"),
            "scripts/textproc.py": _TEXTPROC,
        },
    ),
    "fanout": Template(
        "Collection fan-out: one execution per globbed file (expand: each).",
        {
            "smoketree.yaml": _CONFIG_MINIMAL,
            **_ITEMS,
            "graphs/fanout.yaml": _FANOUT_GRAPH,
            "transformers/shout.yaml": _shell_transformer("shout", "upper"),
            "scripts/textproc.py": _TEXTPROC,
        },
    ),
    "tagged": Template(
        "Tagged collection + filter_tag (multi-match fans out, single is scalar).",
        {
            "smoketree.yaml": _CONFIG_MINIMAL,
            **_ITEMS,
            "graphs/tagged.yaml": _TAGGED_GRAPH,
            "transformers/shout.yaml": _shell_transformer("shout", "upper"),
            "scripts/textproc.py": _TEXTPROC,
        },
    ),
    "portrait": Template(
        "Local-first AI: ollama describe -> ollama prompt -> comfyui txt2img/upscale.",
        {
            "smoketree.yaml": _CONFIG_AI,
            "sources/README.txt": _PORTRAIT_SOURCES_NOTE,
            "graphs/portrait.yaml": _PORTRAIT_GRAPH,
            "transformers/describe.yaml": _DESCRIBE_OLLAMA,
            "transformers/description_to_prompt.yaml": _PROMPT_OLLAMA,
            "transformers/txt2img.yaml": _TXT2IMG_YAML,
            "transformers/txt2img.json": _TXT2IMG_JSON,
            "transformers/upscale.yaml": _UPSCALE_YAML,
            "transformers/upscale.json": _UPSCALE_JSON,
        },
    ),
}

DEFAULT_TEMPLATE = "minimal"


def list_templates() -> dict[str, str]:
    """Template name -> one-line description, for ``smoketree init --list``."""
    return {name: tpl.description for name, tpl in TEMPLATES.items()}


def init_project(
    root: Path, name: str, template: str = DEFAULT_TEMPLATE, force: bool = False
) -> list[Path]:
    """Scaffold a project at ``root`` from ``template``. Returns created files."""
    if template not in TEMPLATES:
        raise SmoketreeError(
            f"Unknown template '{template}'. Available: {', '.join(TEMPLATES)}."
        )
    config_path = root / "smoketree.yaml"
    if config_path.exists() and not force:
        raise SmoketreeError(
            f"{config_path} already exists. Use --force to overwrite scaffolding."
        )

    files: dict[str, str] = {
        **TEMPLATES[template].files,
        "INSTRUCTIONS.md": _instructions(),
        ".gitignore": _GITIGNORE,
    }

    created: list[Path] = []
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.replace(_NAME_TOKEN, name))
        created.append(path)

    for rel in _STANDARD_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)

    return created
