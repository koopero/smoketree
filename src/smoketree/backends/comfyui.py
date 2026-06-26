"""ComfyUI backend (path core): inject inputs into a workflow, submit, collect.

Reads its settings from the rule's ``config`` block:
  workflow      path to a workflow JSON (API format), relative to the project root; may
                contain ``{key}`` axes (e.g. a generated ``songs/{song}/wf.json``)  (required)
  seed_inject   {node, field}: write the per-job seed into this workflow node field  (optional)
  inputs        {name: {node, field}}: inject input ``name`` into that node field; OR
                {name: {node_prefix, field}}: fan a list input across nodes ``<prefix>1..N``
  outputs       {name: {node}}: collect the first media file (image / video / animation)
                produced by that node
  timeout       seconds to wait for the render before erroring (default 600; raise it for
                slow renders like long video generation)  (optional)

Each declared input is injected by media: an image / video / audio file is uploaded and its
server-side name written to the field (feeding a LoadImage / LoadVideo / LoadAudio node);
text/data is read and its text written. Produced media is polled from history and
downloaded to the rule's declared output paths.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx

from ..errors import ExecutionError
from ..media import infer_media
from .base import Backend, ExecutionContext

_POLL_INTERVAL = 1.0
_POLL_TIMEOUT = 600.0


class ComfyUIBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = ctx.config
        workflow_rel = cfg.get("workflow")
        if not workflow_rel:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': comfyui config needs a 'workflow' path."
            )
        # Interpolate the binding's {key} axes so a per-binding workflow path works (e.g. a
        # generated `songs/{song}/refframe.json`); plain paths have no {key} and pass through.
        for key, val in ctx.keys.items():
            workflow_rel = workflow_rel.replace("{" + key + "}", val)
        workflow_path = ctx.project.root / workflow_rel
        if not workflow_path.exists():
            raise ExecutionError(f"ComfyUI workflow not found: {workflow_path}.")
        workflow = _node_dict(json.loads(workflow_path.read_text()))

        base_url = str(ctx.project.config.defaults.comfyui_url).rstrip("/")
        client = httpx.Client(base_url=base_url, timeout=30.0)
        client_id = str(uuid.uuid4())
        try:
            self._inject_inputs(client, workflow, ctx)
            prompt_id = self._submit(client, workflow, client_id)
            timeout = float(cfg.get("timeout", _POLL_TIMEOUT))
            history = self._poll_history(client, prompt_id, timeout)
            self._collect_outputs(client, history, ctx)
        except httpx.HTTPError as exc:
            raise ExecutionError(f"ComfyUI request failed: {exc}") from exc
        finally:
            client.close()

    def _inject_inputs(
        self, client: httpx.Client, workflow: dict, ctx: ExecutionContext
    ) -> None:
        cfg = ctx.config

        seed_inject = cfg.get("seed_inject")
        if seed_inject:
            node = _node(workflow, seed_inject["node"], "seed_inject")
            node["inputs"][seed_inject["field"]] = ctx.seed

        for name, spec in cfg.get("inputs", {}).items():
            value = ctx.inputs.get(name)
            if value is None:
                raise ExecutionError(
                    f"Rule '{ctx.rule_name}': comfyui config injects input '{name}', "
                    f"but the rule declares no such input."
                )
            # A list/glob input with `node_prefix` fans across numbered nodes: each file goes
            # to <prefix>1, <prefix>2, … (e.g. 1-5 reference images into ref1..refN). The
            # workflow is expected to have exactly as many ref nodes as files (generated to
            # match), so injection is 1:1.
            if "node_prefix" in spec:
                paths = value if isinstance(value, list) else [value]
                for i, path in enumerate(paths, 1):
                    node = _node(workflow, f"{spec['node_prefix']}{i}",
                                 f"input '{name}' #{i}")
                    self._inject_one(client, node, spec["field"], path, name)
                continue
            if isinstance(value, list):
                raise ExecutionError(
                    f"ComfyUI input '{name}' received a multi-file (grouped) input; set "
                    f"`node_prefix` to fan it across numbered nodes, or pass a single file."
                )
            node = _node(workflow, spec["node"], f"input '{name}'")
            self._inject_one(client, node, spec["field"], value, name)

    def _inject_one(self, client, node, field: str, path: "Path", name: str) -> None:
        media = infer_media(path)
        if media in ("image", "video", "audio"):
            node["inputs"][field] = self._upload_file(client, path)
        elif media in ("text", "data"):
            node["inputs"][field] = path.read_text()
        else:
            raise ExecutionError(
                f"ComfyUI cannot inject media '{media}' ({path}) for input '{name}'."
            )

    def _upload_file(self, client: httpx.Client, path: Path) -> str:
        """Upload a binary input (image / video / audio) to ComfyUI's input dir.

        ComfyUI's ``/upload/image`` endpoint stores any uploaded file under ``input/`` and a
        Load* node (LoadImage, LoadVideo, LoadAudio) reads it back by name — so the same
        endpoint serves all of them.
        """
        data = path.read_bytes()
        # Content-addressed upload name: a Load* node caches by filename across prompts, so
        # reusing a generic name would serve a stale file to later jobs. A content hash keeps
        # distinct files distinct and dedupes identical ones.
        name = f"smoketree_{hashlib.sha256(data).hexdigest()[:16]}{path.suffix.lower()}"
        files = {"image": (name, data)}
        resp = client.post("/upload/image", files=files, data={"overwrite": "true"})
        if resp.status_code >= 400:
            raise ExecutionError(_http_detail(f"upload file '{name}'", resp))
        return resp.json()["name"]

    def _submit(self, client: httpx.Client, workflow: dict, client_id: str) -> str:
        resp = client.post("/prompt", json={"prompt": workflow, "client_id": client_id})
        if resp.status_code >= 400:
            raise ExecutionError(_http_detail("submit prompt", resp))
        body = resp.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise ExecutionError(f"ComfyUI did not return a prompt_id: {body}")
        return prompt_id

    def _poll_history(
        self, client: httpx.Client, prompt_id: str, timeout: float = _POLL_TIMEOUT
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = client.get(f"/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(_POLL_INTERVAL)
        raise ExecutionError(
            f"Timed out after {timeout:.0f}s waiting for ComfyUI prompt {prompt_id}. "
            f"For slow renders (long videos), raise the rule's comfyui `config.timeout`."
        )

    def _collect_outputs(
        self, client: httpx.Client, history: dict, ctx: ExecutionContext
    ) -> None:
        outputs_cfg = ctx.config.get("outputs", {})
        node_outputs = history.get("outputs", {})
        for name, target in ctx.outputs.items():
            spec = outputs_cfg.get(name)
            if spec is None:
                raise ExecutionError(
                    f"Rule '{ctx.rule_name}': comfyui config has no 'outputs.{name}' "
                    f"collect spec for declared output '{name}'."
                )
            node_output = node_outputs.get(spec["node"]) or {}
            files = _output_files(node_output)
            if not files:
                raise ExecutionError(
                    f"ComfyUI node '{spec['node']}' produced no collectable output "
                    f"(for output '{name}'; saw keys "
                    f"{', '.join(sorted(node_output)) or 'none'})."
                )
            data = self._download(client, files[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def _download(self, client: httpx.Client, image: dict) -> bytes:
        resp = client.get(
            "/view",
            params={
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            },
        )
        resp.raise_for_status()
        return resp.content


def _output_files(node_output: dict) -> list:
    """The list of saved file dicts a node reported, whatever media key it used.

    ComfyUI buckets a node's saved files by media type: ``images`` for stills, but
    ``videos`` / ``gifs`` for video and animation nodes (e.g. SaveVideo, VHS). Try those in
    turn, then fall back to any list of file dicts (a ``filename`` entry), so an image and a
    video output collect through the same path.
    """
    for key in ("images", "videos", "gifs", "audio"):
        files = node_output.get(key)
        if files:
            return files
    for value in node_output.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "filename" in value[0]:
            return value
    return []


def _node(workflow: dict, node_id: str, context: str) -> dict:
    node = workflow.get(node_id)
    if node is None or "inputs" not in node:
        raise ExecutionError(
            f"Workflow has no node '{node_id}' with inputs (for {context})."
        )
    return node


def _node_dict(workflow: dict) -> dict:
    """Keep only node entries, dropping annotation keys (e.g. ``_comment``).

    ComfyUI's /prompt treats every top-level key as a node and 500s on a non-node
    value, so a documentation key like ``_comment`` would break submission.
    """
    return {k: v for k, v in workflow.items() if isinstance(v, dict)}


def _http_detail(action: str, resp: httpx.Response) -> str:
    """Build an error including ComfyUI's response body.

    ComfyUI returns workflow validation failures as JSON with ``error`` and
    ``node_errors`` keys; surfacing them is what makes a 4xx/5xx actionable.
    """
    detail = resp.text
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and ("error" in data or "node_errors" in data):
        parts = []
        if data.get("error"):
            parts.append(json.dumps(data["error"], indent=2))
        if data.get("node_errors"):
            parts.append("node_errors:\n" + json.dumps(data["node_errors"], indent=2))
        detail = "\n".join(parts)
    detail = (detail or "").strip()[:2000]
    return f"ComfyUI failed to {action} ({resp.status_code}):\n{detail}"
