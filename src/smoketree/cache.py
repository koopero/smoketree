"""Content hashing, cache keys, deterministic paths, and the state file.

Cache key (per DESIGN.md) is the SHA-256 of:
  1. the content hash of each input artifact (recursively resolved via file content)
  2. the full text of the transformer YAML
  3. for ComfyUI: the full text of the workflow JSON
  4. the take index
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import MediaType
from .project import Project


@dataclass
class Artifact:
    """A resolved artifact on disk."""

    path: Path
    media: MediaType
    format: str | None
    content_hash: str


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_seed(graph_id: str, node_id: str, take: int) -> int:
    digest = hashlib.sha256(f"{graph_id}:{node_id}:{take}".encode()).hexdigest()
    return int(digest, 16) % (2**32)


def compute_cache_key(
    input_hashes: dict[str, str],
    transformer_text: str,
    workflow_text: str | None,
    take: int,
) -> str:
    h = hashlib.sha256()
    # Sort inputs by name for a stable key regardless of declaration order.
    for name in sorted(input_hashes):
        h.update(f"{name}={input_hashes[name]}".encode())
    h.update(b"\x00transformer\x00")
    h.update(transformer_text.encode("utf-8"))
    if workflow_text is not None:
        h.update(b"\x00workflow\x00")
        h.update(workflow_text.encode("utf-8"))
    h.update(f"\x00take={take}".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Deterministic paths
# --------------------------------------------------------------------------- #


def cache_node_dir(project: Project, graph_id: str, node_id: str, take: int) -> Path:
    return project.cache_dir / graph_id / node_id / f"take_{take}"


def scratch_node_dir(project: Project, graph_id: str, node_id: str, take: int) -> Path:
    return project.scratch_dir / graph_id / node_id / f"take_{take}"


def output_node_dir(project: Project, graph_id: str, node_id: str, take: int) -> Path:
    """Per-execution output dir, exposed to transformers as ``{dirs.output}``."""
    return cache_node_dir(project, graph_id, node_id, take)


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #


@dataclass
class NodeState:
    input_hash: str
    take: int
    completed_at: str


class State:
    """The per-graph state file, recording last-successful input hashes."""

    def __init__(self, project: Project, graph_id: str):
        self.project = project
        self.graph_id = graph_id
        self.nodes: dict[str, NodeState] = {}

    @property
    def path(self) -> Path:
        return self.project.state_dir / f"{self.graph_id}.json"

    @classmethod
    def load(cls, project: Project, graph_id: str) -> "State":
        state = cls(project, graph_id)
        if state.path.exists():
            data = json.loads(state.path.read_text())
            for node_id, raw in data.get("nodes", {}).items():
                state.nodes[node_id] = NodeState(
                    input_hash=raw["input_hash"],
                    take=raw.get("take", 0),
                    completed_at=raw.get("completed_at", ""),
                )
        return state

    def record(self, node_id: str, input_hash: str, take: int) -> None:
        self.nodes[node_id] = NodeState(
            input_hash=input_hash,
            take=take,
            completed_at=datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "nodes": {
                node_id: {
                    "input_hash": ns.input_hash,
                    "take": ns.take,
                    "completed_at": ns.completed_at,
                }
                for node_id, ns in self.nodes.items()
            }
        }
        self.path.write_text(json.dumps(data, indent=2) + "\n")
