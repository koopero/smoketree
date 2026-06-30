"""Replicate hosted-model backend (path core, https://replicate.com).

Reads its settings from the rule's ``config`` block:
  model       "owner/name" or "owner/name:<version>"  (required)
  params      static model inputs merged into the request  (default {})
  seed_field  inject the per-job seed into this model field  (optional)
  fields      per-input overrides: {name: {field: <model-field>, array: bool}}
  image_max_edge  downscale cap for image inputs (px long edge)  (optional)

Any config string may carry **Jinja** referencing the rule's input data (parsed by name)
and ``{key}`` axes, so the model or a param can be chosen by a data field —
``aspect_ratio: "{{ '3:4' if concept.kind == 'character' else '16:9' }}"`` or a
performance-vs-motion ``model``. Plain (marker-free) config is untouched; inputs are hashed
for staleness, so the raw template is a sound cache key. An **optional** input (rule-level
``optional:`` list) that matches nothing contributes no field rather than unbinding the rule
— one rule can i2i when a reference exists and txt2img when it doesn't.

Each input is mapped to a model field by media: text/data -> the file's text, image /
audio / video / binary -> a ``data:`` URI. Outputs are taken in declared order from the
prediction result and written to the rule's declared output paths.

A **structured (data) output** — a rule with a single non-scatter ``data`` output port
(``.json``/``.yaml``) whose model result is a JSON structure rather than a file (a dict, or
a list of non-file items) — is serialized WHOLE into that port (YAML/JSON by extension), not
downloaded. This captures models that return rich structured results, e.g. Whisper's
``{text, segments, ...}`` transcript. FileOutput values nested in the structure are flattened
to their URLs so it stays JSON-serializable.

A **scatter** output — an ``out`` port whose path carries a ``{key}`` no input binds (e.g.
``songs/{song}/stems/{stem}.wav``) — fans the model's structured result across that axis:
a mapping result keys each file by its name (``{vocals: ..., drums: ...}``), a list result
names files from their URL basename (falling back to a zero-padded index). A scatter port
must be the rule's only output.

``replicate`` is an optional dependency (the ``[replicate]`` extra), imported lazily.
The API token comes from ``REPLICATE_API_TOKEN``.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import time
from pathlib import Path

import httpx

from ..bind import Pattern
from ..errors import ExecutionError
from ..images import encode_image
from ..media import infer_media
from ..serde import dump_data
from ._prompt import build_context, render_str
from .base import Backend, ExecutionContext

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
# Low-credit accounts are throttled hard (e.g. 6 predictions/min, burst 1). Retry
# patiently, honouring the "resets in ~Ns" hint Replicate returns in the 429.
_MAX_RETRIES = 8
_RETRY_WAIT = 15.0  # fallback when no reset hint is present
_POLL_INTERVAL = 3.0
# Default cap on how long to wait for a prediction (incl. cold boot). The blocking SDK
# `client.run` gives up after ~60s, which fails slow/cold models even though the prediction
# keeps running; we create + poll instead so a long render completes. Override per-rule with
# `config.timeout`.
_DEFAULT_TIMEOUT = 900.0


class ReplicateBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = self._render_config(ctx)
        model = cfg.get("model")
        if not model:
            raise ExecutionError(f"Rule '{ctx.rule_name}': replicate config needs a 'model'.")

        inp = self._build_input(ctx, cfg)
        timeout = float(cfg.get("timeout", _DEFAULT_TIMEOUT))
        output = self._run_prediction(model, inp, timeout)

        scatter = [name for name in ctx.outputs if self._scatter_axes(ctx, name)]
        if scatter:
            if len(ctx.outputs) != 1:
                raise ExecutionError(
                    f"Rule '{ctx.rule_name}': scatter replicate output '{scatter[0]}' must be "
                    f"the rule's only output (a single model result is fanned across it)."
                )
            self._write_scatter(ctx, scatter[0], model, output)
            return

        # A single data-file output captures the WHOLE structured result (dict / list of
        # non-file items) as JSON/YAML — e.g. Whisper's {text, segments}. No file download.
        if len(ctx.outputs) == 1:
            name, target = next(iter(ctx.outputs.items()))
            if infer_media(target) == "data" and _is_structured(output):
                target.parent.mkdir(parents=True, exist_ok=True)
                dump_data(_jsonable(output), target)
                return

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

    def _render_config(self, ctx: ExecutionContext) -> dict:
        """Render Jinja in the rule's config over its input data + keys, so model/params can
        be chosen by a data field (e.g. ``aspect_ratio: "{{ ar[concept.kind] }}"`` or a
        performance-vs-motion ``model``). Only strings carrying Jinja markers are touched, so
        plain config is unchanged. Inputs are hashed for staleness, so the raw template as
        cache key is sound."""
        if not _has_template(ctx.config):
            return ctx.config
        context, _ = build_context(ctx.inputs, ctx.keys)
        return _render_tree(ctx.config, context)

    def _build_input(self, ctx: ExecutionContext, cfg: dict) -> dict:
        fields = cfg.get("fields", {})
        max_edge = cfg.get("image_max_edge", ctx.project.config.defaults.image_max_edge)

        inp: dict = dict(cfg.get("params", {}))
        for name, value in ctx.inputs.items():
            spec = fields.get(name, {})
            array = spec.get("array", False)
            field = spec.get("field", name)
            paths = value if isinstance(value, list) else [value]
            if isinstance(value, list) and not paths:
                continue  # an optional input that matched nothing — contribute no field
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
        if media in ("audio", "video"):
            return _data_uri(path)
        raise ExecutionError(
            f"Replicate input '{name}' has unsupported media '{media}' ({path})."
        )

    def _scatter_axes(self, ctx: ExecutionContext, name: str) -> list[str]:
        """Output keys of port ``name`` that no input binds — the scatter axis (or [])."""
        raw = ctx.out_patterns.get(name)
        if not raw:
            return []
        return [k for k in Pattern.compile(raw).keys if k not in ctx.keys]

    def _write_scatter(self, ctx: ExecutionContext, name: str, model: str, output) -> None:
        """Fan a single structured model result across the port's one scatter ``{key}``."""
        axes = self._scatter_axes(ctx, name)
        if len(axes) != 1:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': scatter replicate output '{name}' must have exactly "
                f"one scatter {{key}} (an output key no input binds); found {axes or 'none'}."
            )
        axis = axes[0]
        pattern = Pattern.compile(ctx.out_patterns[name])
        used: set[str] = set()
        for raw_name, item in self._scatter_items(model, output):
            slug, base, n = _slug(raw_name), _slug(raw_name), 2
            while slug in used:  # keep stem names unique within this run
                slug, n = f"{base}-{n}", n + 1
            used.add(slug)
            target = ctx.project.root / pattern.fill({**ctx.keys, axis: slug})
            data = self._extract(item, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, str):
                target.write_text(data)
            else:
                target.write_bytes(data)

    def _scatter_items(self, model: str, output) -> list[tuple[str, object]]:
        """Normalise a model result into ``[(stem_name, item), ...]`` for the scatter axis."""
        if isinstance(output, dict):
            items = [(str(k), v) for k, v in output.items() if v is not None]
        elif isinstance(output, (list, tuple)):
            items = [(self._stem_name(v, i), v) for i, v in enumerate(output)]
        else:
            items = [(self._stem_name(output, 0), output)]
        if not items:
            raise ExecutionError(
                f"Replicate model '{model}' returned no outputs to scatter."
            )
        return items

    @staticmethod
    def _stem_name(item, index: int) -> str:
        """Best-effort name for a list/scalar output item — its URL basename, else index."""
        url = getattr(item, "url", None)
        if not isinstance(url, str) and isinstance(item, str):
            url = item
        if isinstance(url, str):
            base = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            if stem:
                return stem
        return f"{index:03d}"

    def _run_prediction(self, model: str, inp: dict, timeout: float):
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
        prediction = self._create_prediction(client, model, inp)
        # Poll until terminal — outlasts cold boots (unlike the ~60s blocking client.run).
        deadline = time.monotonic() + timeout
        while prediction.status not in ("succeeded", "failed", "canceled"):
            if time.monotonic() > deadline:
                try:
                    prediction.cancel()
                except Exception:
                    pass
                raise ExecutionError(
                    f"Replicate prediction for model '{model}' timed out after {timeout:.0f}s "
                    f"(status {prediction.status!r}). Raise the rule's `config.timeout`."
                )
            time.sleep(_POLL_INTERVAL)
            prediction.reload()
        if prediction.status != "succeeded":
            raise ExecutionError(
                f"Replicate prediction for model '{model}' {prediction.status}: "
                f"{prediction.error}"
            )
        return prediction.output

    def _create_prediction(self, client, model: str, inp: dict):
        """Create a prediction (non-blocking), honouring throttle 429s with patient retries."""
        name, _, version = model.partition(":")
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                if version:
                    return client.predictions.create(version=version, input=inp)
                return client.models.predictions.create(name, input=inp)
            except Exception as exc:  # the SDK raises a range of error types
                last_exc = exc
                if _is_throttle(exc) and attempt < _MAX_RETRIES - 1:
                    time.sleep(_retry_wait(exc))
                    continue
                raise ExecutionError(
                    f"Replicate prediction for model '{model}' failed to start: {exc}"
                ) from exc
        raise ExecutionError(
            f"Replicate prediction for model '{model}' failed to start: {last_exc}"
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


def _slug(value: object) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return s or "item"


def _has_template(obj: object) -> bool:
    """Whether any string anywhere in a config tree carries a Jinja marker."""
    if isinstance(obj, str):
        return "{{" in obj or "{%" in obj
    if isinstance(obj, dict):
        return any(_has_template(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_has_template(v) for v in obj)
    return False


def _render_tree(obj: object, context: dict):
    """Recursively render Jinja in every templated string of a config tree."""
    if isinstance(obj, str):
        return render_str(obj, context) if ("{{" in obj or "{%" in obj) else obj
    if isinstance(obj, dict):
        return {k: _render_tree(v, context) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_tree(v, context) for v in obj]
    return obj


def _is_file_like(item: object) -> bool:
    """Whether a model-output item is a FILE (SDK FileOutput, URL string), not inline data."""
    if callable(getattr(item, "read", None)):
        return True
    if isinstance(getattr(item, "url", None), str):
        return True
    return isinstance(item, str) and item.startswith(("http://", "https://"))


def _is_structured(output: object) -> bool:
    """Whether a model result is inline JSON data (serialize whole) vs file(s) (download)."""
    if isinstance(output, dict):
        return True
    if isinstance(output, list):
        return not any(_is_file_like(x) for x in output)
    return False


def _jsonable(obj: object):
    """Recursively flatten any FileOutput value to its URL so the result is JSON-serializable."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    url = getattr(obj, "url", None)
    if isinstance(url, str):
        return url
    return obj


def _data_uri(path: Path) -> str:
    """A ``data:`` URI for a binary input (audio/video/…); Replicate uploads it for us."""
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{b64}"


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
