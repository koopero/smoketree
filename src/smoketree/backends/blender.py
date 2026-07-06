"""Blender backend: run a bpy script headless, handing it inputs/outputs via a job file.

Reads its settings from the rule's ``config`` block:
  script    path to a bpy script, relative to the project root; may contain ``{key}`` axes
            (like ComfyUI's ``workflow``)  (required)
  timeout   seconds to wait for the script before erroring (default 600)  (optional)
  args      static extra data merged into the job's ``args``, passed through untouched
            (optional)

The script is invoked as ``blender --background --factory-startup --python-exit-code 1
--python <script>`` with no argv payload — inputs/outputs/keys/args are handed over as a
job JSON, its path given by the ``SMOKETREE_JOB`` env var (alongside the ``SMOKETREE_ROOT``/
``SMOKETREE_RULE``/``SMOKETREE_KEY_*`` vars `shell` already sets), so the script side is two
stdlib lines:

    import os, json
    job = json.load(open(os.environ["SMOKETREE_JOB"]))

Each input is handed over by media: a ``data`` input (``.yaml``/``.yml``/``.json``/``.csv``)
is parsed with ``serde.load_data`` and embedded as a JSON structure directly — this is what
lets a bpy script (whose bundled Python has no pyyaml) consume a YAML timeline without any
external conversion step. Everything else (image / video / audio / mesh / anything
unrecognised) is handed over as a path string (or list of path strings) — Blender's API
always wants a filepath for binary media, never inline bytes. Output parent directories are
created before the script runs; the script must write to the given output paths itself.

``blender_path`` resolves, in order: the ``BLENDER_PATH`` env var (per-machine), the
project's ``defaults.blender_path`` (committed convenience default), then ``blender`` on
``$PATH`` (Linux/CI).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import ExecutionError
from ..media import infer_media
from ..serde import load_data
from .base import Backend, ExecutionContext

_DEFAULT_TIMEOUT = 600.0


class BlenderBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = ctx.config
        script_path = self._resolve_script(ctx, cfg)
        blender_path = self._resolve_blender(ctx)

        for target in ctx.outputs.values():
            target.parent.mkdir(parents=True, exist_ok=True)

        job = {
            "inputs": {name: self._job_value(value) for name, value in ctx.inputs.items()},
            "outputs": {name: str(path) for name, path in ctx.outputs.items()},
            "keys": ctx.keys,
            "args": cfg.get("args", {}),
        }

        env = os.environ.copy()
        env.update(ctx.env)
        env["SMOKETREE_ROOT"] = str(ctx.project.root)
        env["SMOKETREE_RULE"] = ctx.rule_name
        for key, value in ctx.keys.items():
            env[f"SMOKETREE_KEY_{key.upper()}"] = value

        timeout = float(cfg.get("timeout", _DEFAULT_TIMEOUT))
        with tempfile.TemporaryDirectory() as d:
            job_path = Path(d) / "job.json"
            job_path.write_text(json.dumps(job))
            env["SMOKETREE_JOB"] = str(job_path)

            command = [
                blender_path, "--background", "--factory-startup",
                "--python-exit-code", "1", "--python", str(script_path),
            ]
            try:
                result = subprocess.run(
                    command, cwd=ctx.project.root, env=env,
                    capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise ExecutionError(
                    f"Rule '{ctx.rule_name}': Blender script '{script_path}' timed out after "
                    f"{timeout:.0f}s. Raise the rule's `config.timeout`."
                ) from exc

        if result.returncode != 0:
            raise ExecutionError(
                f"Blender script '{script_path}' failed (exit {result.returncode}):\n"
                f"{_tail(result.stdout, 'stdout')}"
                f"{_tail(result.stderr, 'stderr')}"
            )

        missing = [name for name, path in ctx.outputs.items() if not path.exists()]
        if missing:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': Blender script '{script_path}' exited cleanly but "
                f"didn't write output(s): {', '.join(sorted(missing))}."
            )

    def _resolve_script(self, ctx: ExecutionContext, cfg: dict) -> Path:
        script_rel = cfg.get("script")
        if not script_rel:
            raise ExecutionError(f"Rule '{ctx.rule_name}': blender config needs a 'script' path.")
        for key, val in ctx.keys.items():
            script_rel = script_rel.replace("{" + key + "}", val)
        script_path = ctx.project.root / script_rel
        if not script_path.exists():
            raise ExecutionError(f"Blender script not found: {script_path}.")
        return script_path

    def _resolve_blender(self, ctx: ExecutionContext) -> str:
        candidate = (
            os.environ.get("BLENDER_PATH")
            or ctx.project.config.defaults.blender_path
            or shutil.which("blender")
        )
        if not candidate:
            raise ExecutionError(
                "No Blender executable found. Set BLENDER_PATH, add `defaults.blender_path` "
                "to smoketree.yaml, or put `blender` on PATH."
            )
        return candidate

    @staticmethod
    def _job_value(value):
        paths = value if isinstance(value, list) else [value]
        rendered = [BlenderBackend._one_value(p) for p in paths]
        return rendered if isinstance(value, list) else rendered[0]

    @staticmethod
    def _one_value(path: Path):
        if infer_media(path) == "data":
            return load_data(path)
        return str(path)


def _tail(text: str, label: str, lines: int = 20) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return f"--- {label} (tail) ---\n{tail}\n"
