"""Content hashing and the per-pipeline state file.

Staleness is content-addressed: a job's *input hash* is the SHA-256 of its resolved
input file contents plus its rendered command. State maps each job's identity
(rule name + key-tuple) to the input hash recorded at its last successful run, plus a
cheap *fingerprint* (mtime+size of each input) that gates the content hash: when the
fingerprint is unchanged, the inputs are assumed unchanged and the file contents are
not re-read. The content hash remains the machine-independent source of truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .project import Project


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #


@dataclass
class JobState:
    input_hash: str
    completed_at: str
    # cheap mtime+size fingerprint of the inputs at record time; gates the content hash
    fingerprint: str = ""
    # the binding's managed outputs at record time — kept so a binding that stops
    # enumerating entirely (a required input vanished) can still have its previously
    # produced outputs cleaned up, the same way a `filter` mismatch does.
    enumerable_outputs: list[str] = field(default_factory=list)
    owned_prefixes: list[str] = field(default_factory=list)


class State:
    """The per-pipeline state file, recording the last-successful input hash per job."""

    def __init__(self, project: Project, pipeline_id: str):
        self.project = project
        self.pipeline_id = pipeline_id
        self.jobs: dict[str, JobState] = {}  # identity -> JobState

    @property
    def path(self) -> Path:
        return self.project.state_dir / f"{self.pipeline_id}.json"

    @classmethod
    def load(cls, project: Project, pipeline_id: str) -> "State":
        state = cls(project, pipeline_id)
        if state.path.exists():
            data = json.loads(state.path.read_text())
            for identity, raw in data.get("jobs", {}).items():
                state.jobs[identity] = JobState(
                    input_hash=raw["input_hash"],
                    completed_at=raw.get("completed_at", ""),
                    fingerprint=raw.get("fingerprint", ""),
                    enumerable_outputs=raw.get("enumerable_outputs", []),
                    owned_prefixes=raw.get("owned_prefixes", []),
                )
        return state

    def get(self, identity: str) -> JobState | None:
        return self.jobs.get(identity)

    def record(
        self,
        identity: str,
        input_hash: str,
        fingerprint: str = "",
        enumerable_outputs: list[str] | None = None,
        owned_prefixes: list[str] | None = None,
    ) -> None:
        self.jobs[identity] = JobState(
            input_hash=input_hash,
            completed_at=datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            fingerprint=fingerprint,
            enumerable_outputs=enumerable_outputs or [],
            owned_prefixes=owned_prefixes or [],
        )

    def touch_fingerprint(self, identity: str, fingerprint: str) -> None:
        """Refresh only the fingerprint of an existing record (content confirmed equal)."""
        job = self.jobs.get(identity)
        if job is not None:
            job.fingerprint = fingerprint

    def discard(self, identity: str) -> None:
        """Forget a job's record (e.g. its output was dropped by a filter)."""
        self.jobs.pop(identity, None)

    def clear(self) -> None:
        self.jobs.clear()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "jobs": {
                identity: {
                    "input_hash": js.input_hash,
                    "completed_at": js.completed_at,
                    "fingerprint": js.fingerprint,
                    "enumerable_outputs": js.enumerable_outputs,
                    "owned_prefixes": js.owned_prefixes,
                }
                for identity, js in sorted(self.jobs.items())
            }
        }
        self.path.write_text(json.dumps(data, indent=2) + "\n")
