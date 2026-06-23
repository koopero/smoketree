"""Claude backend (path core): an Anthropic Messages API call.

Reads its settings from the rule's ``config`` block: ``model`` (default
``claude-opus-4-8``), ``prompt`` (required template), optional ``system``,
``max_tokens`` (default 16000), and ``image_max_edge``. Text/data inputs are inlined
into the prompt at their ``{name}`` token; image inputs are attached as image content
blocks (for vision). The deterministic per-job seed is not used (the API is sampled).
The raw text response is written to the single declared output.

The API key comes from ``ANTHROPIC_API_KEY`` (read from the project's ``.env``).
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ExecutionError
from ..images import encode_image
from ..serde import write_structured
from ._prompt import render_prompt
from .base import Backend, ExecutionContext

_DEFAULT_MODEL = "claude-opus-4-8"
_DEFAULT_MAX_TOKENS = 16000


class ClaudeBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        cfg = ctx.config
        if "prompt" not in cfg:
            raise ExecutionError(f"Rule '{ctx.rule_name}': claude config needs a 'prompt'.")
        if len(ctx.outputs) != 1:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': claude needs exactly one output "
                f"(got {len(ctx.outputs)})."
            )
        output_name, target = next(iter(ctx.outputs.items()))
        schema = ctx.schemas.get(output_name)

        prompt_text, image_blocks = self._build_prompt(ctx)
        content: list[dict] = [{"type": "text", "text": prompt_text}, *image_blocks]

        kwargs: dict = {
            "model": cfg.get("model", _DEFAULT_MODEL),
            "max_tokens": cfg.get("max_tokens", _DEFAULT_MAX_TOKENS),
            "system": self._render_system(ctx),
            "messages": [{"role": "user", "content": content}],
        }
        if schema is not None:
            # Structured outputs: constrain the response to the port's schema.
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

        from anthropic import Anthropic

        client = Anthropic()
        try:
            message = client.messages.create(**kwargs)
        except Exception as exc:  # network/API failures
            raise ExecutionError(f"Claude API call failed: {exc}") from exc

        if message.stop_reason == "refusal":
            raise ExecutionError(
                f"Claude declined rule '{ctx.rule_name}' "
                f"({getattr(message.stop_details, 'category', None)!r})."
            )
        text = "".join(b.text for b in message.content if b.type == "text")
        if not text.strip():
            raise ExecutionError(
                f"Claude returned an empty response for rule '{ctx.rule_name}' "
                f"(stop_reason={message.stop_reason!r}). Try raising max_tokens."
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

    def _build_prompt(self, ctx: ExecutionContext) -> tuple[str, list[dict]]:
        prompt, image_paths = render_prompt(
            ctx.config["prompt"], {**ctx.inputs, **ctx.context}, ctx.keys
        )
        max_edge = ctx.config.get("image_max_edge", ctx.project.config.defaults.image_max_edge)
        return prompt, [self._image_block(p, max_edge) for p in image_paths]

    @staticmethod
    def _image_block(path: Path, max_edge: int) -> dict:
        data, media_type = encode_image(path, max_edge)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
