"""Claude transformer backend: an Anthropic Messages API call.

Text and data inputs are inlined into the prompt at their ``{inputs.NAME}`` token.
Image inputs are attached as image content blocks (the token is removed from the
text). The raw text response is written to the single declared output.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ExecutionError
from ..images import encode_image
from ..models import ClaudeTransformer
from ._prompt import build_prompt
from .base import Backend, ExecutionContext


def _resolve_max_edge(ctx: ExecutionContext) -> int:
    override = getattr(ctx.transformer, "image_max_edge", None)
    if override is not None:
        return override
    return ctx.project.config.defaults.image_max_edge


class ClaudeBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> dict[str, Path]:
        transformer = ctx.transformer
        assert isinstance(transformer, ClaudeTransformer)

        if len(ctx.output_targets) != 1:
            raise ExecutionError(
                f"Claude transformer '{transformer.name}' must declare exactly one "
                f"output (got {len(ctx.output_targets)})."
            )
        output_name, target = next(iter(ctx.output_targets.items()))

        prompt_text, image_blocks = self._build_prompt(ctx)

        content: list[dict] = [{"type": "text", "text": prompt_text}]
        content.extend(image_blocks)

        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise ExecutionError(
                "The 'anthropic' package is required for claude transformers."
            ) from exc

        client = Anthropic()
        try:
            message = client.messages.create(
                model=transformer.model,
                max_tokens=transformer.max_tokens,
                system=transformer.system or "",
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # pragma: no cover - network/API failures
            raise ExecutionError(f"Claude API call failed: {exc}") from exc

        text = "".join(
            block.text for block in message.content if block.type == "text"
        )
        target.write_text(text)
        return {output_name: target}

    def _build_prompt(self, ctx: ExecutionContext) -> tuple[str, list[dict]]:
        transformer = ctx.transformer
        assert isinstance(transformer, ClaudeTransformer)
        prompt, image_paths = build_prompt(transformer.prompt, ctx.inputs)
        max_edge = _resolve_max_edge(ctx)
        return prompt, [self._image_block(p, max_edge) for p in image_paths]

    @staticmethod
    def _image_block(path: Path, max_edge: int) -> dict:
        data, media_type = encode_image(path, max_edge)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
