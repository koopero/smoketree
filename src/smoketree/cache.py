"""Content hashing, cache keys, deterministic paths, and the state file.

Cache key is the SHA-256 of:
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


def instance_hash(key: dict[str, str]) -> str:
    """Short, readable hash of an expanded node's instance key (input paths).

    The key is the mapping of input name -> producing artifact path that uniquely
    identifies one execution within a fanned-out node.
    """
    h = hashlib.sha256()
    for name in sorted(key):
        h.update(f"{name}={key[name]}\x00".encode())
    return h.hexdigest()[:12]


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


def _instance_dir(base: Path, node_id: str, inst_hash: str | None, take: int) -> Path:
    """Per-execution dir. Fanned-out nodes nest under their instance hash; plain
    (single-instance) nodes use the flat ``take_{n}`` layout."""
    node_dir = base / node_id
    if inst_hash is not None:
        node_dir = node_dir / inst_hash
    return node_dir / f"take_{take}"


def cache_instance_dir(
    project: Project, graph_id: str, node_id: str, inst_hash: str | None, take: int
) -> Path:
    return _instance_dir(project.cache_dir / graph_id, node_id, inst_hash, take)


def scratch_instance_dir(
    project: Project, graph_id: str, node_id: str, inst_hash: str | None, take: int
) -> Path:
    return _instance_dir(project.scratch_dir / graph_id, node_id, inst_hash, take)


def materialize_dir(
    project: Project, graph_id: str, node_id: str, inst_hash: str | None
) -> Path:
    """User-owned, take-independent home for a `materialize` node's artifact."""
    node_dir = project.scenes_dir / graph_id / node_id
    return node_dir / inst_hash if inst_hash is not None else node_dir


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #


@dataclass
class NodeState:
    input_hash: str
    take: int
    completed_at: str


class State:
    """The per-graph state file, recording last-successful input hashes.

    State is keyed per node *and per instance* — a fanned-out node has one entry
    per expanded execution. Plain nodes have a single instance entry.
    """

    def __init__(self, project: Project, graph_id: str):
        self.project = project
        self.graph_id = graph_id
        # node_id -> instance_hash -> NodeState
        self.nodes: dict[str, dict[str, NodeState]] = {}

    @property
    def path(self) -> Path:
        return self.project.state_dir / f"{self.graph_id}.json"

    @classmethod
    def load(cls, project: Project, graph_id: str) -> "State":
        state = cls(project, graph_id)
        if state.path.exists():
            data = json.loads(state.path.read_text())
            for node_id, raw in data.get("nodes", {}).items():
                instances = raw.get("instances", {})
                state.nodes[node_id] = {
                    inst: NodeState(
                        input_hash=ns["input_hash"],
                        take=ns.get("take", 0),
                        completed_at=ns.get("completed_at", ""),
                    )
                    for inst, ns in instances.items()
                }
        return state

    def get(self, node_id: str, inst_hash: str) -> NodeState | None:
        return self.nodes.get(node_id, {}).get(inst_hash)

    def record(
        self, node_id: str, inst_hash: str, input_hash: str, take: int
    ) -> None:
        self.nodes.setdefault(node_id, {})[inst_hash] = NodeState(
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
                    "instances": {
                        inst: {
                            "input_hash": ns.input_hash,
                            "take": ns.take,
                            "completed_at": ns.completed_at,
                        }
                        for inst, ns in instances.items()
                    }
                }
                for node_id, instances in self.nodes.items()
            }
        }
        self.path.write_text(json.dumps(data, indent=2) + "\n")
