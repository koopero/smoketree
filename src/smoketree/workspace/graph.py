"""The full artifact graph of a pipeline — every rule, every instance, its state.

Where [index.py] surfaces only feedback-bearing outputs, this enumerates *all* artifacts
a pipeline intends to produce, assembled entirely from existing engine machinery:

- ``bind_rule`` ([bind.py]) -> every concrete instance (``Binding``) of a rule;
- ``compute_plan`` ([engine.py]) -> each instance's state (RUN/SKIP/PENDING/OFF/DROP) + reason;
- ``State`` ([cache.py]) -> last-completed timestamp;
- ``execution_order`` / ``deps`` ([rules.py]) -> the DAG.

No execution and no new enumeration logic — it reads the same plan the CLI ``plan`` shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..bind import Binding, bind_rule
from ..cache import State
from ..engine import compute_plan
from ..media import infer_media
from ..project import Project
from ..rules import execution_order, load_pipeline
from .channels import ChannelView, read_channels


@dataclass
class OutputView:
    """One output port of one instance, located on disk."""

    port: str
    rel: str            # path relative to project root (the dashboard fetches /file?path=rel)
    media: str
    exists: bool
    is_dir: bool        # scatter owned-directory (vs a concrete file)


@dataclass
class InstanceNode:
    """One binding of a rule: a cell with a state, outputs, and any feedback channels."""

    identity: str
    rule: str
    keys: dict[str, str]
    label: str
    state: str          # RUN | SKIP | PENDING | DROP
    reason: str
    primary: OutputView | None      # primary output (for the thumbnail); None for scatter dirs
    outputs: list[OutputView]
    channels: list[ChannelView]
    reroll: bool
    completed_at: str | None = None


@dataclass
class TriggerInfo:
    describe: str | None


@dataclass
class RuleNode:
    """A rule and its instances (or a placeholder state when nothing binds yet)."""

    name: str
    enabled: bool
    deps: list[str]
    has_feedback: bool
    trigger: TriggerInfo | None
    state: str          # rule-level rollup: OFF / PENDING when there are no instances
    reason: str
    instances: list[InstanceNode] = field(default_factory=list)


@dataclass
class Graph:
    pipeline: str
    rules: list[RuleNode]


def _output_view(root: Path, binding: Binding, port: str) -> OutputView:
    path = binding.outputs[port]
    is_dir = path in binding.owned_prefixes
    return OutputView(
        port=port,
        rel=str(path.relative_to(root)).replace("\\", "/"),
        media=infer_media(path),
        exists=path.exists(),
        is_dir=is_dir,
    )


def _label(rule_name: str, keys: dict[str, str]) -> str:
    return " · ".join(keys[k] for k in sorted(keys)) or rule_name


def build_graph(project: Project, pipeline_id: str) -> Graph:
    """Enumerate every rule and instance of a pipeline with its current plan state."""
    loaded = load_pipeline(project, pipeline_id)
    root = project.root
    state = State.load(project, pipeline_id)
    # compute_plan keys instances by binding.identity, and rule-level placeholders
    # (OFF / "no inputs yet") by the bare rule name.
    plan = {e.identity: e for e in compute_plan(project, loaded)}
    by_name = {r.name: r for r in loaded.rules}

    nodes: list[RuleNode] = []
    for name in execution_order(loaded):
        rule = by_name[name]
        deps = sorted(loaded.deps.get(name, set()))
        trigger = TriggerInfo(describe=rule.trigger.describe) if rule.trigger else None
        node = RuleNode(
            name=name,
            enabled=rule.enabled,
            deps=deps,
            has_feedback=bool(rule.feedback),
            trigger=trigger,
            state="OFF" if not rule.enabled else "PENDING",
            reason="disabled" if not rule.enabled else "no inputs yet",
        )
        if rule.enabled and rule.out:
            ports = list(rule.out)
            for binding in bind_rule(root, rule):
                entry = plan.get(binding.identity)
                outs = [_output_view(root, binding, p) for p in ports]
                concrete = [o for o in outs if not o.is_dir]
                primary = concrete[0] if concrete else None
                job = state.get(binding.identity)
                node.instances.append(
                    InstanceNode(
                        identity=binding.identity,
                        rule=name,
                        keys=binding.keys,
                        label=_label(name, binding.keys),
                        state=entry.action if entry else "PENDING",
                        reason=entry.reason if entry else "",
                        primary=primary,
                        outputs=outs,
                        channels=read_channels(root, rule, binding.keys) if rule.feedback else [],
                        reroll=rule.reroll,
                        completed_at=job.completed_at if job else None,
                    )
                )
            if node.instances:
                # rule-level rollup is informational; instances carry per-cell state.
                node.state = "OK"
                node.reason = f"{len(node.instances)} instance(s)"
        nodes.append(node)

    return Graph(pipeline=pipeline_id, rules=nodes)
