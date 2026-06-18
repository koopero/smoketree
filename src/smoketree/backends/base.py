"""Backend interface and the per-execution context passed to it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..cache import Artifact
from ..models import Transformer
from ..project import Project


@dataclass
class ExecutionContext:
    """Everything a backend needs to execute one node, one take."""

    project: Project
    graph_id: str
    node_id: str
    transformer: Transformer
    # input_name -> resolved upstream artifact, or a list of artifacts for a grouped
    # (multi-file) collection input
    inputs: "dict[str, Artifact | list[Artifact]]"
    output_targets: dict[str, Path]  # output_name -> target path (in cache/output dir)
    scratch_dir: Path
    output_dir: Path
    seed: int
    take: int


class Backend(ABC):
    """A transformer execution backend."""

    @abstractmethod
    def execute(self, ctx: ExecutionContext) -> dict[str, Path]:
        """Run the transformation, writing artifacts to ``ctx.output_targets``.

        Returns a mapping of output name -> produced file path. The returned path
        may differ from the declared target (e.g. a different extension), which the
        executor reconciles and may warn about.
        """
        raise NotImplementedError
