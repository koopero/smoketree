"""Web-facing mutations the workspace performs on the project tree.

Kept separate from the FastAPI wiring so the logic is unit-testable without a server.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..bind import Pattern
from ..errors import SmoketreeError
from ..models import Rule
from ..project import Project


def _round_id() -> str:
    """A fresh, sortable round id for a new trigger marker (e.g. ``r-20260623-224701``)."""
    return datetime.now(timezone.utc).strftime("r-%Y%m%d-%H%M%S")


def fire_trigger(project: Project, rule: Rule, *, round_id: str | None = None) -> Path:
    """Write the next round marker for a ``trigger``-bearing rule; return its path.

    Every key in the marker pattern is filled with one fresh round id, so the rule's input
    glob discovers a new binding on the next run (the "generate more" step of a brainstorm
    loop). Refuses to write outside the project or to clobber an existing marker.
    """
    if rule.trigger is None:
        raise SmoketreeError(f"Rule '{rule.name}' has no trigger.")
    rid = round_id or _round_id()
    pattern = Pattern.compile(rule.trigger.marker)
    rel = pattern.fill({k: rid for k in pattern.keys})
    path = (project.root / rel).resolve()
    if not str(path).startswith(str(project.root.resolve())):
        raise SmoketreeError(f"Trigger marker '{rel}' resolves outside the project.")
    if path.exists():
        raise SmoketreeError(f"Trigger marker '{rel}' already exists.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rule.trigger.content)
    return path
