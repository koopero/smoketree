"""Enumerate the human-reviewable outputs of a pipeline and their feedback channels.

A rule that declares one or more ``feedback`` channels owns those channels, attached to
its output. The index pairs each such rule's rendered output (found by globbing its
primary output pattern on disk) with the current state of every channel — a ``notes`` log
(append-only text) or a ``select`` choice. No execution: it reads the last run's outputs,
so an unbuilt rule contributes nothing.
"""

from __future__ import annotations

import glob as globlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..bind import Pattern
from ..media import infer_media
from ..project import Project
from ..rules import load_pipeline

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


@dataclass
class FeedbackCard:
    """One reviewable output instance and its feedback channels."""

    id: str  # "<rule>:<key=val,...>"
    rule: str
    label: str
    keys: dict[str, str]
    media: str
    output_path: Path
    channels: list[ChannelView]

    @property
    def flagged(self) -> bool:
        """Whether the human has touched any channel (drives the card highlight)."""
        return any(
            (c.has_note if c.kind == "notes" else c.value != c.default)
            for c in self.channels
        )


def _slug(keys: dict[str, str]) -> str:
    return ",".join(f"{k}={keys[k]}" for k in sorted(keys))


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


def build_index(project: Project, pipeline_id: str) -> list[FeedbackCard]:
    """Index every reviewable output (a rule with ``feedback``) from the last run."""
    loaded = load_pipeline(project, pipeline_id)
    root = project.root
    cards: list[FeedbackCard] = []

    for rule in loaded.rules:
        if not rule.feedback or not rule.out or not rule.enabled:
            continue
        out_pat = Pattern.compile(next(iter(rule.out.values())))  # primary output

        for rel in sorted(globlib.glob(out_pat.glob_str, root_dir=str(root), recursive=True)):
            rel = rel.replace("\\", "/")
            m = out_pat.regex.match(rel)
            if not m:
                continue
            output_path = root / rel
            if not output_path.is_file():
                continue
            keys = m.groupdict()

            channels: list[ChannelView] = []
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
                channels.append(view)

            label = " · ".join(keys[k] for k in sorted(keys)) or rule.name
            cards.append(
                FeedbackCard(
                    id=f"{rule.name}:{_slug(keys)}",
                    rule=rule.name,
                    label=label,
                    keys=keys,
                    media=infer_media(output_path),
                    output_path=output_path,
                    channels=channels,
                )
            )
    return cards


def add_note(channel: ChannelView, text: str) -> bool:
    """Append a note to a ``notes`` channel (append-log semantics).

    The UI box is write-only: each submission *appends* a note (the channel accumulates
    notes a compile rule reads oldest-first). The first real note replaces the seed
    placeholder. An empty submission is a no-op. Returns whether the channel now holds
    real notes.
    """
    text = text.strip()
    if not text:
        return channel.has_note
    existing = channel.path.read_text().strip() if channel.path.exists() else ""
    if existing and existing != _SEED_PLACEHOLDER.strip():
        body = existing + "\n" + text + "\n"
    else:
        body = text + "\n"
    channel.path.parent.mkdir(parents=True, exist_ok=True)
    channel.path.write_text(body)
    return True


def set_select(channel: ChannelView, value: str) -> str:
    """Set a ``select`` channel's choice, preserving the describe/options comment header."""
    if value not in channel.options:
        raise ValueError(
            f"'{value}' is not an option for channel '{channel.name}' ({channel.options})."
        )
    lines: list[str] = []
    if channel.describe:
        lines.append(f"# {channel.describe}")
    lines.append(f"# options: {' | '.join(channel.options)}")
    lines.append(f"{channel.name}: {value}")
    channel.path.parent.mkdir(parents=True, exist_ok=True)
    channel.path.write_text("\n".join(lines) + "\n")
    return value
