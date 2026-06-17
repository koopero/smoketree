"""Ollama transformer backend: a local LLM call over Ollama's HTTP API.

Mirrors the Claude backend for local-first inference. Text/data inputs are inlined into
the prompt; image inputs are sent as base64 in the ``images`` array (for vision models
such as ``llava``). The deterministic Smoketree seed is injected as ``options.seed`` so
runs are reproducible. The raw response text is written to the single declared output.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..errors import ExecutionError
from ..models import OllamaTransformer
from ._prompt import build_prompt
from .base import Backend, ExecutionContext

# Local generation can be slow on cold model loads; allow a generous read timeout.
_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)


class OllamaBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> dict[str, Path]:
        transformer = ctx.transformer
        assert isinstance(transformer, OllamaTransformer)

        if len(ctx.output_targets) != 1:
            raise ExecutionError(
                f"Ollama transformer '{transformer.name}' must declare exactly one "
                f"output (got {len(ctx.output_targets)})."
            )
        output_name, target = next(iter(ctx.output_targets.items()))

        payload = self._build_payload(ctx)
        base_url = str(ctx.project.config.defaults.ollama_url).rstrip("/")

        try:
            with httpx.Client(base_url=base_url, timeout=_TIMEOUT) as client:
                resp = client.post("/api/generate", json=payload)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise ExecutionError(
                f"Ollama request to {base_url} failed: {exc}. "
                f"Is Ollama running and is model '{transformer.model}' pulled?"
            ) from exc

        text = body.get("response", "")
        if not text.strip():
            raise ExecutionError(
                f"Ollama model '{transformer.model}' returned an empty response "
                f"(done_reason={body.get('done_reason')!r}, "
                f"eval_count={body.get('eval_count')}). "
                f"Try a different seed (--take N), raising options.num_predict, "
                f"or a different model."
            )
        target.write_text(text)
        return {output_name: target}

    def _build_payload(self, ctx: ExecutionContext) -> dict:
        transformer = ctx.transformer
        assert isinstance(transformer, OllamaTransformer)

        prompt, image_paths = build_prompt(transformer.prompt, ctx.inputs)

        # Inject the deterministic seed unless the transformer set one explicitly.
        options = dict(transformer.options)
        options.setdefault("seed", ctx.seed)

        payload: dict = {
            "model": transformer.model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if transformer.system:
            payload["system"] = transformer.system
        if image_paths:
            payload["images"] = [_b64(p) for p in image_paths]
        return payload


def _b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")
