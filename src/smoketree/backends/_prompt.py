"""Shared prompt assembly for LLM backends on the path core.

A prompt/system template references inputs and keys by ``{name}``: text/data inputs are
read and inlined at their token; image inputs are dropped from the text and returned
separately so each backend attaches them in its own wire format; a ``{key}`` substitutes
its value.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import ExecutionError
from ..media import infer_media

_TOKEN = re.compile(r"\{(\w+)\}")


def render_prompt(
    template: str,
    inputs: "dict[str, Path | list[Path]]",
    keys: dict[str, str],
) -> tuple[str, list[Path]]:
    """Return the interpolated text and the list of image input paths it referenced."""
    images: list[Path] = []

    def _one(path: Path) -> str:
        media = infer_media(path)
        if media in ("text", "data"):
            return path.read_text()
        if media == "image":
            images.append(path)
            return ""
        raise ExecutionError(f"Cannot embed media '{media}' ({path}) in a prompt.")

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in inputs:
            value = inputs[name]
            paths = value if isinstance(value, list) else [value]
            return "\n\n".join(_one(p) for p in paths)
        if name in keys:
            return keys[name]
        raise ExecutionError(
            f"Prompt references unknown input/key '{{{name}}}'. "
            f"Inputs: {', '.join(inputs) or '(none)'}; keys: {', '.join(keys) or '(none)'}."
        )

    return _TOKEN.sub(repl, template), images
