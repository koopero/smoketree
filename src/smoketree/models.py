"""Pydantic models for project config, graphs, and transformers.

These define the internal data model. Parsed YAML is validated into these
shapes; runtime resolution (topological order, artifact paths) is layered on top in
``graph.py`` and ``executor.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

MediaType = Literal["image", "audio", "video", "text", "data", "latent"]
MEDIA_TYPES: tuple[str, ...] = ("image", "audio", "video", "text", "data", "latent")

ExpandStrategy = Literal["product", "zip", "each"]


# --------------------------------------------------------------------------- #
# Project config
# --------------------------------------------------------------------------- #


class Defaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    comfyui_url: str = "http://localhost:8188"
    ollama_url: str = "http://localhost:11434"
    take: int = 0


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    defaults: Defaults = Field(default_factory=Defaults)
    env: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Graph definition
# --------------------------------------------------------------------------- #


class CollectionSource(BaseModel):
    """One explicitly-declared, optionally-tagged item of a collection node."""

    model_config = ConfigDict(extra="forbid")

    path: str
    tags: list[str] = Field(default_factory=list)


class InputDecl(BaseModel):
    """Long-form input declaration: ``{node: ..., filter_tag: ...}``."""

    model_config = ConfigDict(extra="forbid")

    node: str
    filter_tag: str | None = None


# An input value is either a string ("node", "node.output", or the "node[tag]"
# shorthand) or the long-form mapping.
InputValue = Union[str, InputDecl]


class NodeDef(BaseModel):
    """A node as declared in a graph YAML file (before resolution)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["source", "transform", "collection"]
    # source nodes:
    path: str | None = None
    # collection nodes (exactly one of glob / sources):
    glob: str | None = None
    sources: list[CollectionSource] | None = None
    # collection nodes (glob only): group matched files into one item per subdirectory,
    # so a consuming transform runs once per group with the whole group as a multi-file input
    group_by: Literal["parent"] | None = None
    # transform nodes:
    transformer: str | None = None
    inputs: dict[str, InputValue] = Field(default_factory=dict)
    # fan-out strategy; required iff a transform consumes a collection input
    expand: ExpandStrategy | None = None


class GraphDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    nodes: dict[str, NodeDef]


# --------------------------------------------------------------------------- #
# Transformer definitions
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
    # comfyui-only: where to inject this input into the workflow JSON
    inject: ComfyInject | None = None


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["file"] = "file"
    media: MediaType
    format: str | None = None
    # comfyui-only: which workflow node produces this output
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


class OllamaTransformer(_BaseTransformer):
    type: Literal["ollama"]
    model: str
    system: str | None = None
    prompt: str
    # Toggle reasoning for thinking-capable models. Set false so the whole token
    # budget goes to the answer (thinking models otherwise return an empty response).
    think: bool | None = None
    # Passed through to Ollama's "options" (e.g. temperature, num_predict). The
    # deterministic Smoketree seed is injected as options.seed unless overridden here.
    options: dict[str, Any] = Field(default_factory=dict)


class ComfyUITransformer(_BaseTransformer):
    type: Literal["comfyui"]
    workflow: str
    # Optional: inject the deterministic per-take seed into a workflow node (e.g. a
    # KSampler's "seed" field) so different takes produce different generations.
    seed_inject: ComfyInject | None = None


class HTTPTransformer(_BaseTransformer):
    """Generic HTTP request transformer (future work; parsed but not executable)."""

    model_config = ConfigDict(extra="allow")

    type: Literal["http"]


Transformer = Annotated[
    Union[
        ShellTransformer,
        ClaudeTransformer,
        OllamaTransformer,
        ComfyUITransformer,
        HTTPTransformer,
    ],
    Field(discriminator="type"),
]
