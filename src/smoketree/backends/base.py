"""Backend interface and the per-job context passed to it (PathTree core).

The engine resolves every path; a backend receives a fully-rendered command and the
concrete input/output paths and just runs the transform, writing to ``ctx.outputs``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..project import Project


@dataclass
class ExecutionContext:
    """Everything a backend needs to execute one binding (one rule, one key-tuple)."""

    project: Project
    rule_name: str
    keys: dict[str, str]
    # input name -> a single resolved Path (scalar) or list[Path] (list/pool input)
    inputs: "dict[str, Path | list[Path]]"
    outputs: dict[str, Path]  # output name -> concrete path, or an owned dir for scatter
    # shell backend: the fully rendered command (None for non-shell backends)
    command: str | None = None
    # non-shell backends: the rule's `config` block (model, prompt, params, ...)
    config: dict[str, Any] = field(default_factory=dict)
    # port name -> resolved JSON Schema dict, for ports that declare one. An LLM backend
    # uses its output port's schema to constrain generation.
    schemas: dict[str, dict] = field(default_factory=dict)
    # deterministic per-job seed (from the binding identity)
    seed: int = 0
    env: dict[str, str] = field(default_factory=dict)


class Backend(ABC):
    """A transform execution backend."""

    @abstractmethod
    def execute(self, ctx: ExecutionContext) -> None:
        """Run the transform, writing artifacts to ``ctx.outputs``."""
        raise NotImplementedError
