"""Execution backends, dispatched by a rule's ``backend`` name.

``shell``, ``ollama``, and ``replicate`` are ported onto the PathTree
``ExecutionContext``. ``claude``/``comfyui`` remain on disk, dormant, until ported.
"""

from __future__ import annotations

from ..errors import ExecutionError
from .base import Backend, ExecutionContext
from .ollama import OllamaBackend
from .replicate import ReplicateBackend
from .shell import ShellBackend

_BACKENDS: dict[str, type[Backend]] = {
    "shell": ShellBackend,
    "ollama": OllamaBackend,
    "replicate": ReplicateBackend,
}


def get_backend(name: str) -> Backend:
    backend_cls = _BACKENDS.get(name)
    if backend_cls is None:
        raise ExecutionError(
            f"Backend '{name}' is not available in this build. "
            f"Available: {', '.join(sorted(_BACKENDS))}."
        )
    return backend_cls()


__all__ = ["Backend", "ExecutionContext", "get_backend"]
