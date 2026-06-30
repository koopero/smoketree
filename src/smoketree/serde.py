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


def _is_empty(value: Any) -> bool:
    """Whether ``value`` carries no content (blank string, None, or an all-empty container)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return all(_is_empty(v) for v in value.values())
    if isinstance(value, list):
        return all(_is_empty(v) for v in value)
    return False  # numbers / booleans carry content


def prune_empty(value: Any) -> Any:
    """Drop wholly-empty items from arrays (recursively).

    Constrained-decoding LLM backends can pad an array with a trailing all-blank item the
    model couldn't fill (it can't enforce item counts, and string `minLength` is only a
    hint). Such items carry no content and would fail schema validation, so we strip them —
    leaving partially-filled items intact (a real quality problem worth surfacing, not hiding).
    """
    if isinstance(value, dict):
        return {k: prune_empty(v) for k, v in value.items()}
    if isinstance(value, list):
        return [prune_empty(v) for v in value if not _is_empty(v)]
    return value


def write_structured(text: str, path: Path) -> None:
    """Parse JSON ``text`` (a schema-constrained LLM response) and write it to ``path``.

    The on-disk format follows ``path``'s extension, so a ``.yaml`` output lands as YAML
    even though the model returned JSON. Wholly-empty trailing array items (a constrained-
    decoding artifact) are pruned before writing.
    """
    try:
        # Constrained decoding occasionally appends trailing whitespace or a second object
        # after the JSON value (seen with some local models). Decode the first complete
        # value and ignore any trailing content rather than failing the whole job.
        obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except json.JSONDecodeError as exc:
        raise SmoketreeError(
            f"Expected JSON matching the schema, got: {text[:200]!r} ({exc})."
        ) from exc
    dump_data(prune_empty(obj), path)
