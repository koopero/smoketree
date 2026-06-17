"""Transformer execution backends, dispatched by transformer ``type``."""

from __future__ import annotations

from ..errors import ExecutionError
from ..models import Transformer
from .base import Backend, ExecutionContext
from .claude import ClaudeBackend
from .comfyui import ComfyUIBackend
from .ollama import OllamaBackend
from .shell import ShellBackend

_BACKENDS: dict[str, type[Backend]] = {
    "shell": ShellBackend,
    "claude": ClaudeBackend,
    "ollama": OllamaBackend,
    "comfyui": ComfyUIBackend,
}


def get_backend(transformer: Transformer) -> Backend:
    backend_cls = _BACKENDS.get(transformer.type)
    if backend_cls is None:
        raise ExecutionError(
            f"Transformer type '{transformer.type}' is not yet implemented."
        )
    return backend_cls()


__all__ = ["Backend", "ExecutionContext", "get_backend"]
