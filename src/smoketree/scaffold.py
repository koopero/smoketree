"""Project scaffolding for ``smoketree init`` (PathTree core).

`init` creates a project from a chosen starter template (see ``TEMPLATES``). Every
project also gets the shared ``INSTRUCTIONS.md`` guide, a ``.gitignore``, and the
standard directory skeleton. The default template, ``minimal``, is the bare skeleton;
``demo`` is a runnable shell pipeline exercising scatter/map/pool.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from .errors import SmoketreeError

_NAME_TOKEN = "__PROJECT_NAME__"
_STANDARD_DIRS = ("graphs", "sources", "scripts")


def _instructions() -> str:
    return resources.files("smoketree").joinpath("templates", "INSTRUCTIONS.md").read_text()


_GITIGNORE = """\
.smoketree/
.env
work/
report.txt
__pycache__/
"""

_CONFIG = f"""\
name: {_NAME_TOKEN}

defaults:
  max_iterations: 100
"""

# --------------------------------------------------------------------------- #
# demo: media-breakdown-lite — scatter -> map -> pool -> pool
# --------------------------------------------------------------------------- #

_DEMO_PIPELINE = """\
name: demo

# A path-based pipeline. The DAG is inferred from how output patterns feed input
# patterns; fan-out comes from the {key} axes discovered by globbing the tree.
# Quote any pattern containing {braces} so YAML doesn't read it as a flow mapping.
rules:
  # scatter: split each episode's lines into one segment dir per line. The {segment}
  # key is introduced by the output and discovered at runtime, so {segments} resolves
  # to the owned directory work/episode/{episode}/segment/. prune drops vanished segments.
  - name: split
    in:
      lines: "sources/episode/{episode}/lines.txt"
    out:
      segments: "work/episode/{episode}/segment/{segment}/line.txt"
    run: "python scripts/split.py {lines} {segments}"
    prune: true

  # map: one job per (episode, segment) — shout each line.
  - name: shout
    in:
      line: "work/episode/{episode}/segment/{segment}/line.txt"
    out:
      loud: "work/episode/{episode}/segment/{segment}/loud.txt"
    run: "tr a-z A-Z < {line} > {loud}"

  # pool: collapse the {segment} axis (glob it) into one summary per episode.
  - name: summary
    in:
      parts: "work/episode/{episode}/segment/*/loud.txt"
    out:
      summary: "work/episode/{episode}/summary.txt"
    run: "cat {parts} > {summary}"

  # pool again: collapse {episode} into a single season report.
  - name: report
    in:
      summaries: "work/episode/*/summary.txt"
    out:
      report: "report.txt"
    run: "cat {summaries} > {report}"
"""

_SPLIT_PY = '''\
"""Split a file into one segment directory per non-empty line."""

import sys
from pathlib import Path


def main() -> None:
    infile, outdir = sys.argv[1], Path(sys.argv[2])
    lines = [ln for ln in Path(infile).read_text().splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        seg = outdir / f"{i:02d}"
        seg.mkdir(parents=True, exist_ok=True)
        (seg / "line.txt").write_text(line + "\\n")


if __name__ == "__main__":
    main()
'''

_EP01 = "the smoketree diffuses like pixels\na kaiju wakes over the harbor\nthe city holds its breath\n"
_EP02 = "dawn light on wet rooftops\na lone figure crosses the bridge\n"


class Template:
    def __init__(self, description: str, files: dict[str, str]):
        self.description = description
        self.files = files


TEMPLATES: dict[str, Template] = {
    "minimal": Template(
        "Bare skeleton — just smoketree.yaml and empty dirs.",
        {"smoketree.yaml": _CONFIG},
    ),
    "demo": Template(
        "Offline shell pipeline: scatter -> map -> pool -> pool (no API keys).",
        {
            "smoketree.yaml": _CONFIG,
            "graphs/demo.yaml": _DEMO_PIPELINE,
            "scripts/split.py": _SPLIT_PY,
            "sources/episode/ep01/lines.txt": _EP01,
            "sources/episode/ep02/lines.txt": _EP02,
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
