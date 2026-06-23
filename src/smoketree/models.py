"""Pydantic models for project config, pipelines (rules), and transformers.

The PathTree core models a pipeline as a list of **rules**. A rule binds input
path-patterns to output path-patterns and a shell command; the DAG is inferred and
fan-out comes from the ``{key}`` axes discovered by globbing the tree (see ``bind.py``
and ``engine.py``). The ``Transformer`` union below is retained, dormant, for a later
slice that ports the non-shell backends onto this core.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

MediaType = Literal["image", "audio", "video", "text", "data", "latent"]
MEDIA_TYPES: tuple[str, ...] = ("image", "audio", "video", "text", "data", "latent")


# --------------------------------------------------------------------------- #
# Project config
# --------------------------------------------------------------------------- #


class Defaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    comfyui_url: str = "http://localhost:8188"
    ollama_url: str = "http://localhost:11434"
    image_max_edge: int = 1536
    # Fixpoint circuit breaker: a single `run` performs at most this many passes
    # before erroring. Guards a runaway rule from re-invoking paid transforms forever.
    max_iterations: int = 100


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    defaults: Defaults = Field(default_factory=Defaults)
    env: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pipeline definition (rules)
# --------------------------------------------------------------------------- #


class FeedbackSpec(BaseModel):
    """A feedback channel attached to an (output) rule.

    ``append`` is a path-pattern (keyed by a subset of the rule's keys) for a
    human-authored notes file that smoketree **seeds** once per discovered key-tuple
    (placeholder ``(no feedback yet)``) and thereafter never clobbers. The render rule
    that declares it *owns* the channel; a separate compile rule typically reads the
    file and turns the notes into a directive. Notes are meant to accumulate (append).
    """

    model_config = ConfigDict(extra="forbid")

    append: str


class Rule(BaseModel):
    """One transform: input path-pattern(s) -> output path-pattern(s) + a command.

    Patterns may contain ``{key}`` axes (one path segment, also a command variable)
    and plain ``*`` / ``**`` globs. An input pattern containing a glob makes that input
    a *list* (the globbed axis collapses); a pattern with only keys is a *scalar*. An
    ``out`` pattern carrying a key that no ``in`` binds is a *scatter* — the command
    writes a runtime-determined set under the owned directory prefix.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    in_: dict[str, str] = Field(default_factory=dict, alias="in")
    out: dict[str, str] = Field(default_factory=dict)
    backend: str = "shell"
    # set false to turn a rule (a whole stage) off without deleting it: it never binds,
    # runs, seeds its feedback, or surfaces in the workspace. Flip back to re-enable.
    enabled: bool = True
    # a human-feedback channel attached to this rule's output (seeded, never clobbered)
    feedback: FeedbackSpec | None = None
    # shell backend: the command template. Required for backend=shell, ignored otherwise.
    run: str | None = None
    # non-shell backends (ollama, replicate, ...): backend-specific settings — model,
    # prompt/system templates, params, seed_field, per-input field maps, etc. The backend
    # interprets it. Template strings reference inputs/keys by {name}.
    config: dict[str, Any] = Field(default_factory=dict)
    # delete managed, key-scoped children under this rule's owned prefix that vanish
    # from the regenerated set (scatter GC). Scoped to the binding.
    prune: bool = False


class Pipeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rules: list[Rule] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Transformer definitions (dormant — retained for a later backend-port slice)
# --------------------------------------------------------------------------- #


class ComfyInject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    field: str


class ComfyCollect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    field: str


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["file"] = "file"
    media: MediaType
    inject: ComfyInject | None = None
    field: str | None = None
    array: bool = False


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["file"] = "file"
    media: MediaType
    format: str | None = None
    collect: ComfyCollect | None = None


class _BaseTransformer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)


class ShellTransformer(_BaseTransformer):
    type: Literal["shell"]
    command: str
    env: dict[str, str] = Field(default_factory=dict)


class ClaudeTransformer(_BaseTransformer):
    type: Literal["claude"]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    system: str | None = None
    prompt: str
    image_max_edge: int | None = None


class OllamaTransformer(_BaseTransformer):
    type: Literal["ollama"]
    model: str
    system: str | None = None
    prompt: str
    think: bool | None = None
    image_max_edge: int | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ComfyUITransformer(_BaseTransformer):
    type: Literal["comfyui"]
    workflow: str
    seed_inject: ComfyInject | None = None


class ReplicateTransformer(_BaseTransformer):
    type: Literal["replicate"]
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    seed_field: str | None = None
    image_max_edge: int | None = None


class HTTPTransformer(_BaseTransformer):
    model_config = ConfigDict(extra="allow")

    type: Literal["http"]


Transformer = Annotated[
    Union[
        ShellTransformer,
        ClaudeTransformer,
        OllamaTransformer,
        ComfyUITransformer,
        ReplicateTransformer,
        HTTPTransformer,
    ],
    Field(discriminator="type"),
]
