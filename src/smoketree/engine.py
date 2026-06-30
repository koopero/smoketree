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

import jsonschema

from .backends import ExecutionContext, get_backend
from .bind import Binding, Pattern, bind_rule, template_pattern
from .cache import State, hash_file, hash_text
from .errors import ExecutionError
from .loader import load_yaml
from .project import Project
from .rules import LoadedPipeline
from .serde import load_data

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


def _roll_path(binding: Binding) -> Path | None:
    """The re-roll counter sidecar beside a reroll rule's primary output (or None)."""
    if not binding.rule.reroll or not binding.outputs:
        return None
    primary = next(iter(binding.outputs.values()))
    return primary.with_name(primary.name + ".roll")


def _roll_value(binding: Binding) -> int:
    """Current re-roll count for a binding (0 when unset — equivalent to no re-roll)."""
    path = _roll_path(binding)
    if path is None or not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or "0")
    except ValueError:
        return 0


def bump_roll(binding: Binding) -> int:
    """Increment a binding's re-roll counter — re-renders that cell with a fresh seed."""
    path = _roll_path(binding)
    if path is None:
        raise ExecutionError(
            f"Rule '{binding.rule.name}' is not a re-roll rule (set reroll: true)."
        )
    n = _roll_value(binding) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{n}\n")
    return n


def _input_hash(binding: Binding) -> str:
    parts: list[str] = []
    for name in sorted(binding.inputs):
        value = binding.inputs[name]
        paths = value if isinstance(value, list) else [value]
        for path in sorted(paths):
            parts.append(f"{name}:{hash_file(path)}")
    for port in sorted(binding.schemas):
        path = binding.schemas[port]
        parts.append(f"schema:{port}:{hash_file(path) if path.exists() else 'missing'}")
    if binding.rule.reroll:
        parts.append(f"roll:{_roll_value(binding)}")
    parts.append(f"transform:{hash_text(binding.transform_fingerprint)}")
    return hash_text("\n".join(parts))


def _input_fingerprint(binding: Binding) -> str:
    """A cheap mtime+size fingerprint of the inputs plus the transform text.

    Machine-local and not a correctness authority — it only decides whether the
    content hash is worth recomputing. A miss falls through to the content hash;
    a match skips re-reading (potentially large) input files.
    """
    parts: list[str] = []
    for name in sorted(binding.inputs):
        value = binding.inputs[name]
        paths = value if isinstance(value, list) else [value]
        for path in sorted(paths):
            try:
                st = path.stat()
                parts.append(f"{name}:{path}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{name}:{path}:missing")
    for port in sorted(binding.schemas):
        path = binding.schemas[port]
        try:
            st = path.stat()
            parts.append(f"schema:{port}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"schema:{port}:missing")
    if binding.rule.reroll:
        parts.append(f"roll:{_roll_value(binding)}")
    parts.append(f"transform:{binding.transform_fingerprint}")
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


def _channel_seed(channel) -> str:
    """Initial content for a feedback channel file (never overwrites an existing one)."""
    if channel.kind == "select":
        lines: list[str] = []
        if channel.describe:
            lines.append(f"# {channel.describe}")
        lines.append(f"# options: {' | '.join(channel.options)}")
        lines.append(f"{channel.name}: {channel.default}")
        return "\n".join(lines) + "\n"
    return _SEED_PLACEHOLDER  # notes


def _seed_feedback(project: Project, loaded: LoadedPipeline, report: Reporter) -> None:
    """Seed each rule's feedback channel files for every discovered key-tuple.

    Seeded once (when absent) — notes with a placeholder, select with its default; an
    existing file is never clobbered, so human edits survive. Deleting a channel file
    re-seeds it on the next pass.
    """
    known = _known_key_values(project.root, loaded)
    for rule in loaded.rules:
        if not rule.enabled or not rule.feedback:
            continue
        for channel in rule.feedback:
            pat = Pattern.compile(channel.path)
            if any(k not in known for k in pat.keys):
                continue  # no source for these keys yet
            value_sets = [sorted(known[k]) for k in pat.keys]
            for combo in itertools.product(*value_sets):
                keys = dict(zip(pat.keys, combo))
                path = project.root / pat.fill(keys)
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(_channel_seed(channel))
                    report(f"  seed   {path}")


def _seed_authored(project: Project, loaded: LoadedPipeline, report: Reporter) -> None:
    """Courtesy-copy each authored output's template to its authored copy when absent.

    The generator writes ``name.template.ext`` (a managed output); the authored copy
    ``name.ext`` is seeded once from it and thereafter human-owned — never clobbered, and
    the file downstream consumes. Deleting the authored copy re-seeds it next pass.
    """
    root = project.root
    base_root = project.forkbase_root / loaded.id
    for rule in loaded.rules:
        if not rule.enabled or not rule.author:
            continue
        for port in rule.author:
            decl_pat = Pattern.compile(rule.out[port])
            tpl_pat = Pattern.compile(template_pattern(rule.out[port]))
            for rel in globlib.glob(tpl_pat.glob_str, root_dir=str(root), recursive=True):
                rel = rel.replace("\\", "/")
                m = tpl_pat.regex.match(rel)
                if not m:
                    continue
                template = root / rel
                if not template.is_file():
                    continue
                authored_rel = decl_pat.fill(m.groupdict())
                authored = root / authored_rel
                if not authored.exists():
                    authored.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(template, authored)
                    # Record the fork-base (template content at copy time) for reconcile.
                    base = base_root / authored_rel
                    base.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(template, base)
                    report(f"  author {authored}")


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


def _staleness(binding: Binding, state: State, exist_only: bool = False) -> tuple[bool, str]:
    """Return (is_stale, reason).

    A cheap mtime+size fingerprint gates the content hash: an unchanged fingerprint
    means up-to-date without re-reading inputs. A moved fingerprint falls through to
    the content hash — if the contents are in fact identical (e.g. a touched file),
    the stored fingerprint is refreshed so the next pass is cheap again.

    With ``exist_only`` the recorded state and input hashes are ignored entirely: a binding
    is up to date iff all its (enumerable) outputs already exist on disk — i.e. run only what
    is missing, regardless of whether inputs changed.
    """
    if exist_only:
        missing = [p for p in binding.enumerable_outputs if not p.exists()]
        return (True, "output missing") if missing else (False, "exists")
    record = state.get(binding.identity)
    if record is None:
        return True, "new"
    missing = [p for p in binding.enumerable_outputs if not p.exists()]
    if missing:
        return True, "output missing"
    fingerprint = _input_fingerprint(binding)
    if record.fingerprint and record.fingerprint == fingerprint:
        return False, "up to date"
    if record.input_hash == _input_hash(binding):
        state.touch_fingerprint(binding.identity, fingerprint)
        return False, "up to date"
    return True, "inputs changed"


# --------------------------------------------------------------------------- #
# Execution of a single binding
# --------------------------------------------------------------------------- #


def _load_schemas(binding: Binding) -> dict[str, dict]:
    """Resolve each declared schema file to a dict (authored YAML -> JSON Schema)."""
    schemas: dict[str, dict] = {}
    for port, path in binding.schemas.items():
        if not path.exists():
            raise ExecutionError(
                f"Rule '{binding.rule.name}': schema for port '{port}' not found: {path}."
            )
        schemas[port] = load_yaml(path)
    return schemas


def _validate_port(rule_name: str, port: str, path: Path, schema: dict) -> None:
    """Validate a data file against its port's schema; hard-error on mismatch."""
    try:
        jsonschema.validate(load_data(path), schema)
    except jsonschema.ValidationError as exc:
        raise ExecutionError(
            f"Rule '{rule_name}': port '{port}' ({path}) failed schema validation: "
            f"{exc.message} (at {'/'.join(str(p) for p in exc.absolute_path) or '<root>'})."
        ) from exc
    except jsonschema.SchemaError as exc:
        raise ExecutionError(
            f"Rule '{rule_name}': schema for port '{port}' is itself invalid: {exc.message}."
        ) from exc


def _execute(project: Project, binding: Binding, report: Reporter) -> None:
    schemas = _load_schemas(binding)

    # Validate schema'd inputs before running — the contract this rule consumes.
    for port, schema in schemas.items():
        value = binding.inputs.get(port)
        if value is None:
            continue
        for path in value if isinstance(value, list) else [value]:
            _validate_port(binding.rule.name, port, path, schema)

    for path in binding.enumerable_outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    for owned in binding.owned_prefixes:
        owned.mkdir(parents=True, exist_ok=True)

    # Re-roll folds into the seed: roll 0 reproduces the bare-identity seed (no churn),
    # roll N gives a fresh but still-deterministic seed. Inert for seedless backends.
    roll = _roll_value(binding)
    seed_basis = binding.identity if roll == 0 else f"{binding.identity}#{roll}"

    ctx = ExecutionContext(
        project=project,
        rule_name=binding.rule.name,
        keys=binding.keys,
        inputs=binding.inputs,
        outputs=binding.outputs,
        out_patterns=dict(binding.rule.out),
        command=binding.command,
        config=binding.rule.config,
        schemas=schemas,
        context=binding.context,
        seed=int(hash_text(seed_basis), 16) % (2**32),
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

    # Validate schema'd outputs after running — the contract this rule produces.
    for port, schema in schemas.items():
        path = binding.outputs.get(port)
        if path is not None and path.is_file():
            _validate_port(binding.rule.name, port, path, schema)


def _filter_pass(binding: Binding) -> bool:
    """Whether ``binding`` passes its rule's filter predicate (reads an input data file).

    The predicate input may be a regular input or an ambient ``context`` input — the latter
    lets a non-shell rule gate on a data field without also feeding that file to its backend
    (e.g. a replicate rule routed by a sibling ``clip_plan.json`` it must not send to the model).
    """
    spec = binding.rule.filter
    value = binding.inputs.get(spec.input)
    if value is None:
        ctx = binding.context.get(spec.input)
        value = ctx[0] if ctx else None
    if not isinstance(value, Path) or not value.is_file():
        return False
    data = load_data(value)
    if spec.field is not None:
        actual = data.get(spec.field) if isinstance(data, dict) else None
    else:
        actual = data
    return spec.matches(actual)


def _drop(binding: Binding, state: State, report: Reporter) -> bool:
    """Remove a filtered-out binding's managed output(s) and forget its record.

    Returns whether anything was removed on disk (so the caller can re-plan downstream).
    """
    removed = False
    for path in binding.enumerable_outputs:
        if path.exists() or path.is_symlink():
            path.unlink()
            report(f"  drop   {path}")
            removed = True
    for owned in binding.owned_prefixes:
        if owned.is_dir():
            shutil.rmtree(owned)
            report(f"  drop   {owned}")
            removed = True
    state.discard(binding.identity)
    return removed


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


def _check_only(loaded: LoadedPipeline, only: set[str] | None) -> None:
    if only is not None:
        unknown = only - {r.name for r in loaded.rules}
        if unknown:
            raise ExecutionError(
                f"No such rule(s) in '{loaded.id}': {', '.join(sorted(unknown))}. "
                f"Rules: {', '.join(r.name for r in loaded.rules)}."
            )


def _where_match(binding: Binding, where: dict[str, str] | None) -> bool:
    return where is None or all(binding.keys.get(k) == v for k, v in where.items())


def run(
    project: Project,
    loaded: LoadedPipeline,
    *,
    force: bool = False,
    only: set[str] | None = None,
    where: dict[str, str] | None = None,
    exist_only: bool = False,
    report: Reporter = lambda _: None,
) -> int:
    """Run the pipeline to fixpoint. Returns the number of jobs executed.

    ``only`` restricts to the named rules; ``where`` restricts to bindings whose keys
    match every ``key=value`` (a binding lacking a named key is excluded).
    """
    _check_only(loaded, only)
    state = State.load(project, loaded.id)
    if force:
        state.clear()

    max_iter = project.config.defaults.max_iterations
    executed = 0
    for iteration in range(1, max_iter + 1):
        _seed_feedback(project, loaded, report)
        _seed_authored(project, loaded, report)
        bindings = [
            b
            for rule in loaded.rules
            if rule.enabled and (only is None or rule.name in only)
            for b in bind_rule(project.root, rule)
            if _where_match(b, where)
        ]
        progressed = False
        for binding in bindings:
            if not _inputs_present(binding):
                continue  # inputs pruned earlier this pass; re-evaluated next pass
            if binding.rule.filter is not None and not _filter_pass(binding):
                # Filtered out: keep the managed set in sync by dropping any output this
                # binding produced before (e.g. an idea un-approved). Removing something
                # is progress — downstream that globbed it must re-plan.
                if _drop(binding, state, report):
                    state.save()
                    progressed = True
                continue
            stale, reason = _staleness(binding, state, exist_only)
            if not stale:
                continue
            report(f"[run ] {binding.identity}  ({reason})")
            run_start = time.time()
            _execute(project, binding, report)
            state.record(
                binding.identity, _input_hash(binding), _input_fingerprint(binding)
            )
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


def compute_plan(
    project: Project,
    loaded: LoadedPipeline,
    *,
    force: bool = False,
    only: set[str] | None = None,
    where: dict[str, str] | None = None,
    exist_only: bool = False,
) -> list[PlanEntry]:
    """A single-pass dry run: current runnable bindings + rules still waiting on inputs."""
    _check_only(loaded, only)
    state = State.load(project, loaded.id)
    entries: list[PlanEntry] = []
    for rule in loaded.rules:
        if only is not None and rule.name not in only:
            continue
        if not rule.enabled:
            entries.append(PlanEntry(rule.name, "OFF", "disabled"))
            continue
        bindings = [b for b in bind_rule(project.root, rule) if _where_match(b, where)]
        if not bindings:
            entries.append(PlanEntry(rule.name, "PENDING", "no inputs yet"))
            continue
        for binding in bindings:
            if rule.filter is not None and (
                not _inputs_present(binding) or not _filter_pass(binding)
            ):
                entries.append(PlanEntry(binding.identity, "DROP", "filtered out"))
                continue
            if force:
                entries.append(PlanEntry(binding.identity, "RUN", "forced"))
                continue
            stale, reason = _staleness(binding, state, exist_only)
            entries.append(
                PlanEntry(binding.identity, "RUN" if stale else "SKIP", reason)
            )
    return entries
