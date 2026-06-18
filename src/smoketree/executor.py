"""Execution: input resolution, fan-out, caching, dirs, seed injection, dispatch.

Every node resolves to a *list* of artifacts: sources and plain
transforms produce one; collection nodes and transforms that consume collections produce
many. A fanned-out transform expands its collection inputs (``product``/``zip``/``each``)
into independent *instances*, each with its own cache key, instance directory, and state
entry.
"""

from __future__ import annotations

import itertools
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import cache as cachelib
from .backends import ExecutionContext, get_backend
from .cache import Artifact, State
from .errors import ExecutionError, SmoketreeError
from .graph import InputRef, ResolvedGraph, infer_media_from_path
from .models import ComfyUITransformer
from .project import Project

Reporter = Callable[[str], None]


@dataclass
class PlanEntry:
    node_id: str
    action: str  # "SKIP" | "RUN" | "PENDING"
    reason: str


# An input binding is either a single artifact (a flat collection item or scalar) or a
# list of artifacts (a grouped collection item — the whole group of files).
InputBinding = "Artifact | list[Artifact]"


def _binding_artifacts(value) -> list[Artifact]:
    return value if isinstance(value, list) else [value]


@dataclass
class Instance:
    """One execution of a node: the resolved input bindings that produced it.

    A binding is a single :class:`Artifact`, or a list of artifacts when the input is a
    grouped (multi-file) collection item.
    """

    inputs: "dict[str, Artifact | list[Artifact]]"

    @property
    def key(self) -> dict[str, str]:
        return {
            name: "\x1f".join(str(a.path) for a in _binding_artifacts(val))
            for name, val in self.inputs.items()
        }

    @property
    def hash(self) -> str:
        return cachelib.instance_hash(self.key)

    def label(self, node_id: str) -> str:
        if not self.inputs:
            return node_id
        tokens: list[str] = []
        for _, val in sorted(self.inputs.items()):
            if isinstance(val, list):
                # a group — label by its common parent dir (e.g. the city name)
                tokens.append(val[0].path.parent.name if val else "empty")
            else:
                tokens.append(_artifact_token(val.path))
        return "|".join(tokens)


# --------------------------------------------------------------------------- #
# Resolution (artifact lists + fan-out), with per-run memoization
# --------------------------------------------------------------------------- #


def _artifact_token(path: Path) -> str:
    """A short, distinguishing label for an input artifact.

    For a fanned-out upstream output (``.../{node}/{hash}/take_N/{out}.ext``) the
    filename is generic, so use the upstream instance hash; otherwise use the stem.
    """
    if path.parent.name.startswith("take_"):
        return path.parent.parent.name
    return path.stem


def _ref_output(graph: ResolvedGraph, ref: InputRef) -> str:
    producer = graph.nodes[ref.node_id]
    if producer.type in ("source", "collection"):
        return ""  # single implicit output
    if ref.output_name is not None:
        return ref.output_name
    return next(iter(graph.transformers[ref.node_id].outputs))


class Resolver:
    """Resolves node outputs to artifact lists and expands transforms into instances.

    Reads producer outputs from the cache, preferring take 0 and falling back to the
    run take (so a graph built at take 0 stays the stable base for other takes).
    """

    def __init__(self, project: Project, graph: ResolvedGraph, take: int):
        self.project = project
        self.graph = graph
        self.take = take
        self._produced: dict[tuple[str, str, str | None], list[Artifact]] = {}
        self._instances: dict[str, list[Instance]] = {}
        self._collection_items: dict[str, list[tuple[Artifact, frozenset[str]]]] = {}

    def produced_artifacts(
        self, node_id: str, output_name: str, filter_tag: str | None = None
    ) -> list[Artifact]:
        cache_key = (node_id, output_name, filter_tag)
        if cache_key in self._produced:
            return self._produced[cache_key]
        result = self._produced_artifacts(node_id, output_name, filter_tag)
        self._produced[cache_key] = result
        return result

    def _produced_artifacts(
        self, node_id: str, output_name: str, filter_tag: str | None
    ) -> list[Artifact]:
        node = self.graph.nodes[node_id]
        if node.type == "source":
            return [resolve_source_artifact(self.project, node.path or "")]
        if node.type == "collection":
            return self._filter_collection(node_id, filter_tag)

        # transform: one artifact per instance
        transformer = self.graph.transformers[node_id]
        spec = transformer.outputs[output_name]
        collection = self.graph.is_collection(node_id)
        artifacts: list[Artifact] = []
        for inst in self.transform_instances(node_id):
            inst_hash = inst.hash if collection else None
            art = self._read_instance_output(node_id, output_name, inst_hash, spec.media)
            if art is None:
                raise ExecutionError(
                    f"Dependency '{node_id}.{output_name}' has no cached output for "
                    f"instance {inst.label(node_id)}. Run the graph at take 0 first."
                )
            artifacts.append(art)
        return artifacts

    def _filter_collection(
        self, node_id: str, filter_tag: str | None
    ) -> list[Artifact]:
        items = self._collection_items_for(node_id)
        if filter_tag is not None:
            items = [(art, tags) for art, tags in items if filter_tag in tags]
        if not items:
            detail = (
                f"filter_tag '{filter_tag}' matched no items"
                if filter_tag is not None
                else "matched no files"
            )
            raise ExecutionError(f"Collection node '{node_id}' {detail}.")
        return [art for art, _ in items]

    def _collection_items_for(
        self, node_id: str
    ) -> list[tuple[Artifact, frozenset[str]]]:
        if node_id in self._collection_items:
            return self._collection_items[node_id]
        node = self.graph.nodes[node_id]
        items: list[tuple[Artifact, frozenset[str]]] = []
        if node.sources is not None:
            for src in node.sources:
                items.append(
                    (resolve_source_artifact(self.project, src.path), frozenset(src.tags))
                )
        else:
            files = sorted(
                p for p in self.project.root.glob(node.glob or "") if p.is_file()
            )
            for p in files:
                items.append((
                    Artifact(
                        path=p.resolve(),
                        media=infer_media_from_path(p) or "data",
                        format=p.suffix.lstrip(".").lower() or None,
                        content_hash=cachelib.hash_file(p),
                    ),
                    frozenset(),
                ))
        self._collection_items[node_id] = items
        return items

    def _read_instance_output(
        self, node_id: str, output_name: str, inst_hash: str | None, media
    ) -> Artifact | None:
        for take in (0, self.take):
            node_dir = cachelib.cache_instance_dir(
                self.project, self.graph.id, node_id, inst_hash, take
            )
            matches = sorted(node_dir.glob(f"{output_name}.*"))
            if matches:
                path = matches[0]
                return Artifact(
                    path=path,
                    media=media,
                    format=path.suffix.lstrip(".").lower() or None,
                    content_hash=cachelib.hash_file(path),
                )
        return None

    def produced_items(self, ref: InputRef) -> list:
        """The fan-out units of a producer: one per collection item.

        For a grouped collection each unit is a list of artifacts (the group); for a flat
        collection / source / transform each unit is a single artifact.
        """
        producer = self.graph.nodes[ref.node_id]
        if producer.type == "collection" and producer.group_by:
            return self._grouped_items(ref.node_id)
        return self.produced_artifacts(
            ref.node_id, _ref_output(self.graph, ref), ref.filter_tag
        )

    def _grouped_items(self, node_id: str) -> list[list[Artifact]]:
        node = self.graph.nodes[node_id]
        files = sorted(
            p for p in self.project.root.glob(node.glob or "") if p.is_file()
        )
        groups: dict[str, list[Artifact]] = {}
        for p in files:
            key = p.parent.name  # group_by == "parent"
            groups.setdefault(key, []).append(
                Artifact(
                    path=p.resolve(),
                    media=infer_media_from_path(p) or "data",
                    format=p.suffix.lstrip(".").lower() or None,
                    content_hash=cachelib.hash_file(p),
                )
            )
        if not groups:
            raise ExecutionError(
                f"Collection node '{node_id}' glob '{node.glob}' matched no files."
            )
        return [groups[key] for key in sorted(groups)]

    def collection_size(self, node_id: str) -> tuple[int, str]:
        """(count, unit) for a collection node, for plan/run reporting."""
        node = self.graph.nodes[node_id]
        if node.group_by:
            return len(self._grouped_items(node_id)), "groups"
        return len(self._filter_collection(node_id, None)), "files"

    def transform_instances(self, node_id: str) -> list[Instance]:
        if node_id in self._instances:
            return self._instances[node_id]
        result = self._transform_instances(node_id)
        self._instances[node_id] = result
        return result

    def _transform_instances(self, node_id: str) -> list[Instance]:
        refs = self.graph.input_refs.get(node_id, {})
        coll_names = self.graph.collection_inputs.get(node_id, set())
        collection_inputs = [name for name in refs if name in coll_names]

        # Collection inputs contribute fan-out items (single artifact, or a group list);
        # singleton inputs each resolve to a single artifact.
        input_items: dict[str, list] = {
            name: self.produced_items(refs[name]) for name in collection_inputs
        }
        singletons: dict[str, Artifact] = {
            name: self.produced_artifacts(
                refs[name].node_id, _ref_output(self.graph, refs[name]),
                refs[name].filter_tag,
            )[0]
            for name in refs
            if name not in coll_names
        }
        if not collection_inputs:
            return [Instance(inputs=dict(singletons))]

        combos = _combine(
            node_id, self.graph.nodes[node_id].expand, collection_inputs, input_items
        )
        return [Instance(inputs={**singletons, **combo}) for combo in combos]


def _combine(
    node_id: str,
    expand: str | None,
    collection_inputs: list[str],
    input_lists: dict[str, list],
) -> list[dict]:
    if expand == "each":
        only = collection_inputs[0]
        return [{only: art} for art in input_lists[only]]
    if expand == "zip":
        lengths = {c: len(input_lists[c]) for c in collection_inputs}
        if len(set(lengths.values())) > 1:
            detail = ", ".join(f"{c}={n}" for c, n in lengths.items())
            raise ExecutionError(
                f"Node '{node_id}' expand='zip' requires equal-length collection "
                f"inputs, got {detail}."
            )
        length = next(iter(lengths.values()))
        return [
            {c: input_lists[c][i] for c in collection_inputs} for i in range(length)
        ]
    # product
    pools = [input_lists[c] for c in collection_inputs]
    return [
        dict(zip(collection_inputs, combo)) for combo in itertools.product(*pools)
    ]


def resolve_source_artifact(project: Project, path_rel: str) -> Artifact:
    path = (project.root / path_rel).resolve()
    if not path.exists():
        raise SmoketreeError(f"Source file not found: {path}")
    return Artifact(
        path=path,
        media=infer_media_from_path(path) or "data",
        format=path.suffix.lstrip(".").lower() or None,
        content_hash=cachelib.hash_file(path),
    )


# --------------------------------------------------------------------------- #
# Cache key
# --------------------------------------------------------------------------- #


def _transformer_text(project: Project, node_id: str, graph: ResolvedGraph) -> str:
    name = graph.nodes[node_id].transformer
    assert name is not None
    return project.transformer_path(name).read_text()


def _workflow_text(project: Project, graph: ResolvedGraph, node_id: str) -> str | None:
    transformer = graph.transformers[node_id]
    if isinstance(transformer, ComfyUITransformer):
        path = project.transformers_dir / transformer.workflow
        if path.exists():
            return path.read_text()
    return None


def compute_node_cache_key(
    project: Project,
    graph: ResolvedGraph,
    node_id: str,
    inputs: "dict[str, Artifact | list[Artifact]]",
    take: int,
) -> str:
    input_hashes = {
        name: "+".join(a.content_hash for a in _binding_artifacts(val))
        for name, val in inputs.items()
    }
    return cachelib.compute_cache_key(
        input_hashes=input_hashes,
        transformer_text=_transformer_text(project, node_id, graph),
        workflow_text=_workflow_text(project, graph, node_id),
        take=take,
    )


def _instance_outputs_present(
    project: Project,
    graph: ResolvedGraph,
    node_id: str,
    inst_hash: str | None,
    take: int,
) -> bool:
    transformer = graph.transformers[node_id]
    node_dir = cachelib.cache_instance_dir(project, graph.id, node_id, inst_hash, take)
    for name in transformer.outputs:
        if not list(node_dir.glob(f"{name}.*")):
            return False
    return True


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def compute_plan(
    project: Project,
    graph: ResolvedGraph,
    take: int,
    target_node: str | None = None,
    force: bool = False,
) -> list[PlanEntry]:
    order = (
        graph.subgraph_order(target_node) if target_node else graph.execution_order
    )
    resolver = Resolver(project, graph, take)
    state = State.load(project, graph.id)
    entries: list[PlanEntry] = []
    will_run: set[str] = set()

    for node_id in order:
        node = graph.nodes[node_id]
        if node.type == "source":
            path = (project.root / (node.path or "")).resolve()
            action, reason = ("SKIP", "source") if path.exists() else (
                "PENDING", "missing source file")
            entries.append(PlanEntry(node_id, action, reason))
            continue
        if node.type == "collection":
            try:
                n, unit = resolver.collection_size(node_id)
                entries.append(PlanEntry(node_id, "SKIP", f"collection, {n} {unit}"))
            except SmoketreeError:
                entries.append(PlanEntry(node_id, "PENDING", "glob matched no files"))
            continue

        deps = graph.dependencies(node_id)
        if any(dep in will_run for dep in deps):
            will_run.add(node_id)
            entries.append(PlanEntry(node_id, "RUN", "upstream will rebuild"))
            continue
        if force:
            will_run.add(node_id)
            entries.append(PlanEntry(node_id, "RUN", "forced"))
            continue

        try:
            instances = resolver.transform_instances(node_id)
            to_build = sum(
                1
                for inst in instances
                if not _instance_cached(project, graph, state, node_id, inst, take)
            )
        except SmoketreeError:
            will_run.add(node_id)
            entries.append(PlanEntry(node_id, "RUN", "inputs not yet built"))
            continue

        n = len(instances)
        if to_build == 0:
            reason = "cached" if n == 1 else f"{n} cached"
            entries.append(PlanEntry(node_id, "SKIP", reason))
        else:
            will_run.add(node_id)
            reason = (
                "changed or never built"
                if n == 1
                else f"{to_build}/{n} to build"
            )
            entries.append(PlanEntry(node_id, "RUN", reason))

    return entries


def _instance_cached(
    project: Project,
    graph: ResolvedGraph,
    state: State,
    node_id: str,
    inst: Instance,
    take: int,
) -> bool:
    collection = graph.is_collection(node_id)
    inst_hash = inst.hash if collection else None
    key = compute_node_cache_key(project, graph, node_id, inst.inputs, take)
    recorded = state.get(node_id, inst.hash)
    return (
        recorded is not None
        and recorded.input_hash == key
        and _instance_outputs_present(project, graph, node_id, inst_hash, take)
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def run(
    project: Project,
    graph: ResolvedGraph,
    take: int,
    target_node: str | None = None,
    force: bool = False,
    report: Reporter = print,
) -> None:
    order = (
        graph.subgraph_order(target_node) if target_node else graph.execution_order
    )
    resolver = Resolver(project, graph, take)
    state = State.load(project, graph.id)
    exported = _exported_nodes(graph, order)

    for node_id in order:
        node = graph.nodes[node_id]
        if node.type == "source":
            resolve_source_artifact(project, node.path or "")  # existence + hash
            report(f"[SKIP]  {node_id:<16}(source)")
            continue
        if node.type == "collection":
            n, unit = resolver.collection_size(node_id)
            report(f"[SKIP]  {node_id:<16}(collection, {n} {unit})")
            continue

        instances = resolver.transform_instances(node_id)
        collection = graph.is_collection(node_id)
        fresh_links: set[Path] = set()
        for inst in instances:
            col = (
                f"{node_id} · {inst.label(node_id)}  "
                if collection
                else f"{node_id:<16}"
            )
            key = compute_node_cache_key(project, graph, node_id, inst.inputs, take)
            inst_hash = inst.hash if collection else None

            cached = (
                not force
                and (rec := state.get(node_id, inst.hash)) is not None
                and rec.input_hash == key
                and _instance_outputs_present(project, graph, node_id, inst_hash, take)
            )
            if cached:
                report(f"[SKIP]  {col}(cached)")
            else:
                report(f"[RUN ]  {col}...")
                started = time.monotonic()
                produced = _execute_instance(project, graph, node_id, inst, take)
                elapsed = time.monotonic() - started
                _validate_outputs(graph, node_id, produced, report)
                if collection:
                    _write_instance_sidecar(project, graph, node_id, inst, take)
                state.record(node_id, inst.hash, key, take)
                state.save()
                report(f"[DONE]  {col}({elapsed:.1f}s)")

            # Export this instance immediately so outputs/ fills in as the run proceeds.
            if node_id in exported:
                link = _link_instance(project, graph, node_id, inst, take)
                if link is not None:
                    fresh_links.add(link)

        if node_id in exported:
            _prune_stale_links(project, graph.id, node_id, fresh_links)


def _execute_instance(
    project: Project,
    graph: ResolvedGraph,
    node_id: str,
    inst: Instance,
    take: int,
) -> dict[str, Artifact]:
    transformer = graph.transformers[node_id]
    collection = graph.is_collection(node_id)
    inst_hash = inst.hash if collection else None

    scratch_dir = cachelib.scratch_instance_dir(
        project, graph.id, node_id, inst_hash, take
    )
    output_dir = cachelib.cache_instance_dir(
        project, graph.id, node_id, inst_hash, take
    )
    # Scratch and the take's cache dir are cleared and recreated so no stale outputs
    # linger across reruns.
    _reset_dir(scratch_dir)
    _reset_dir(output_dir)

    output_targets: dict[str, Path] = {}
    for name, spec in transformer.outputs.items():
        filename = f"{name}.{spec.format}" if spec.format else name
        output_targets[name] = output_dir / filename

    seed = cachelib.compute_seed(graph.id, node_id, take)
    ctx = ExecutionContext(
        project=project,
        graph_id=graph.id,
        node_id=node_id,
        transformer=transformer,
        inputs=inst.inputs,
        output_targets=output_targets,
        scratch_dir=scratch_dir,
        output_dir=output_dir,
        seed=seed,
        take=take,
    )

    produced_paths = get_backend(transformer).execute(ctx)

    produced: dict[str, Artifact] = {}
    for name, path in produced_paths.items():
        spec = transformer.outputs[name]
        produced[name] = Artifact(
            path=path,
            media=spec.media,
            format=path.suffix.lstrip(".").lower() or None,
            content_hash=cachelib.hash_file(path) if path.exists() else "",
        )
    return produced


def _write_instance_sidecar(
    project: Project,
    graph: ResolvedGraph,
    node_id: str,
    inst: Instance,
    take: int,
) -> None:
    """Write ``.instance.json`` recording the instance key for human inspection.

    A single-file input records a path string; a grouped (multi-file) input records the
    list of paths (relative to the project root where possible).
    """
    inst_dir = (
        cachelib.cache_instance_dir(project, graph.id, node_id, inst.hash, take).parent
    )
    inst_dir.mkdir(parents=True, exist_ok=True)

    def _rel(path: Path) -> str:
        try:
            return str(path.relative_to(project.root))
        except ValueError:
            return str(path)

    rel: dict[str, object] = {}
    for name, val in inst.inputs.items():
        paths = [_rel(a.path) for a in _binding_artifacts(val)]
        rel[name] = paths if isinstance(val, list) else paths[0]
    (inst_dir / ".instance.json").write_text(
        json.dumps({"inputs": rel}, indent=2) + "\n"
    )


def _validate_outputs(
    graph: ResolvedGraph,
    node_id: str,
    produced: dict[str, Artifact],
    report: Reporter,
) -> None:
    transformer = graph.transformers[node_id]
    for name, spec in transformer.outputs.items():
        artifact = produced.get(name)
        if artifact is None or not artifact.path.exists():
            raise ExecutionError(
                f"Node '{node_id}' did not produce declared output '{name}'."
            )
        if spec.format and artifact.format and artifact.format != spec.format:
            report(
                f"  warning: output '{name}' expected format '{spec.format}' "
                f"but produced '.{artifact.format}'"
            )


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _exported_nodes(graph: ResolvedGraph, order: list[str]) -> set[str]:
    """Nodes whose outputs go to outputs/: terminal transforms + `output: true`."""
    consumed = {
        ref.node_id for refs in graph.input_refs.values() for ref in refs.values()
    }
    return {
        node_id
        for node_id in order
        if graph.nodes[node_id].type == "transform"
        and (node_id not in consumed or graph.nodes[node_id].output)
    }


def _link_instance(
    project: Project, graph: ResolvedGraph, node_id: str, inst: Instance, take: int
) -> Path | None:
    """Link one instance's first output into outputs/ (single name, or hash-suffixed
    for a fanned-out node). Returns the link path, or None if nothing is there yet."""
    transformer = graph.transformers[node_id]
    if not transformer.outputs:
        return None
    first_output = next(iter(transformer.outputs))
    collection = graph.is_collection(node_id)
    inst_hash = inst.hash if collection else None
    node_dir = cachelib.cache_instance_dir(project, graph.id, node_id, inst_hash, take)
    matches = sorted(node_dir.glob(f"{first_output}.*"))
    if not matches:
        return None
    source = matches[0]
    ext = source.suffix
    project.outputs_dir.mkdir(parents=True, exist_ok=True)
    if collection:
        link = project.outputs_dir / f"{graph.id}__{node_id}__{inst.hash}{ext}"
    else:
        link = project.outputs_dir / f"{graph.id}__{node_id}{ext}"
    _link_or_copy(source, link)
    return link


def _prune_stale_links(
    project: Project, graph_id: str, node_id: str, fresh: set[Path]
) -> None:
    """Remove this node's old output links that weren't recreated this run.

    Keeps ``outputs/`` in sync when a node's instances change (e.g. after rewiring).
    Only this node's links are touched — ``{graph}__{node}.ext`` or
    ``{graph}__{node}__{hash}.ext`` — so partial runs don't disturb other nodes.
    """
    dot = f"{graph_id}__{node_id}."
    grouped = f"{graph_id}__{node_id}__"
    for existing in project.outputs_dir.iterdir():
        name = existing.name
        if (name.startswith(dot) or name.startswith(grouped)) and existing not in fresh:
            if existing.is_symlink() or existing.is_file():
                existing.unlink()


def _link_or_copy(source: Path, link: Path) -> None:
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(source)
    except OSError:  # pragma: no cover - Windows / unsupported FS
        shutil.copy2(source, link)
