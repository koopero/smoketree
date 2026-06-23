"""Data (de)serialization: YAML is the on-disk format; JSON stays on the wire.

``load_data`` parses YAML — a superset of JSON, so it reads either — and never enforces
a top-level mapping (data may be a list or scalar). ``dump_data`` writes YAML by default
and JSON only when the path's extension asks for it. ``write_structured`` takes the JSON
text an LLM returns under a schema constraint and writes it in the output's format.

The point: every artifact a human reads or edits stays YAML; JSON is confined to API
payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .errors import SmoketreeError

_JSON_SUFFIXES = {".json"}


def load_data(path: Path) -> Any:
    """Parse a data file (YAML or JSON) to a Python object."""
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise SmoketreeError(f"Failed to parse data file {path}: {exc}") from exc


def dump_data(obj: Any, path: Path) -> None:
    """Serialize ``obj`` to ``path``, YAML unless the extension is ``.json``."""
    if path.suffix.lower() in _JSON_SUFFIXES:
        text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    else:
        text = yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_structured(text: str, path: Path) -> None:
    """Parse JSON ``text`` (a schema-constrained LLM response) and write it to ``path``.

    The on-disk format follows ``path``'s extension, so a ``.yaml`` output lands as YAML
    even though the model returned JSON.
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SmoketreeError(
            f"Expected JSON matching the schema, got: {text[:200]!r} ({exc})."
        ) from exc
    dump_data(obj, path)
