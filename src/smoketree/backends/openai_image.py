"""OpenAI image backend (path core): generate (or edit) an image with the Images API.

Reads its settings from the rule's ``config`` block:
  model        OpenAI image model (default ``gpt-image-1``)
  prompt       prompt template (required); ``{name}`` tokens inline text/data inputs
  size         image size, e.g. ``1024x1024`` / ``1536x1024`` / ``1024x1536`` / ``auto``
  quality      optional: ``low`` / ``medium`` / ``high`` / ``auto``
  background    optional: ``transparent`` / ``opaque`` / ``auto``
  image_max_edge  optional: downscale reference images before sending

Text/data inputs are inlined into the prompt at their ``{name}`` token. If the rule has
image inputs, they become reference images and the call uses the *edit* endpoint (so this
backend serves both txt2img and reference-guided editing); otherwise it generates from the
prompt alone. The decoded image is written to the rule's single declared output.

The API key comes from ``OPENAI_API_KEY`` (read from the project's ``.env``).
"""

from __future__ import annotations

import base64

from ..errors import ExecutionError
from ._prompt import render_prompt
from .base import Backend, ExecutionContext

_DEFAULT_MODEL = "gpt-image-1"


class OpenAIImageBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = ctx.config
        model = cfg.get("model", _DEFAULT_MODEL)
        if "prompt" not in cfg:
            raise ExecutionError(f"Rule '{ctx.rule_name}': openai_image config needs a 'prompt'.")
        if len(ctx.outputs) != 1:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': openai_image needs exactly one output "
                f"(got {len(ctx.outputs)})."
            )
        _, target = next(iter(ctx.outputs.items()))

        prompt, image_paths = render_prompt(
            cfg["prompt"], {**ctx.inputs, **ctx.context}, ctx.keys
        )
        if not prompt.strip():
            raise ExecutionError(f"Rule '{ctx.rule_name}': openai_image rendered an empty prompt.")

        kwargs: dict = {"model": model, "prompt": prompt, "n": 1}
        for key in ("size", "quality", "background"):
            if cfg.get(key) is not None:
                kwargs[key] = cfg[key]

        from openai import OpenAI

        try:
            client = OpenAI()  # reads OPENAI_API_KEY; raises here if it's missing
            if image_paths:
                # Reference images present -> edit endpoint (txt+image -> image).
                handles = [path.open("rb") for path in image_paths]
                try:
                    resp = client.images.edit(image=handles, **kwargs)
                finally:
                    for handle in handles:
                        handle.close()
            else:
                resp = client.images.generate(**kwargs)
        except Exception as exc:  # missing key / network / API failures
            raise ExecutionError(f"OpenAI image API call failed: {exc}") from exc

        data = self._image_bytes(resp, client)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    @staticmethod
    def _image_bytes(resp, client) -> bytes:
        """Decode the first image from an Images API response (b64 or, failing that, url)."""
        item = resp.data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(item, "url", None)
        if url:
            import httpx

            r = httpx.get(url, timeout=60.0)
            r.raise_for_status()
            return r.content
        raise ExecutionError("OpenAI image response had neither b64_json nor url.")
