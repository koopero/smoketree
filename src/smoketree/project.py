"""Project discovery and loading.

A Smoketree project is a directory containing ``smoketree.yaml``. The project owns the
config, the ``.smoketree/`` cache/scratch/state tree, and the ``graphs/`` and
``transformers/`` definition directories.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from .errors import SmoketreeError
from .loader import load_dotenv, load_yaml
from .models import ProjectConfig, Transformer
from pydantic import TypeAdapter

CONFIG_FILENAME = "smoketree.yaml"

_transformer_adapter: TypeAdapter[Transformer] = TypeAdapter(Transformer)


def find_project_root(start: Path | None = None) -> Path:
    """Walk upward from ``start`` (default cwd) to find ``smoketree.yaml``."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILENAME).exists():
            return candidate
    raise SmoketreeError(
        f"No {CONFIG_FILENAME} found in {current} or any parent directory. "
        "Run 'smoketree init' to create a project."
    )


class Project:
    """A loaded Smoketree project rooted at ``root``."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        # Load .env before any env substitution happens.
        load_dotenv(self.root / ".env")
        self.config = self._load_config()

    @classmethod
    def discover(cls, start: Path | None = None) -> "Project":
        return cls(find_project_root(start))

    def _load_config(self) -> ProjectConfig:
        data = load_yaml(self.root / CONFIG_FILENAME)
        try:
            return ProjectConfig.model_validate(data)
        except PydanticValidationError as exc:
            raise SmoketreeError(f"Invalid {CONFIG_FILENAME}:\n{exc}") from exc

    # ----- directory layout ------------------------------------------------ #

    @property
    def graphs_dir(self) -> Path:
        return self.root / "graphs"

    @property
    def transformers_dir(self) -> Path:
        return self.root / "transformers"

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def smoketree_dir(self) -> Path:
        return self.root / ".smoketree"

    @property
    def state_dir(self) -> Path:
        return self.smoketree_dir / "state"

    # ----- definition loading ---------------------------------------------- #

    def graph_path(self, graph_id: str) -> Path:
        path = self.graphs_dir / f"{graph_id}.yaml"
        if not path.exists():
            raise SmoketreeError(f"Graph '{graph_id}' not found at {path}.")
        return path

    def transformer_path(self, name: str) -> Path:
        path = self.transformers_dir / f"{name}.yaml"
        if not path.exists():
            raise SmoketreeError(
                f"Transformer '{name}' not found at {path}."
            )
        return path

    @cached_property
    def _transformer_cache(self) -> dict[str, Transformer]:
        return {}

    def load_transformer(self, name: str) -> Transformer:
        if name in self._transformer_cache:
            return self._transformer_cache[name]
        path = self.transformer_path(name)
        data = load_yaml(path)
        try:
            transformer = _transformer_adapter.validate_python(data)
        except PydanticValidationError as exc:
            raise SmoketreeError(
                f"Invalid transformer '{name}' ({path}):\n{exc}"
            ) from exc
        if transformer.name != name:
            raise SmoketreeError(
                f"Transformer file '{path.name}' declares name "
                f"'{transformer.name}'; expected '{name}'."
            )
        self._transformer_cache[name] = transformer
        return transformer

    def list_graphs(self) -> list[str]:
        if not self.graphs_dir.exists():
            return []
        return sorted(p.stem for p in self.graphs_dir.glob("*.yaml"))
