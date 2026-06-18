"""Shared prompt assembly for LLM backends (Claude, Ollama).

Walks ``{inputs.NAME}`` references in a prompt template: text/data inputs are read and
inlined at their token; image inputs are removed from the text and returned separately
so each backend can attach them in its own wire format.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..cache import Artifact
from ..errors import ExecutionError

_TOKEN = re.compile(r"\{inputs\.([a-zA-Z0-9_]+)\}")


def build_prompt(
    template: str, inputs: "dict[str, Artifact | list[Artifact]]"
) -> tuple[str, list[Path]]:
    """Return the interpolated prompt text and the list of image input paths.

    An input may be a single artifact or a list (a grouped/multi-file input): a
    multi-image input attaches every image; multi text/data inputs are concatenated.
    """
    images: list[Path] = []

    def _one(artifact: Artifact) -> str:
        if artifact.media in ("text", "data"):
            return artifact.path.read_text()
        if artifact.media == "image":
            images.append(artifact.path)
            return ""
        raise ExecutionError(
            f"Cannot embed media type '{artifact.media}' in a prompt."
        )

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        value = inputs.get(name)
        if value is None:
            raise ExecutionError(f"Prompt references unknown input '{name}'.")
        artifacts = value if isinstance(value, list) else [value]
        return "\n\n".join(_one(a) for a in artifacts)

    return _TOKEN.sub(repl, template), images
