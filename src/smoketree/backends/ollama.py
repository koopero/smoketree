"""Ollama backend (path core): a local LLM call over Ollama's HTTP API.

Reads its settings from the rule's ``config`` block: ``model`` (required),
``prompt`` (required template), optional ``system``, ``options``, ``think``, and
``image_max_edge``. Text/data inputs are inlined into the prompt; image inputs are sent
as base64 in the ``images`` array (for vision models). The deterministic per-job seed is
injected as ``options.seed``. The response text is written to the single declared output.
"""

from __future__ import annotations

import httpx

from ..errors import ExecutionError
from ..images import encode_image
from ..serde import write_structured
from ._prompt import render_prompt
from .base import Backend, ExecutionContext

_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)


class OllamaBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = ctx.config
        model = cfg.get("model")
        if not model:
            raise ExecutionError(f"Rule '{ctx.rule_name}': ollama config needs a 'model'.")
        if "prompt" not in cfg:
            raise ExecutionError(f"Rule '{ctx.rule_name}': ollama config needs a 'prompt'.")
        if len(ctx.outputs) != 1:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': ollama needs exactly one output "
                f"(got {len(ctx.outputs)})."
            )
        output_name, target = next(iter(ctx.outputs.items()))
        schema = ctx.schemas.get(output_name)

        payload = self._build_payload(ctx)
        if schema is not None:
            payload["format"] = schema  # constrain output to the port's schema
        base_url = str(ctx.project.config.defaults.ollama_url).rstrip("/")
        try:
            with httpx.Client(base_url=base_url, timeout=_TIMEOUT) as client:
                resp = client.post("/api/generate", json=payload)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise ExecutionError(
                f"Ollama request to {base_url} failed: {exc}. "
                f"Is Ollama running and is model '{model}' pulled?"
            ) from exc

        text = body.get("response", "")
        if not text.strip():
            raise ExecutionError(
                f"Ollama model '{model}' returned an empty response "
                f"(done_reason={body.get('done_reason')!r}). Try raising "
                f"options.num_predict or a different model."
            )
        if schema is not None:
            write_structured(text, target)  # JSON response -> YAML/JSON per extension
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def _build_payload(self, ctx: ExecutionContext) -> dict:
        cfg = ctx.config
        prompt, image_paths = render_prompt(cfg["prompt"], ctx.inputs, ctx.keys)

        options = dict(cfg.get("options", {}))
        options.setdefault("seed", ctx.seed)

        payload: dict = {
            "model": cfg["model"],
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if cfg.get("system"):
            system, _ = render_prompt(cfg["system"], ctx.inputs, ctx.keys)
            payload["system"] = system
        if cfg.get("think") is not None:
            payload["think"] = cfg["think"]
        if image_paths:
            max_edge = cfg.get("image_max_edge", ctx.project.config.defaults.image_max_edge)
            payload["images"] = [encode_image(p, max_edge)[0] for p in image_paths]
        return payload
