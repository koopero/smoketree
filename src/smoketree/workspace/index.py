"""Enumerate the human-reviewable outputs of a pipeline.

A rule that declares ``feedback.append`` owns a feedback channel attached to its output.
The index pairs each such rule's rendered output (found by globbing its primary output
pattern on disk) with that channel's note file, producing the cards the workspace shows.
No execution — it reads the last run's outputs, so an unbuilt rule contributes nothing.
"""

from __future__ import annotations

import glob as globlib
from dataclasses import dataclass, field
from pathlib import Path

from ..bind import Pattern
from ..media import infer_media
from ..project import Project
from ..rules import load_pipeline

_SEED_PLACEHOLDER = "(no feedback yet)\n"


@dataclass
class FeedbackCard:
    """One reviewable output instance and its feedback channel."""

    id: str  # "<rule>:<key=val,...>"
    rule: str
    label: str
    keys: dict[str, str]
    media: str
    output_path: Path
    note_path: Path
    note_text: str = ""

    @property
    def has_note(self) -> bool:
        stripped = self.note_text.strip()
        return bool(stripped) and stripped != _SEED_PLACEHOLDER.strip()


def _slug(keys: dict[str, str]) -> str:
    return ",".join(f"{k}={keys[k]}" for k in sorted(keys))


def build_index(project: Project, pipeline_id: str) -> list[FeedbackCard]:
    """Index every reviewable output (a rule with ``feedback.append``) from the last run."""
    loaded = load_pipeline(project, pipeline_id)
    root = project.root
    cards: list[FeedbackCard] = []

    for rule in loaded.rules:
        if rule.feedback is None or not rule.out or not rule.enabled:
            continue
        out_pat = Pattern.compile(next(iter(rule.out.values())))  # primary output
        fb_pat = Pattern.compile(rule.feedback.append)

        for rel in sorted(globlib.glob(out_pat.glob_str, root_dir=str(root), recursive=True)):
            rel = rel.replace("\\", "/")
            m = out_pat.regex.match(rel)
            if not m:
                continue
            output_path = root / rel
            if not output_path.is_file():
                continue
            keys = m.groupdict()
            note_path = root / fb_pat.fill(keys)
            note_text = note_path.read_text() if note_path.exists() else ""
            label = " · ".join(keys[k] for k in sorted(keys)) or rule.name
            cards.append(
                FeedbackCard(
                    id=f"{rule.name}:{_slug(keys)}",
                    rule=rule.name,
                    label=label,
                    keys=keys,
                    media=infer_media(output_path),
                    output_path=output_path,
                    note_path=note_path,
                    note_text=note_text,
                )
            )
    return cards


def add_note(card: FeedbackCard, text: str) -> bool:
    """Append a note to the card's feedback channel (append-log semantics).

    The UI box is write-only: each submission *appends* a note (the channel accumulates
    notes the compile rule reads oldest-first). The first real note replaces the seed
    placeholder. An empty submission is a no-op — the channel is a pipeline input, never
    emptied. Returns whether the channel now holds real notes.
    """
    text = text.strip()
    if not text:
        return card.has_note
    existing = card.note_path.read_text().strip() if card.note_path.exists() else ""
    if existing and existing != _SEED_PLACEHOLDER.strip():
        body = existing + "\n" + text + "\n"
    else:
        body = text + "\n"
    card.note_path.parent.mkdir(parents=True, exist_ok=True)
    card.note_path.write_text(body)
    return True
