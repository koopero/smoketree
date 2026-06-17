"""Execution: input resolution, caching, dirs, seed injection, backend dispatch.

Implements the execution flow from DESIGN.md. ``compute_plan`` produces the dry-run
view; ``run`` executes nodes in topological order with full caching semantics.
"""

from __future__ import annotations

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


# --------------------------------------------------------------------------- #
# Input resolution
# --------------------------------------------------------------------------- #


def _selected_output(graph: ResolvedGraph, ref: InputRef) -> str:
    transformer = graph.transformers[ref.node_id]
    if ref.output_name is not None:
        return ref.output_name
    return next(iter(transformer.outputs))


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


def find_cached_output(
    project: Project,
    graph: ResolvedGraph,
    producer_id: str,
    output_name: str,
    run_take: int,
) -> Path | None:
    """Locate a producer's output file, preferring take 0 then the run take."""
    for take in (0, run_take):
        node_dir = cachelib.cache_node_dir(project, graph.id, producer_id, take)
        matches = sorted(node_dir.glob(f"{output_name}.*"))
        if matches:
            return matches[0]
    return None


def resolve_input_artifact(
    project: Project, graph: ResolvedGraph, ref: InputRef, run_take: int
) -> Artifact:
    producer = graph.nodes[ref.node_id]
    if producer.type == "source":
        return resolve_source_artifact(project, producer.path or "")

    output_name = _selected_output(graph, ref)
    transformer = graph.transformers[ref.node_id]
    spec = transformer.outputs[output_name]
    path = find_cached_output(project, graph, ref.node_id, output_name, run_take)
    if path is None:
        raise ExecutionError(
            f"Dependency '{ref.node_id}.{output_name}' has no cached output. "
            f"Run the graph at take 0 first."
        )
    return Artifact(
        path=path,
        media=spec.media,
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
    inputs: dict[str, Artifact],
    take: int,
) -> str:
    input_hashes = {name: art.content_hash for name, art in inputs.items()}
    return cachelib.compute_cache_key(
        input_hashes=input_hashes,
        transformer_text=_transformer_text(project, node_id, graph),
        workflow_text=_workflow_text(project, graph, node_id),
        take=take,
    )


def _outputs_present(
    project: Project, graph: ResolvedGraph, node_id: str, take: int
) -> bool:
    transformer = graph.transformers[node_id]
    node_dir = cachelib.cache_node_dir(project, graph.id, node_id, take)
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
    state = State.load(project, graph.id)
    entries: list[PlanEntry] = []
    will_run: set[str] = set()

    for node_id in order:
        node = graph.nodes[node_id]
        if node.type == "source":
            path = (project.root / (node.path or "")).resolve()
            if path.exists():
                entries.append(PlanEntry(node_id, "SKIP", "source"))
            else:
                entries.append(PlanEntry(node_id, "PENDING", "missing source file"))
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
            inputs = _resolve_inputs(project, graph, node_id, take)
            key = compute_node_cache_key(project, graph, node_id, inputs, take)
        except SmoketreeError:
            will_run.add(node_id)
            entries.append(PlanEntry(node_id, "RUN", "inputs not yet built"))
            continue

        recorded = state.nodes.get(node_id)
        if (
            recorded
            and recorded.input_hash == key
            and _outputs_present(project, graph, node_id, take)
        ):
            entries.append(PlanEntry(node_id, "SKIP", "cached"))
        else:
            will_run.add(node_id)
            entries.append(PlanEntry(node_id, "RUN", "changed or never built"))

    return entries


def _resolve_inputs(
    project: Project, graph: ResolvedGraph, node_id: str, take: int
) -> dict[str, Artifact]:
    refs = graph.input_refs.get(node_id, {})
    return {
        name: resolve_input_artifact(project, graph, ref, take)
        for name, ref in refs.items()
    }


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
    state = State.load(project, graph.id)

    for node_id in order:
        node = graph.nodes[node_id]
        if node.type == "source":
            resolve_source_artifact(project, node.path or "")  # existence + hash
            report(f"[SKIP]  {node_id:<16}(source)")
            continue

        inputs = _resolve_inputs(project, graph, node_id, take)
        key = compute_node_cache_key(project, graph, node_id, inputs, take)

        recorded = state.nodes.get(node_id)
        if (
            not force
            and recorded
            and recorded.input_hash == key
            and _outputs_present(project, graph, node_id, take)
        ):
            report(f"[SKIP]  {node_id:<16}(cached)")
            continue

        report(f"[RUN ]  {node_id:<16}...")
        started = time.monotonic()
        produced = _execute_node(project, graph, node_id, inputs, take, report)
        elapsed = time.monotonic() - started

        _validate_outputs(graph, node_id, produced, report)
        state.record(node_id, key, take)
        state.save()
        report(f"[DONE]  {node_id:<16}({elapsed:.1f}s)")

    _update_output_links(project, graph, take, order, report)


def _execute_node(
    project: Project,
    graph: ResolvedGraph,
    node_id: str,
    inputs: dict[str, Artifact],
    take: int,
    report: Reporter,
) -> dict[str, Artifact]:
    transformer = graph.transformers[node_id]

    scratch_dir = cachelib.scratch_node_dir(project, graph.id, node_id, take)
    output_dir = cachelib.cache_node_dir(project, graph.id, node_id, take)
    # Scratch is cleared and recreated on every run; cache take dir is reset so no
    # stale outputs linger.
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
        inputs=inputs,
        output_targets=output_targets,
        scratch_dir=scratch_dir,
        output_dir=output_dir,
        seed=seed,
        take=take,
    )

    backend = get_backend(transformer)
    produced_paths = backend.execute(ctx)

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


def _update_output_links(
    project: Project,
    graph: ResolvedGraph,
    take: int,
    order: list[str],
    report: Reporter,
) -> None:
    """Symlink (or copy) terminal-node outputs into ``outputs/``."""
    consumed = {
        ref.node_id for refs in graph.input_refs.values() for ref in refs.values()
    }
    terminals = [
        node_id
        for node_id in order
        if graph.nodes[node_id].type == "transform" and node_id not in consumed
    ]
    if not terminals:
        return
    project.outputs_dir.mkdir(parents=True, exist_ok=True)

    for node_id in terminals:
        transformer = graph.transformers[node_id]
        if not transformer.outputs:
            continue
        first_output = next(iter(transformer.outputs))
        source = find_cached_output(project, graph, node_id, first_output, take)
        if source is None:
            continue
        ext = source.suffix
        link = project.outputs_dir / f"{graph.id}__{node_id}{ext}"
        _link_or_copy(source, link)


def _link_or_copy(source: Path, link: Path) -> None:
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(source)
    except OSError:  # pragma: no cover - Windows / unsupported FS
        shutil.copy2(source, link)
