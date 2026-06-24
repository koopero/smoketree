"""OpenAI backend (path core): a Chat Completions API call.

Reads its settings from the rule's ``config`` block: ``model`` (required), ``prompt``
(required template), optional ``system``, ``max_tokens`` (default 16000), and
``image_max_edge``. Text/data inputs are inlined into the prompt at their ``{name}`` token;
image inputs are attached as ``image_url`` content blocks (for vision). With an output-port
schema, the response is constrained via Structured Outputs (``response_format`` json_schema,
strict). The raw JSON response is written to the single declared output.

The API key comes from ``OPENAI_API_KEY`` (read from the project's ``.env``).
"""

from __future__ import annotations

from typing import Any

from ..errors import ExecutionError
from ..images import encode_image
from ..serde import write_structured
from ._prompt import render_prompt
from .base import Backend, ExecutionContext

_DEFAULT_MAX_TOKENS = 16000

# JSON Schema validation keywords OpenAI strict Structured Outputs rejects. We strip them
# from the schema we *send* (so generation is constrained on shape + enums), and still
# validate the result against the full schema afterward (the engine does this).
_UNSUPPORTED = frozenset({
    "minLength", "maxLength", "pattern", "format",
    "minItems", "maxItems", "uniqueItems",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "default", "examples",
})


def _strict_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strict_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if isinstance(schema, list):
        return [_strict_schema(v) for v in schema]
    return schema


class OpenAIBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = ctx.config
        model = cfg.get("model")
        if not model:
            raise ExecutionError(f"Rule '{ctx.rule_name}': openai config needs a 'model'.")
        if "prompt" not in cfg:
            raise ExecutionError(f"Rule '{ctx.rule_name}': openai config needs a 'prompt'.")
        if len(ctx.outputs) != 1:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': openai needs exactly one output "
                f"(got {len(ctx.outputs)})."
            )
        output_name, target = next(iter(ctx.outputs.items()))
        schema = ctx.schemas.get(output_name)

        messages: list[dict] = []
        system = self._render_system(ctx)
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": self._user_content(ctx)})

        kwargs: dict = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": cfg.get("max_tokens", _DEFAULT_MAX_TOKENS),
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": _strict_schema(schema), "strict": True},
            }

        from openai import OpenAI

        try:
            client = OpenAI()  # reads OPENAI_API_KEY; raises here if it's missing
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:  # missing key / network / API failures
            raise ExecutionError(f"OpenAI API call failed: {exc}") from exc

        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ExecutionError(
                f"OpenAI declined rule '{ctx.rule_name}': {choice.message.refusal}"
            )
        text = choice.message.content or ""
        if not text.strip():
            raise ExecutionError(
                f"OpenAI model '{model}' returned an empty response "
                f"(finish_reason={choice.finish_reason!r}). Try raising max_tokens."
            )
        if schema is not None:
            write_structured(text, target)  # JSON response -> YAML/JSON per extension
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def _render_system(self, ctx: ExecutionContext) -> str:
        system = ctx.config.get("system")
        if not system:
            return ""
        rendered, _ = render_prompt(system, {**ctx.inputs, **ctx.context}, ctx.keys)
        return rendered

    def _user_content(self, ctx: ExecutionContext) -> list[dict]:
        prompt, image_paths = render_prompt(
            ctx.config["prompt"], {**ctx.inputs, **ctx.context}, ctx.keys
        )
        content: list[dict] = [{"type": "text", "text": prompt}]
        max_edge = ctx.config.get("image_max_edge", ctx.project.config.defaults.image_max_edge)
        for path in image_paths:
            data, media_type = encode_image(path, max_edge)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            })
        return content
