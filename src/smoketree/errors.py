"""Smoketree error types.

``SmoketreeError`` is raised for any user-facing failure (bad config, validation
mismatch, execution failure). The CLI catches it and prints a clean message rather
than a traceback.
"""

from __future__ import annotations


class SmoketreeError(Exception):
    """A user-facing error. The CLI prints these without a traceback."""


class ValidationError(SmoketreeError):
    """Raised when a graph or transformer fails parse-time validation."""


class ExecutionError(SmoketreeError):
    """Raised when a transformer fails during execution."""
