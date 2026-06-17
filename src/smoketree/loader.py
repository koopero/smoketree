"""YAML loading with ``${ENV_VAR}`` substitution.

Substitution runs at load time across every string value in a parsed YAML document.
Shell environment takes precedence over a project-root ``.env`` file (loaded once at
project resolution time).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .errors import SmoketreeError

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.

    Shell environment takes precedence: existing keys are never overwritten.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def substitute_env(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` references in strings within a structure."""
    if isinstance(value, str):
        return _substitute_str(value)
    if isinstance(value, dict):
        return {k: substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_env(v) for v in value]
    return value


def _substitute_str(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in os.environ:
            raise SmoketreeError(
                f"Required environment variable '{var}' is not set "
                f"(referenced as ${{{var}}})."
            )
        return os.environ[var]

    return _ENV_PATTERN.sub(repl, value)


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and substitute environment variables."""
    if not path.exists():
        raise SmoketreeError(f"File not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough of parser detail
        raise SmoketreeError(f"Failed to parse YAML {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SmoketreeError(f"Expected a mapping at the top of {path}.")
    return substitute_env(data)
