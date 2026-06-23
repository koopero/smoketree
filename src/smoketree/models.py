"""Pydantic models for project config, pipelines (rules), and transformers.

The PathTree core models a pipeline as a list of **rules**. A rule binds input
path-patterns to output path-patterns and a shell command; the DAG is inferred and
fan-out comes from the ``{key}`` axes discovered by globbing the tree (see ``bind.py``
and ``engine.py``). The ``Transformer`` union below is retained, dormant, for a later
slice that ports the non-shell backends onto this core.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class FeedbackChannel(BaseModel):
    """One human-feedback channel attached to a rule's output.

    ``path`` is a path-pattern (keyed by a subset of the rule's keys) for an authored
    file smoketree **seeds** once per discovered key-tuple and never clobbers. ``kind``
    picks the seed + workspace widget:

    - ``notes`` — a free-text log, seeded with a placeholder (``(no feedback yet)``);
    - ``select`` — a single choice among ``options``, seeded as ``{name}: {default}``
      (``default`` defaults to ``options[0]``).

    ``describe`` is shown to the human in the workspace. Channels are plain files,
    consumed downstream as ordinary inputs — this block only governs seeding and
    workspace surfacing. ``name`` (defaulting to ``kind``) identifies the channel and
    must be unique within a rule.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    path: str
    kind: Literal["notes", "select"] = "notes"
    describe: str | None = None
    options: list[str] = Field(default_factory=list)  # select only
    default: str | None = None                          # select only

    @model_validator(mode="after")
    def _check(self) -> "FeedbackChannel":
        if self.name is None:
            self.name = self.kind
        if self.kind == "select":
            if not self.options:
                raise ValueError(
                    f"feedback channel '{self.name}': kind 'select' needs non-empty 'options'."
                )
            if self.default is None:
                self.default = self.options[0]
            elif self.default not in self.options:
                raise ValueError(
                    f"feedback channel '{self.name}': default '{self.default}' is not in "
                    f"options {self.options}."
                )
        elif self.options or self.default is not None:
            raise ValueError(
                f"feedback channel '{self.name}': 'options'/'default' apply only to "
                f"kind 'select'."
            )
        return self


class FilterSpec(BaseModel):
    """A declarative keep/drop predicate over one of a rule's input data files.

    Reads ``input`` (a data file: YAML/JSON), takes ``field`` from it (or the whole value
    when ``field`` is omitted), and keeps the binding when that value equals ``equals`` or
    is among ``among``. A rule with a ``filter`` emits its output only for bindings that
    pass; a binding that fails has its managed output dropped — so the rule *projects a
    selected subset* (e.g. ``approved/{idea}/…``) that downstream globs. No expression
    language: exactly one of ``equals`` / ``among``.
    """

    model_config = ConfigDict(extra="forbid")

    input: str
    field: str | None = None
    equals: str | None = None
    among: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "FilterSpec":
        if (self.equals is not None) == bool(self.among):
            raise ValueError("filter needs exactly one of 'equals' or 'among'.")
        return self

    def matches(self, value: object) -> bool:
        if self.equals is not None:
            return value == self.equals
        return value in self.among


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
    # read-only ambient inputs: globbed and exposed to prompts/commands as {name}, but
    # EXCLUDED from staleness, the inputs-present gate, and dependency inference. Lets a
    # generator consider existing artifacts (an ignore list) without depending on them —
    # the way to read your own output without a fixpoint cycle.
    context: dict[str, str] = Field(default_factory=dict)
    backend: str = "shell"
    # set false to turn a rule (a whole stage) off without deleting it: it never binds,
    # runs, seeds its feedback, or surfaces in the workspace. Flip back to re-enable.
    enabled: bool = True
    # human-feedback channels attached to this rule's output (seeded, never clobbered)
    feedback: list[FeedbackChannel] = Field(default_factory=list)
    # port name -> path of a JSON Schema (authored in YAML) the engine validates that
    # port's data against. An output-port schema also constrains LLM backends. Schema
    # files are dependencies: editing one re-runs and re-validates the rule.
    schemas: dict[str, str] = Field(default_factory=dict, alias="schema")
    # shell backend: the command template. Required for backend=shell, ignored otherwise.
    run: str | None = None
    # non-shell backends (ollama, replicate, ...): backend-specific settings — model,
    # prompt/system templates, params, seed_field, per-input field maps, etc. The backend
    # interprets it. Template strings reference inputs/keys by {name}.
    config: dict[str, Any] = Field(default_factory=dict)
    # delete managed, key-scoped children under this rule's owned prefix that vanish
    # from the regenerated set (scatter GC). Scoped to the binding.
    prune: bool = False
    # keep/drop predicate: the rule emits its output only for bindings that pass, and
    # drops the managed output of bindings that fail (a data-driven selector/gate).
    filter: FilterSpec | None = None


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
