"""Graph parsing: resolution, DAG construction, topological sort, validation.

A graph YAML is loaded into a :class:`ResolvedGraph`, which carries the node
definitions, their resolved transformers, the topological execution order, and the
parsed input references. Media-type compatibility is checked here at parse time —
mismatches are hard errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from .errors import ValidationError
from .loader import load_yaml
from .models import GraphDef, MediaType, NodeDef, Transformer
from .project import Project

# Map common file extensions to media types, used to infer the media type of a
# source artifact for parse-time validation.
_EXT_MEDIA: dict[str, MediaType] = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "webp": "image", "bmp": "image", "tiff": "image", "tif": "image",
    "wav": "audio", "mp3": "audio", "flac": "audio", "ogg": "audio",
    "aac": "audio", "m4a": "audio",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video", "webm": "video",
    "txt": "text", "md": "text",
    "json": "data", "yaml": "data", "yml": "data", "csv": "data",
    "latent": "latent",
}


def infer_media_from_path(path: Path) -> MediaType | None:
    ext = path.suffix.lstrip(".").lower()
    return _EXT_MEDIA.get(ext)


@dataclass
class InputRef:
    """A parsed ``node_id`` or ``node_id.output_name`` input reference."""

    node_id: str
    output_name: str | None  # None means "first declared output"

    @classmethod
    def parse(cls, raw: str) -> "InputRef":
        node_id, sep, output_name = raw.partition(".")
        return cls(node_id=node_id, output_name=output_name if sep else None)


@dataclass
class ResolvedGraph:
    id: str
    name: str
    nodes: dict[str, NodeDef]
    transformers: dict[str, Transformer]  # node_id -> transformer (transform nodes only)
    input_refs: dict[str, dict[str, InputRef]]  # node_id -> input_name -> ref
    execution_order: list[str] = field(default_factory=list)
    # node_id -> whether the node resolves to multiple artifacts (a collection)
    collections: dict[str, bool] = field(default_factory=dict)

    def is_collection(self, node_id: str) -> bool:
        return self.collections.get(node_id, False)

    def dependencies(self, node_id: str) -> list[str]:
        """Direct upstream node ids that ``node_id`` consumes."""
        return [ref.node_id for ref in self.input_refs.get(node_id, {}).values()]

    def subgraph_order(self, target: str) -> list[str]:
        """Execution order for ``target`` and its transitive dependencies."""
        if target not in self.nodes:
            raise ValidationError(f"Node '{target}' not found in graph '{self.id}'.")
        keep: set[str] = set()
        stack = [target]
        while stack:
            node_id = stack.pop()
            if node_id in keep:
                continue
            keep.add(node_id)
            stack.extend(self.dependencies(node_id))
        return [n for n in self.execution_order if n in keep]


def load_graph(project: Project, graph_id: str) -> ResolvedGraph:
    """Load, parse, and validate a graph. Raises :class:`ValidationError`."""
    path = project.graph_path(graph_id)
    data = load_yaml(path)
    try:
        graph_def = GraphDef.model_validate(data)
    except PydanticValidationError as exc:
        raise ValidationError(f"Invalid graph '{graph_id}' ({path}):\n{exc}") from exc

    transformers: dict[str, Transformer] = {}
    input_refs: dict[str, dict[str, InputRef]] = {}

    _validate_node_shapes(project, graph_def, transformers, input_refs)
    _validate_references(graph_id, graph_def, input_refs)

    execution_order = _topological_sort(graph_id, graph_def, input_refs)

    graph = ResolvedGraph(
        id=graph_id,
        name=graph_def.name,
        nodes=dict(graph_def.nodes),
        transformers=transformers,
        input_refs=input_refs,
        execution_order=execution_order,
    )
    _validate_media_types(graph)
    _resolve_collections(graph)
    return graph


def _resolve_collections(graph: ResolvedGraph) -> None:
    """Mark which nodes resolve to multiple artifacts, and validate ``expand``.

    A node is a collection if it is a ``collection`` node, or a transform that
    consumes at least one collection input. Computed in topological order so a
    node's inputs are already classified.
    """
    for node_id in graph.execution_order:
        node = graph.nodes[node_id]
        if node.type == "collection":
            graph.collections[node_id] = True
            continue
        if node.type == "source":
            graph.collections[node_id] = False
            continue

        collection_inputs = [
            name
            for name, ref in graph.input_refs[node_id].items()
            if graph.is_collection(ref.node_id)
        ]
        graph.collections[node_id] = bool(collection_inputs)
        _validate_expand(node_id, node, collection_inputs)


def _validate_expand(
    node_id: str, node: NodeDef, collection_inputs: list[str]
) -> None:
    if not collection_inputs:
        if node.expand is not None:
            raise ValidationError(
                f"Node '{node_id}' declares expand='{node.expand}' but has no "
                f"collection inputs. Omit 'expand'."
            )
        return
    if node.expand is None:
        raise ValidationError(
            f"Node '{node_id}' consumes collection input(s) "
            f"{', '.join(sorted(collection_inputs))} but does not declare 'expand' "
            f"(one of: product, zip, each)."
        )
    if node.expand == "each" and len(collection_inputs) != 1:
        raise ValidationError(
            f"Node '{node_id}' uses expand='each' but has "
            f"{len(collection_inputs)} collection inputs "
            f"({', '.join(sorted(collection_inputs))}); 'each' requires exactly one."
        )


def _validate_node_shapes(
    project: Project,
    graph_def: GraphDef,
    transformers: dict[str, Transformer],
    input_refs: dict[str, dict[str, InputRef]],
) -> None:
    for node_id, node in graph_def.nodes.items():
        if node.type == "source":
            if not node.path:
                raise ValidationError(
                    f"Source node '{node_id}' must declare a 'path'."
                )
            if node.transformer or node.inputs:
                raise ValidationError(
                    f"Source node '{node_id}' must not declare a transformer or inputs."
                )
        elif node.type == "collection":
            if not node.glob:
                raise ValidationError(
                    f"Collection node '{node_id}' must declare a 'glob'."
                )
            if node.transformer or node.inputs or node.path:
                raise ValidationError(
                    f"Collection node '{node_id}' must declare only a 'glob'."
                )
        else:  # transform
            if not node.transformer:
                raise ValidationError(
                    f"Transform node '{node_id}' must declare a 'transformer'."
                )
            transformer = project.load_transformer(node.transformer)
            transformers[node_id] = transformer
            input_refs[node_id] = {
                name: InputRef.parse(ref) for name, ref in node.inputs.items()
            }
            _validate_node_inputs(node_id, node, transformer)


def _validate_node_inputs(
    node_id: str, node: NodeDef, transformer: Transformer
) -> None:
    declared = set(transformer.inputs)
    provided = set(node.inputs)
    missing = declared - provided
    extra = provided - declared
    if missing:
        raise ValidationError(
            f"Node '{node_id}' (transformer '{transformer.name}') is missing "
            f"inputs: {', '.join(sorted(missing))}."
        )
    if extra:
        raise ValidationError(
            f"Node '{node_id}' (transformer '{transformer.name}') declares unknown "
            f"inputs: {', '.join(sorted(extra))}. "
            f"Declared inputs: {', '.join(sorted(declared)) or '(none)'}."
        )


def _validate_references(
    graph_id: str,
    graph_def: GraphDef,
    input_refs: dict[str, dict[str, InputRef]],
) -> None:
    for node_id, refs in input_refs.items():
        for input_name, ref in refs.items():
            if ref.node_id not in graph_def.nodes:
                raise ValidationError(
                    f"Node '{node_id}' input '{input_name}' references unknown node "
                    f"'{ref.node_id}'."
                )
            producer = graph_def.nodes[ref.node_id]
            if ref.output_name and producer.type in ("source", "collection"):
                raise ValidationError(
                    f"Node '{node_id}' input '{input_name}' uses dotted reference "
                    f"'{ref.node_id}.{ref.output_name}', but '{ref.node_id}' is a "
                    f"{producer.type} node with a single output."
                )


def _topological_sort(
    graph_id: str,
    graph_def: GraphDef,
    input_refs: dict[str, dict[str, InputRef]],
) -> list[str]:
    # Kahn's algorithm. Iterate in declaration order for deterministic output.
    deps: dict[str, set[str]] = {
        node_id: {ref.node_id for ref in input_refs.get(node_id, {}).values()}
        for node_id in graph_def.nodes
    }
    order: list[str] = []
    resolved: set[str] = set()
    while len(resolved) < len(graph_def.nodes):
        ready = [
            node_id
            for node_id in graph_def.nodes
            if node_id not in resolved and deps[node_id] <= resolved
        ]
        if not ready:
            remaining = [n for n in graph_def.nodes if n not in resolved]
            raise ValidationError(
                f"Graph '{graph_id}' contains a cycle involving: "
                f"{', '.join(sorted(remaining))}."
            )
        for node_id in ready:
            order.append(node_id)
            resolved.add(node_id)
    return order


def _validate_media_types(graph: ResolvedGraph) -> None:
    for node_id, refs in graph.input_refs.items():
        transformer = graph.transformers[node_id]
        for input_name, ref in refs.items():
            in_media = transformer.inputs[input_name].media
            out_media = _producer_media(graph, ref)
            if out_media is not None and out_media != in_media:
                raise ValidationError(
                    f"Media type mismatch: node '{node_id}' input '{input_name}' "
                    f"expects '{in_media}', but '{_ref_label(ref)}' produces "
                    f"'{out_media}'."
                )


def _producer_media(graph: ResolvedGraph, ref: InputRef) -> MediaType | None:
    producer = graph.nodes[ref.node_id]
    if producer.type == "source":
        return infer_media_from_path(Path(producer.path or ""))
    if producer.type == "collection":
        return infer_media_from_path(Path(producer.glob or ""))
    transformer = graph.transformers[ref.node_id]
    outputs = transformer.outputs
    if not outputs:
        raise ValidationError(
            f"Node '{ref.node_id}' (transformer '{transformer.name}') declares no "
            f"outputs but is used as an input."
        )
    if ref.output_name is None:
        first = next(iter(outputs))
        return outputs[first].media
    if ref.output_name not in outputs:
        raise ValidationError(
            f"Node '{ref.node_id}' has no output '{ref.output_name}'. "
            f"Available: {', '.join(outputs)}."
        )
    return outputs[ref.output_name].media


def _ref_label(ref: InputRef) -> str:
    return f"{ref.node_id}.{ref.output_name}" if ref.output_name else ref.node_id
