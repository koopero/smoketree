"""Shared prompt assembly for LLM backends on the path core.

Prompt/system templates are **Jinja2**, rendered over the rule's upstream data. Each
input/context port is exposed by name as parsed, structured data — so a template can pick
fields, filter, reshape, and format rather than dumping raw file text:

- a **data** file (json/yaml) → its parsed object (dict/list): ``{{ band.name }}``,
  ``{% for b in others %}…{% endfor %}``, ``{{ others | map(attribute='name') | join(', ') }}``;
- a **text** file (txt/md) → its string;
- an **image** file → auto-attached to the backend (returned separately) and rendered as ``""``;
- a **list/glob** port (e.g. a ``*`` context) → a list of the above (images excluded).

Every ``{key}`` path axis is also a string variable. Filters: built-in ``tojson`` plus
``to_yaml`` (for human-readable inline of a parsed object) and ``slug``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from ..errors import ExecutionError
from ..media import infer_media
from ..serde import load_data


def _to_yaml(value: Any) -> str:
    """Compact YAML for inlining a parsed object back into a prompt."""
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).rstrip("\n")


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


_ENV = SandboxedEnvironment(
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_ENV.filters["to_yaml"] = _to_yaml
_ENV.filters["slug"] = _slug


def render_prompt(
    template: str,
    inputs: "dict[str, Path | list[Path]]",
    keys: dict[str, str],
) -> tuple[str, list[Path]]:
    """Render a Jinja2 prompt over parsed upstream data.

    Returns the rendered text and the list of image input paths to attach (every
    image-typed input/context file is attached, regardless of whether the template
    references it — images can't be inlined as text).
    """
    images: list[Path] = []
    _OMIT = object()  # an image: attached, contributes nothing to the text

    def _expose(path: Path) -> Any:
        media = infer_media(path)
        if media == "data":
            return load_data(path)
        if media == "text":
            return path.read_text()
        if media == "image":
            images.append(path)
            return _OMIT
        raise ExecutionError(f"Cannot embed media '{media}' ({path}) in a prompt.")

    context: dict[str, Any] = dict(keys)
    for name, value in inputs.items():
        if isinstance(value, list):
            context[name] = [v for v in (_expose(p) for p in value) if v is not _OMIT]
        else:
            exposed = _expose(value)
            context[name] = "" if exposed is _OMIT else exposed

    try:
        rendered = _ENV.from_string(template).render(context)
    except TemplateError as exc:
        raise ExecutionError(
            f"Prompt template error: {exc}. "
            f"Available: {', '.join(sorted(context)) or '(none)'}."
        ) from exc
    return rendered, images
