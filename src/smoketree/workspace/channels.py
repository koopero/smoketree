"""Reading the current state of a rule's feedback channels for one key-tuple.

Shared by the feedback index ([index.py]) and the artifact graph ([graph.py]) so both
read ``notes``/``select`` channel files the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..bind import Pattern

_SEED_PLACEHOLDER = "(no feedback yet)\n"


@dataclass
class ChannelView:
    """The current state of one feedback channel for one output instance."""

    name: str
    kind: str                       # "notes" | "select"
    describe: str | None
    path: Path
    # notes:
    has_note: bool = False
    # select:
    options: list[str] = field(default_factory=list)
    value: str | None = None        # current selection
    default: str | None = None


def _notes_has_content(path: Path) -> bool:
    if not path.exists():
        return False
    stripped = path.read_text().strip()
    return bool(stripped) and stripped != _SEED_PLACEHOLDER.strip()


def _select_value(path: Path, channel) -> str | None:
    """Read the current selection from a select channel's file (falling back to default)."""
    if not path.exists():
        return channel.default
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    if isinstance(data, dict):
        return data.get(channel.name, channel.default)
    return data if isinstance(data, str) else channel.default


def read_channels(root: Path, rule, keys: dict[str, str]) -> list[ChannelView]:
    """Resolve every feedback channel of ``rule`` for one key-tuple to its current state."""
    views: list[ChannelView] = []
    for ch in rule.feedback:
        path = root / Pattern.compile(ch.path).fill(keys)
        view = ChannelView(
            name=ch.name, kind=ch.kind, describe=ch.describe, path=path,
            options=ch.options, default=ch.default,
        )
        if ch.kind == "select":
            view.value = _select_value(path, ch)
        else:
            view.has_note = _notes_has_content(path)
        views.append(view)
    return views
