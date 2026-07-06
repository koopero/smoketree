"""Execution backends, dispatched by a rule's ``backend`` name.

All backends — ``shell``, ``ollama``, ``replicate``, ``claude``, ``openai``,
``openai_image``, ``comfyui``, ``explode``, and ``blender`` — are ported onto the PathTree
``ExecutionContext`` and read their settings from the rule's ``config`` block (except
``shell``, which uses the rendered ``run`` command).
"""

from __future__ import annotations

from ..errors import ExecutionError
from .base import Backend, ExecutionContext
from .blender import BlenderBackend
from .claude import ClaudeBackend
from .comfyui import ComfyUIBackend
from .explode import ExplodeBackend
from .ollama import OllamaBackend
from .openai import OpenAIBackend
from .openai_image import OpenAIImageBackend
from .replicate import ReplicateBackend
from .shell import ShellBackend

_BACKENDS: dict[str, type[Backend]] = {
    "shell": ShellBackend,
    "ollama": OllamaBackend,
    "replicate": ReplicateBackend,
    "claude": ClaudeBackend,
    "openai": OpenAIBackend,
    "openai_image": OpenAIImageBackend,
    "comfyui": ComfyUIBackend,
    "explode": ExplodeBackend,
    "blender": BlenderBackend,
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
