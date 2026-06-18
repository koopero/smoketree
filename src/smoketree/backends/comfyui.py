"""ComfyUI transformer backend: inject inputs into a workflow, submit, collect.

The workflow JSON (exported from ComfyUI in API format) is loaded, declared input
fields are replaced, the prompt is submitted over the HTTP API, and produced files
are polled from history and downloaded to the output target.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx

from ..errors import ExecutionError
from ..models import ComfyUITransformer
from .base import Backend, ExecutionContext

_POLL_INTERVAL = 1.0
_POLL_TIMEOUT = 600.0


class ComfyUIBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> dict[str, Path]:
        transformer = ctx.transformer
        assert isinstance(transformer, ComfyUITransformer)

        base_url = str(ctx.project.config.defaults.comfyui_url).rstrip("/")
        workflow_path = ctx.project.transformers_dir / transformer.workflow
        if not workflow_path.exists():
            raise ExecutionError(
                f"ComfyUI workflow not found: {workflow_path}."
            )
        workflow = json.loads(workflow_path.read_text())

        client = httpx.Client(base_url=base_url, timeout=30.0)
        client_id = str(uuid.uuid4())
        try:
            self._inject_inputs(client, workflow, ctx)
            prompt_id = self._submit(client, workflow, client_id)
            history = self._poll_history(client, prompt_id)
            return self._collect_outputs(client, history, ctx)
        except httpx.HTTPError as exc:
            raise ExecutionError(f"ComfyUI request failed: {exc}") from exc
        finally:
            client.close()

    def _inject_inputs(
        self, client: httpx.Client, workflow: dict, ctx: ExecutionContext
    ) -> None:
        transformer = ctx.transformer
        assert isinstance(transformer, ComfyUITransformer)

        seed_inject = transformer.seed_inject
        if seed_inject is not None:
            node = workflow.get(seed_inject.node_id)
            if node is None or "inputs" not in node:
                raise ExecutionError(
                    f"Workflow has no node '{seed_inject.node_id}' with inputs "
                    f"(for seed_inject)."
                )
            node["inputs"][seed_inject.field] = ctx.seed

        for name, spec in transformer.inputs.items():
            inject = spec.inject
            if inject is None:
                raise ExecutionError(
                    f"ComfyUI input '{name}' is missing an 'inject' spec."
                )
            artifact = ctx.inputs[name]
            node = workflow.get(inject.node_id)
            if node is None or "inputs" not in node:
                raise ExecutionError(
                    f"Workflow has no node '{inject.node_id}' with inputs "
                    f"(for input '{name}')."
                )
            if spec.media == "image":
                value: object = self._upload_image(client, artifact.path)
            elif spec.media in ("text", "data"):
                value = artifact.path.read_text()
            else:
                raise ExecutionError(
                    f"ComfyUI cannot inject media type '{spec.media}' "
                    f"(input '{name}')."
                )
            node["inputs"][inject.field] = value

    def _upload_image(self, client: httpx.Client, path: Path) -> str:
        files = {"image": (path.name, path.read_bytes())}
        resp = client.post("/upload/image", files=files, data={"overwrite": "true"})
        if resp.status_code >= 400:
            raise ExecutionError(_http_detail(f"upload image '{path.name}'", resp))
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

    def _poll_history(self, client: httpx.Client, prompt_id: str) -> dict:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            resp = client.get(f"/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(_POLL_INTERVAL)
        raise ExecutionError(
            f"Timed out after {_POLL_TIMEOUT:.0f}s waiting for ComfyUI prompt "
            f"{prompt_id}."
        )

    def _collect_outputs(
        self, client: httpx.Client, history: dict, ctx: ExecutionContext
    ) -> dict[str, Path]:
        transformer = ctx.transformer
        assert isinstance(transformer, ComfyUITransformer)
        node_outputs = history.get("outputs", {})
        produced: dict[str, Path] = {}
        for name, spec in transformer.outputs.items():
            collect = spec.collect
            if collect is None:
                raise ExecutionError(
                    f"ComfyUI output '{name}' is missing a 'collect' spec."
                )
            node_output = node_outputs.get(collect.node_id)
            images = (node_output or {}).get("images") or []
            if not images:
                raise ExecutionError(
                    f"ComfyUI node '{collect.node_id}' produced no images "
                    f"(for output '{name}')."
                )
            image = images[0]
            data = self._download(client, image)
            ext = Path(image["filename"]).suffix or (
                f".{spec.format}" if spec.format else ".png"
            )
            target = ctx.output_targets[name].with_suffix(ext)
            target.write_bytes(data)
            produced[name] = target
        return produced

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
