"""Pipeline loading, validation, and best-effort static dependency inference.

Execution does not need an explicit DAG — the fixpoint loop in ``engine.py`` re-globs
the tree and re-plans each pass. Dependency inference here is only for human-facing
display (``plan``) and cycle warnings: a rule R *feeds* rule S when one of R's output
patterns can produce a path that one of S's input patterns matches.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError as PydanticValidationError

from .bind import Pattern, _VAR
from .errors import ValidationError
from .loader import load_yaml
from .models import Pipeline, Rule
from .project import Project


@dataclass
class LoadedPipeline:
    id: str
    pipeline: Pipeline
    # rule name -> set of upstream rule names that feed it (inferred, best-effort)
    deps: dict[str, set[str]] = field(default_factory=dict)

    @property
    def rules(self) -> list[Rule]:
        return self.pipeline.rules

    @property
    def name(self) -> str:
        return self.pipeline.name


def load_pipeline(project: Project, pipeline_id: str) -> LoadedPipeline:
    """Load, parse, and validate a pipeline. Raises :class:`ValidationError`."""
    path = project.graph_path(pipeline_id)
    data = load_yaml(path)
    try:
        pipeline = Pipeline.model_validate(data)
    except PydanticValidationError as exc:
        raise ValidationError(f"Invalid pipeline '{pipeline_id}' ({path}):\n{exc}") from exc

    _validate(pipeline_id, pipeline)
    deps = infer_dependencies(pipeline)
    return LoadedPipeline(id=pipeline_id, pipeline=pipeline, deps=deps)


def _validate(pipeline_id: str, pipeline: Pipeline) -> None:
    seen: set[str] = set()
    for rule in pipeline.rules:
        if rule.name in seen:
            raise ValidationError(
                f"Pipeline '{pipeline_id}' declares duplicate rule '{rule.name}'."
            )
        seen.add(rule.name)

        in_keys: set[str] = set()
        for name, pat in rule.in_.items():
            in_keys |= set(Pattern.compile(pat).keys)
        out_keys: set[str] = set()
        for name, pat in rule.out.items():
            out_keys |= set(Pattern.compile(pat).keys)

        if rule.backend == "shell" and rule.run is None:
            raise ValidationError(
                f"Rule '{rule.name}': backend 'shell' requires a 'run' command."
            )

        ports = set(rule.in_) | set(rule.out)
        unknown_ports = set(rule.schemas) - ports
        if unknown_ports:
            raise ValidationError(
                f"Rule '{rule.name}': schema declared for unknown port(s) "
                f"{', '.join(sorted(unknown_ports))}. Ports: "
                f"{', '.join(sorted(ports)) or '(none)'}."
            )

        fb_names: set[str] = set()
        for channel in rule.feedback:
            if channel.name in fb_names:
                raise ValidationError(
                    f"Rule '{rule.name}': duplicate feedback channel name '{channel.name}'."
                )
            fb_names.add(channel.name)
            unknown = set(Pattern.compile(channel.path).keys) - (in_keys | out_keys)
            if unknown:
                raise ValidationError(
                    f"Rule '{rule.name}': feedback channel '{channel.name}' uses key(s) "
                    f"{', '.join(sorted(unknown))} that the rule doesn't bind. "
                    f"Keys available: {', '.join(sorted(in_keys | out_keys)) or '(none)'}."
                )

        # `run` may reference input names, output names, and any bound key. Output keys
        # not bound by an input are scatter axes (resolved to an owned dir, not a value).
        bound_vars = set(rule.in_) | set(rule.out) | in_keys | (out_keys - in_keys)
        if rule.run is not None:
            for m in _VAR.finditer(rule.run):
                var = m.group(1)
                if var not in bound_vars:
                    raise ValidationError(
                        f"Rule '{rule.name}': command references unknown variable "
                        f"'{{{var}}}'. Known: {', '.join(sorted(bound_vars)) or '(none)'}."
                    )


def infer_dependencies(pipeline: Pipeline) -> dict[str, set[str]]:
    """For each rule, the set of rules whose outputs can feed its inputs."""
    out_pats = {r.name: [Pattern.compile(p) for p in r.out.values()] for r in pipeline.rules}
    in_pats = {r.name: [Pattern.compile(p) for p in r.in_.values()] for r in pipeline.rules}

    deps: dict[str, set[str]] = {r.name: set() for r in pipeline.rules}
    for consumer in pipeline.rules:
        for producer in pipeline.rules:
            if producer.name == consumer.name:
                continue
            if any(
                _feeds(op, ip)
                for op in out_pats[producer.name]
                for ip in in_pats[consumer.name]
            ):
                deps[consumer.name].add(producer.name)
    return deps


def _feeds(out_pat: Pattern, in_pat: Pattern) -> bool:
    """Whether ``out_pat`` could produce a path matched by ``in_pat`` (heuristic)."""
    probe = _VAR.sub("__x__", out_pat.raw)
    probe = probe.replace("**", "__x__").replace("*", "__x__")
    if in_pat.regex.match(probe):
        return True
    reverse = _VAR.sub("__x__", in_pat.raw).replace("**", "__x__").replace("*", "__x__")
    return bool(out_pat.regex.match(reverse))


def execution_order(loaded: LoadedPipeline) -> list[str]:
    """A best-effort topological order over the inferred deps (declaration order on tie)."""
    order: list[str] = []
    resolved: set[str] = set()
    names = [r.name for r in loaded.rules]
    while len(resolved) < len(names):
        ready = [n for n in names if n not in resolved and loaded.deps[n] <= resolved]
        if not ready:  # a cycle — emit the rest in declaration order
            order.extend(n for n in names if n not in resolved)
            break
        for n in ready:
            order.append(n)
            resolved.add(n)
    return order
