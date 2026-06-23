"""The PathTree evaluation engine: a fixpoint loop over rule bindings.

Each pass globs the tree, enumerates every runnable binding, runs the stale ones, and
records their input hashes. Re-globbing is what discovers a scatter rule's runtime
outputs and re-plans the next pass. The loop stops when a full pass produces nothing
new, or errors at ``defaults.max_iterations`` (the runaway-rule circuit breaker).
"""

from __future__ import annotations

import glob as globlib
import itertools
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .backends import ExecutionContext, get_backend
from .bind import Binding, Pattern, bind_rule
from .cache import State, hash_file, hash_text
from .errors import ExecutionError
from .project import Project
from .rules import LoadedPipeline

_SEED_PLACEHOLDER = "(no feedback yet)\n"

Reporter = Callable[[str], None]


@dataclass
class PlanEntry:
    identity: str
    action: str  # "RUN" | "SKIP" | "PENDING"
    reason: str


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #


def _input_hash(binding: Binding) -> str:
    parts: list[str] = []
    for name in sorted(binding.inputs):
        value = binding.inputs[name]
        paths = value if isinstance(value, list) else [value]
        for path in sorted(paths):
            parts.append(f"{name}:{hash_file(path)}")
    parts.append(f"transform:{hash_text(binding.transform_fingerprint)}")
    return hash_text("\n".join(parts))


def _known_key_values(root: Path, loaded: LoadedPipeline) -> dict[str, set[str]]:
    """Every value seen for each ``{key}`` by globbing all rules' input patterns.

    This is how feedback seeding learns its key-tuples without depending on the output
    rule running (which would deadlock the feedback loop): the briefs that supply, e.g.,
    ``{mutant}`` are themselves inputs to other rules.
    """
    values: dict[str, set[str]] = {}
    for rule in loaded.rules:
        if not rule.enabled:
            continue
        for pat_str in rule.in_.values():
            pat = Pattern.compile(pat_str)
            if not pat.keys:
                continue
            for rel in globlib.glob(pat.glob_str, root_dir=str(root), recursive=True):
                m = pat.regex.match(rel.replace("\\", "/"))
                if m:
                    for k, v in m.groupdict().items():
                        values.setdefault(k, set()).add(v)
    return values


def _seed_feedback(project: Project, loaded: LoadedPipeline, report: Reporter) -> None:
    """Seed each rule's ``feedback.append`` file for every discovered key-tuple.

    Seeded once (when absent) with a placeholder; an existing file is never clobbered,
    so human notes survive. Deleting a feedback file re-seeds it on the next pass.
    """
    known = _known_key_values(project.root, loaded)
    for rule in loaded.rules:
        if rule.feedback is None or not rule.enabled:
            continue
        pat = Pattern.compile(rule.feedback.append)
        if any(k not in known for k in pat.keys):
            continue  # no source for these keys yet
        value_sets = [sorted(known[k]) for k in pat.keys]
        for combo in itertools.product(*value_sets):
            keys = dict(zip(pat.keys, combo))
            path = project.root / pat.fill(keys)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(_SEED_PLACEHOLDER)
                report(f"  seed   {path}")


def _inputs_present(binding: Binding) -> bool:
    """Whether every resolved input still exists on disk.

    A binding is enumerated at the start of a pass; an earlier rule in the same pass may
    have pruned its inputs out from under it (a vanished scatter key). Such a binding is
    skipped — it simply won't be re-enumerated next pass.
    """
    for value in binding.inputs.values():
        paths = value if isinstance(value, list) else [value]
        if not all(p.is_file() for p in paths):
            return False
    return True


def _staleness(binding: Binding, state: State) -> tuple[bool, str]:
    """Return (is_stale, reason)."""
    record = state.get(binding.identity)
    if record is None:
        return True, "new"
    missing = [p for p in binding.enumerable_outputs if not p.exists()]
    if missing:
        return True, "output missing"
    if record.input_hash != _input_hash(binding):
        return True, "inputs changed"
    return False, "up to date"


# --------------------------------------------------------------------------- #
# Execution of a single binding
# --------------------------------------------------------------------------- #


def _execute(project: Project, binding: Binding, report: Reporter) -> None:
    for path in binding.enumerable_outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    for owned in binding.owned_prefixes:
        owned.mkdir(parents=True, exist_ok=True)

    ctx = ExecutionContext(
        project=project,
        rule_name=binding.rule.name,
        keys=binding.keys,
        inputs=binding.inputs,
        outputs=binding.outputs,
        command=binding.command,
        config=binding.rule.config,
        seed=int(hash_text(binding.identity), 16) % (2**32),
        env=dict(project.config.env),
    )
    backend = get_backend(binding.rule.backend)
    backend.execute(ctx)

    missing = [p for p in binding.enumerable_outputs if not p.exists()]
    if missing:
        raise ExecutionError(
            f"Rule '{binding.rule.name}' ({binding.identity}) did not produce its "
            f"declared output(s): {', '.join(str(p) for p in missing)}.\n"
            f"  $ {binding.command}"
        )


def _newest_mtime(path: Path) -> float:
    if path.is_file():
        return path.stat().st_mtime
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        if child.is_file():
            newest = max(newest, child.stat().st_mtime)
    return newest


def _prune(binding: Binding, run_start: float, report: Reporter) -> None:
    """Delete managed, key-scoped children under the owned prefix that vanished."""
    for owned in binding.owned_prefixes:
        if not owned.is_dir():
            continue
        for child in sorted(owned.iterdir()):
            if _newest_mtime(child) < run_start:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                report(f"  prune  {child}")


# --------------------------------------------------------------------------- #
# Fixpoint loop
# --------------------------------------------------------------------------- #


def run(
    project: Project,
    loaded: LoadedPipeline,
    *,
    force: bool = False,
    report: Reporter = lambda _: None,
) -> int:
    """Run the pipeline to fixpoint. Returns the number of jobs executed."""
    state = State.load(project, loaded.id)
    if force:
        state.clear()

    max_iter = project.config.defaults.max_iterations
    executed = 0
    for iteration in range(1, max_iter + 1):
        _seed_feedback(project, loaded, report)
        bindings = [
            b
            for rule in loaded.rules
            if rule.enabled
            for b in bind_rule(project.root, rule)
        ]
        progressed = False
        for binding in bindings:
            if not _inputs_present(binding):
                continue  # inputs pruned earlier this pass; re-evaluated next pass
            stale, reason = _staleness(binding, state)
            if not stale:
                continue
            report(f"[run ] {binding.identity}  ({reason})")
            run_start = time.time()
            _execute(project, binding, report)
            state.record(binding.identity, _input_hash(binding))
            # Persist after every job: an expensive run that fails partway (a paid
            # render, a throttle) must not lose — or re-bill — the work already done.
            state.save()
            if binding.rule.prune:
                _prune(binding, run_start, report)
            executed += 1
            progressed = True
        if not progressed:
            break
    else:
        raise ExecutionError(
            f"Pipeline '{loaded.id}' did not converge in {max_iter} passes — a rule is "
            f"likely producing inputs for itself. Inspect rules that both read and write "
            f"the same path axis."
        )

    state.save()
    if executed == 0:
        report("Nothing to do — all outputs up to date.")
    return executed


def compute_plan(project: Project, loaded: LoadedPipeline, *, force: bool = False) -> list[PlanEntry]:
    """A single-pass dry run: current runnable bindings + rules still waiting on inputs."""
    state = State.load(project, loaded.id)
    entries: list[PlanEntry] = []
    for rule in loaded.rules:
        if not rule.enabled:
            entries.append(PlanEntry(rule.name, "OFF", "disabled"))
            continue
        bindings = bind_rule(project.root, rule)
        if not bindings:
            entries.append(PlanEntry(rule.name, "PENDING", "no inputs yet"))
            continue
        for binding in bindings:
            if force:
                entries.append(PlanEntry(binding.identity, "RUN", "forced"))
                continue
            stale, reason = _staleness(binding, state)
            entries.append(
                PlanEntry(binding.identity, "RUN" if stale else "SKIP", reason)
            )
    return entries
