"""Shell backend: run the engine-rendered command in the project root."""

from __future__ import annotations

import os
import subprocess

from ..errors import ExecutionError
from .base import Backend, ExecutionContext


class ShellBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        env = os.environ.copy()
        env.update(ctx.env)
        env["SMOKETREE_ROOT"] = str(ctx.project.root)
        env["SMOKETREE_RULE"] = ctx.rule_name
        for key, value in ctx.keys.items():
            env[f"SMOKETREE_KEY_{key.upper()}"] = value

        result = subprocess.run(
            ctx.command,
            shell=True,
            cwd=ctx.project.root,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ExecutionError(
                f"Shell command failed (exit {result.returncode}):\n"
                f"  $ {ctx.command}\n"
                f"{_tail(result.stdout, 'stdout')}"
                f"{_tail(result.stderr, 'stderr')}"
            )


def _tail(text: str, label: str, lines: int = 20) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return f"--- {label} (tail) ---\n{tail}\n"
