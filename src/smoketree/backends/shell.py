"""Shell transformer backend: run an arbitrary command with interpolation + env."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..errors import ExecutionError
from ..models import ShellTransformer
from .base import Backend, ExecutionContext

_TOKEN = re.compile(r"\{([a-z_]+(?:\.[a-zA-Z0-9_]+)?)\}")


def _interpolate(template: str, mapping: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in mapping:
            raise ExecutionError(f"Unknown template variable '{{{key}}}' in command.")
        return mapping[key]

    return _TOKEN.sub(repl, template)


class ShellBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> dict[str, Path]:
        transformer = ctx.transformer
        assert isinstance(transformer, ShellTransformer)

        mapping: dict[str, str] = {
            "dirs.scratch": str(ctx.scratch_dir),
            "dirs.output": str(ctx.output_dir),
            "seed": str(ctx.seed),
            "take": str(ctx.take),
            "node_id": ctx.node_id,
            "graph_id": ctx.graph_id,
        }
        for name, value in ctx.inputs.items():
            # a grouped (multi-file) input expands to its space-separated paths
            artifacts = value if isinstance(value, list) else [value]
            mapping[f"inputs.{name}"] = " ".join(str(a.path) for a in artifacts)
        for name, target in ctx.output_targets.items():
            mapping[f"outputs.{name}"] = str(target)

        command = _interpolate(transformer.command, mapping)

        env = os.environ.copy()
        # Project env was already merged into os.environ-style precedence by the
        # executor's project config; transformer env wins over project env here.
        env.update(transformer.env)
        env.update(
            {
                "SMOKETREE_SCRATCH": str(ctx.scratch_dir),
                "SMOKETREE_OUTPUT": str(ctx.output_dir),
                "SMOKETREE_SEED": str(ctx.seed),
                "SMOKETREE_TAKE": str(ctx.take),
                "SMOKETREE_NODE_ID": ctx.node_id,
                "SMOKETREE_GRAPH_ID": ctx.graph_id,
            }
        )

        result = subprocess.run(
            command,
            shell=True,
            cwd=ctx.project.root,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ExecutionError(
                f"Shell command failed (exit {result.returncode}):\n"
                f"  $ {command}\n"
                f"{_tail(result.stdout, 'stdout')}"
                f"{_tail(result.stderr, 'stderr')}"
            )

        # The script is expected to write to each declared output target.
        return dict(ctx.output_targets)


def _tail(text: str, label: str, lines: int = 20) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return f"--- {label} (tail) ---\n{tail}\n"
