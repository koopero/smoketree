"""Replicate hosted-model backend (path core, https://replicate.com).

Reads its settings from the rule's ``config`` block:
  model       "owner/name" or "owner/name:<version>"  (required)
  params      static model inputs merged into the request  (default {})
  seed_field  inject the per-job seed into this model field  (optional)
  fields      per-input overrides: {name: {field: <model-field>, array: bool}}
  image_max_edge  downscale cap for image inputs (px long edge)  (optional)

Each input is mapped to a model field by media: text/data -> the file's text, image ->
a ``data:`` URI. Outputs are taken in declared order from the prediction result and
written to the rule's declared output paths.

``replicate`` is an optional dependency (the ``[replicate]`` extra), imported lazily.
The API token comes from ``REPLICATE_API_TOKEN``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import httpx

from ..errors import ExecutionError
from ..images import encode_image
from ..media import infer_media
from .base import Backend, ExecutionContext

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
# Low-credit accounts are throttled hard (e.g. 6 predictions/min, burst 1). Retry
# patiently, honouring the "resets in ~Ns" hint Replicate returns in the 429.
_MAX_RETRIES = 8
_RETRY_WAIT = 15.0  # fallback when no reset hint is present


class ReplicateBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = ctx.config
        model = cfg.get("model")
        if not model:
            raise ExecutionError(f"Rule '{ctx.rule_name}': replicate config needs a 'model'.")

        inp = self._build_input(ctx)
        output = self._run_prediction(model, inp)
        items = list(output) if isinstance(output, (list, tuple)) else [output]

        for i, (name, target) in enumerate(ctx.outputs.items()):
            if i >= len(items):
                raise ExecutionError(
                    f"Replicate model '{model}' returned {len(items)} output(s), but "
                    f"rule '{ctx.rule_name}' needs output '{name}' (index {i})."
                )
            data = self._extract(items[i], target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, str):
                target.write_text(data)
            else:
                target.write_bytes(data)

    def _build_input(self, ctx: ExecutionContext) -> dict:
        cfg = ctx.config
        fields = cfg.get("fields", {})
        max_edge = cfg.get("image_max_edge", ctx.project.config.defaults.image_max_edge)

        inp: dict = dict(cfg.get("params", {}))
        for name, value in ctx.inputs.items():
            spec = fields.get(name, {})
            array = spec.get("array", False)
            field = spec.get("field", name)
            paths = value if isinstance(value, list) else [value]
            if isinstance(value, list) and not array:
                raise ExecutionError(
                    f"Replicate input '{name}' is a multi-file (list) input; set "
                    f"fields.{name}.array: true, or pass a single file."
                )
            values = [self._value_for(name, p, max_edge) for p in paths]
            if array:
                # Several distinct inputs may target the same array field (e.g. a
                # multi-reference compose: model_ref + outfit_ref -> input_images).
                # Accumulate in declaration order rather than overwriting.
                inp.setdefault(field, [])
                inp[field].extend(values)
            else:
                inp[field] = values[0]

        if cfg.get("seed_field"):
            inp[cfg["seed_field"]] = ctx.seed
        return inp

    @staticmethod
    def _value_for(name: str, path: Path, max_edge: int) -> str:
        media = infer_media(path)
        if media == "image":
            b64, media_type = encode_image(path, max_edge)
            return f"data:{media_type};base64,{b64}"
        if media in ("text", "data"):
            return path.read_text()
        raise ExecutionError(
            f"Replicate input '{name}' has unsupported media '{media}' ({path})."
        )

    def _run_prediction(self, model: str, inp: dict):
        token = os.environ.get("REPLICATE_API_TOKEN")
        if not token:
            raise ExecutionError(
                "REPLICATE_API_TOKEN is not set. Add it to your .env or environment "
                "(create one at https://replicate.com/account/api-tokens)."
            )
        try:
            import replicate
        except ModuleNotFoundError as exc:
            raise ExecutionError(
                "The 'replicate' package is required for replicate rules. Install it "
                "with: uv pip install 'smoketree[replicate]'."
            ) from exc
        client = replicate.Client(api_token=token)
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return client.run(model, input=inp)
            except Exception as exc:  # the SDK raises a range of error types
                last_exc = exc
                if _is_throttle(exc) and attempt < _MAX_RETRIES - 1:
                    time.sleep(_retry_wait(exc))
                    continue
                break
        raise ExecutionError(
            f"Replicate prediction for model '{model}' failed: {last_exc}"
        ) from last_exc

    def _extract(self, item, target: Path):
        """Normalise one prediction output item to bytes (or text for a .txt/.md target)."""
        read = getattr(item, "read", None)
        if callable(read):  # SDK FileOutput
            return read()
        if isinstance(item, (bytes, bytearray)):
            return bytes(item)
        url = getattr(item, "url", None)
        if isinstance(url, str):
            return _download(url)
        if isinstance(item, str):
            if item.startswith(("http://", "https://")):
                return _download(item)
            return item if infer_media(target) == "text" else item.encode("utf-8")
        raise ExecutionError(
            f"Don't know how to read a Replicate output of type '{type(item).__name__}'."
        )


def _is_throttle(exc: Exception) -> bool:
    if getattr(exc, "status", None) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "throttled" in text or "rate limit" in text


def _retry_wait(exc: Exception) -> float:
    """Honour Replicate's 'resets in ~Ns' hint (plus a small buffer), else the default."""
    m = re.search(r"resets in ~?(\d+)\s*s", str(exc))
    return float(m.group(1)) + 3.0 if m else _RETRY_WAIT


def _download(url: str) -> bytes:
    try:
        resp = httpx.get(url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExecutionError(f"Failed to download Replicate output {url}: {exc}") from exc
    return resp.content
