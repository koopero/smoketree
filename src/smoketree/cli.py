"""Smoketree command-line interface (PathTree core)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer

from . import cache as cachelib
from . import engine as enginelib
from .bind import bind_rule
from .errors import SmoketreeError
from .project import Project
from .rules import execution_order, load_pipeline

app = typer.Typer(
    add_completion=False,
    help="Smoketree — a path-based pipeline tool for media transformation.",
    no_args_is_help=True,
)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _project() -> Project:
    try:
        return Project.discover()
    except SmoketreeError as exc:
        _fail(str(exc))
        raise  # unreachable


def _targets(rule: list[str], where: list[str]) -> tuple[set[str] | None, dict[str, str] | None]:
    """Parse --rule / --where selectors into engine arguments."""
    only = set(rule) or None
    pairs: dict[str, str] = {}
    for item in where:
        if "=" not in item:
            _fail(f"--where must be KEY=VALUE, got '{item}'.")
        key, _, value = item.partition("=")
        pairs[key.strip()] = value.strip()
    return only, (pairs or None)


# --------------------------------------------------------------------------- #


@app.command()
def init(
    name: Optional[str] = typer.Option(None, "--name", help="Project name."),
    template: str = typer.Option(
        "minimal", "--template", "-t", help="Starter template (see --list)."
    ),
    list_: bool = typer.Option(False, "--list", help="List available templates and exit."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing scaffold."),
) -> None:
    """Initialise a new project in the current directory from a starter template."""
    from .scaffold import init_project, list_templates

    if list_:
        typer.secho("Available templates:", bold=True)
        for tname, desc in list_templates().items():
            typer.echo(f"  {tname:<10} {desc}")
        return

    root = Path.cwd()
    project_name = name or root.name
    try:
        created = init_project(root, project_name, template=template, force=force)
    except SmoketreeError as exc:
        _fail(str(exc))
        return
    typer.secho(
        f"Initialised '{project_name}' ({template} template) in {root}",
        fg=typer.colors.GREEN,
    )
    for path in created:
        typer.echo(f"  + {path.relative_to(root)}")
    nxt = "smoketree run demo" if template == "demo" else "add a pipeline in graphs/"
    typer.echo(f"\nNext:  {nxt}")


@app.command()
def validate(
    pipeline: Optional[str] = typer.Argument(None, help="Pipeline (default: all)."),
) -> None:
    """Validate pipeline definitions (no execution)."""
    project = _project()
    ids = [pipeline] if pipeline else project.list_graphs()
    if not ids:
        typer.echo("No pipelines found.")
        return
    ok = True
    for pid in ids:
        try:
            loaded = load_pipeline(project, pid)
        except SmoketreeError as exc:
            ok = False
            typer.secho(f"[FAIL]  {pid}", fg=typer.colors.RED)
            typer.echo(f"        {exc}")
            continue
        typer.secho(f"[OK]    {pid}", fg=typer.colors.GREEN)
        typer.echo(f"        {' -> '.join(execution_order(loaded))}")
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def plan(
    pipeline: str = typer.Argument(..., help="Pipeline to plan."),
    force: bool = typer.Option(False, "--force", help="Treat all jobs as rebuilding."),
    rule: list[str] = typer.Option([], "--rule", "-r", help="Only this rule (repeatable)."),
    where: list[str] = typer.Option(
        [], "--where", "-w", help="Only bindings with KEY=VALUE (repeatable)."
    ),
) -> None:
    """Show the current execution plan (single-pass dry run)."""
    project = _project()
    only, sel = _targets(rule, where)
    try:
        loaded = load_pipeline(project, pipeline)
        entries = enginelib.compute_plan(project, loaded, force=force, only=only, where=sel)
    except SmoketreeError as exc:
        _fail(str(exc))
        return
    for entry in entries:
        colour = {
            "SKIP": typer.colors.BRIGHT_BLACK,
            "OFF": typer.colors.BRIGHT_BLACK,
            "DROP": typer.colors.BRIGHT_BLACK,
            "RUN": typer.colors.YELLOW,
            "PENDING": typer.colors.RED,
        }.get(entry.action)
        typer.secho(f"[{entry.action:<7}] {entry.identity}  ({entry.reason})", fg=colour)


@app.command()
def run(
    pipeline: str = typer.Argument(..., help="Pipeline to run."),
    force: bool = typer.Option(False, "--force", help="Ignore cache; re-run all jobs."),
    rule: list[str] = typer.Option([], "--rule", "-r", help="Only this rule (repeatable)."),
    where: list[str] = typer.Option(
        [], "--where", "-w", help="Only bindings with KEY=VALUE (repeatable)."
    ),
) -> None:
    """Run a pipeline to fixpoint (optionally narrowed to --rule / --where)."""
    project = _project()
    only, sel = _targets(rule, where)
    try:
        loaded = load_pipeline(project, pipeline)
        executed = enginelib.run(
            project, loaded, force=force, only=only, where=sel, report=typer.echo
        )
    except SmoketreeError as exc:
        _fail(str(exc))
        return
    typer.secho(f"Done — {executed} job(s) executed.", fg=typer.colors.GREEN)


@app.command()
def reroll(
    pipeline: str = typer.Argument(..., help="Pipeline to re-roll."),
    rule: list[str] = typer.Option([], "--rule", "-r", help="Only this rule (repeatable)."),
    where: list[str] = typer.Option(
        [], "--where", "-w", help="Only bindings with KEY=VALUE (repeatable)."
    ),
) -> None:
    """Re-roll matched cells: bump each one's counter (a fresh seed) and re-render.

    Only rules with `reroll: true` are eligible. Narrow with --rule / --where, e.g.
    `smoketree reroll g -w idea=sunset`.
    """
    project = _project()
    only, sel = _targets(rule, where)
    try:
        loaded = load_pipeline(project, pipeline)
    except SmoketreeError as exc:
        _fail(str(exc))
        return

    bumped = 0
    for r in loaded.rules:
        if not r.reroll or (only is not None and r.name not in only):
            continue
        for binding in bind_rule(project.root, r):
            if sel is not None and not all(binding.keys.get(k) == v for k, v in sel.items()):
                continue
            n = enginelib.bump_roll(binding)
            typer.echo(f"  reroll {binding.identity} -> {n}")
            bumped += 1

    if not bumped:
        typer.secho(
            "Nothing to re-roll (matched no cells of a rule with reroll: true).",
            fg=typer.colors.YELLOW,
        )
        return
    try:
        enginelib.run(project, loaded, report=typer.echo)
    except SmoketreeError as exc:
        _fail(str(exc))
        return
    typer.secho("Re-rolled.", fg=typer.colors.GREEN)


@app.command()
def status(
    pipeline: Optional[str] = typer.Argument(None, help="Pipeline (default: all)."),
) -> None:
    """Show the state of the last run."""
    project = _project()
    ids = [pipeline] if pipeline else project.list_graphs()
    for pid in ids:
        state = cachelib.State.load(project, pid)
        if not state.jobs:
            typer.echo(f"{pid}: no recorded runs")
            continue
        typer.secho(f"{pid}:", bold=True)
        for identity, js in sorted(state.jobs.items()):
            typer.echo(f"  {identity:<40} {js.completed_at}  hash={js.input_hash[:12]}")


@app.command()
def workspace(
    pipeline: str = typer.Argument(..., help="Pipeline to review."),
    port: int = typer.Option(8765, "--port", help="Port to serve on."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open a browser."),
) -> None:
    """Open the human-in-the-loop feedback workspace for a built pipeline.

    Shows every rendered output whose rule declares one or more `feedback` channels and lets
    a human record feedback (notes or a selection), which is saved to the channel file and
    folded back in on the next run.
    """
    project = _project()
    try:
        from .workspace import build_index
        from .workspace.server import serve

        if not build_index(project, pipeline):
            typer.secho(
                f"No reviewable outputs for '{pipeline}'. Run it first "
                f"(smoketree run {pipeline}); render rules need a 'feedback:' channel.",
                fg=typer.colors.YELLOW,
            )
        serve(project, pipeline, host=host, port=port, open_browser=not no_open)
    except SmoketreeError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:  # pragma: no cover
        typer.echo("\nstopped.")


@app.command()
def reconcile(
    pipeline: str = typer.Argument(..., help="Pipeline."),
    merge: bool = typer.Option(False, "--merge", help="3-way merge generated into your copy."),
    take_generated: bool = typer.Option(
        False, "--take-generated", help="Replace your copy with the generated template."
    ),
    keep_mine: bool = typer.Option(False, "--keep-mine", help="Keep your copy; dismiss drift."),
    rule: list[str] = typer.Option([], "--rule", "-r", help="Only this rule (repeatable)."),
    where: list[str] = typer.Option(
        [], "--where", "-w", help="Only bindings with KEY=VALUE (repeatable)."
    ),
) -> None:
    """Show (or resolve) authored copies whose generated template has drifted.

    With no action flag, lists the drift. Pass exactly one of --merge / --take-generated /
    --keep-mine to resolve every drifted copy (optionally narrowed by --rule / --where).
    """
    project = _project()
    only, sel = _targets(rule, where)
    actions = [a for a, on in (("merge", merge), ("take-generated", take_generated),
                               ("keep-mine", keep_mine)) if on]
    if len(actions) > 1:
        _fail("pass at most one of --merge / --take-generated / --keep-mine.")
    try:
        from . import reconcile as reconcilelib

        loaded = load_pipeline(project, pipeline)
        drifts = [
            d for d in reconcilelib.find_drift(project, loaded)
            if (only is None or d.rule in only)
            and (sel is None or all(d.keys.get(k) == v for k, v in sel.items()))
        ]
    except SmoketreeError as exc:
        _fail(str(exc))
        return

    if not drifts:
        typer.secho("No drift — every authored copy is up to date with its template.",
                    fg=typer.colors.GREEN)
        return

    if not actions:
        typer.secho(f"{len(drifts)} authored cop(ies) have drifted:", bold=True)
        for d in drifts:
            edited = " (you edited it)" if d.copy_edited else ""
            typer.echo(f"  {d.authored}{edited}")
        typer.echo("\nResolve with --merge, --take-generated, or --keep-mine.")
        return

    action = actions[0]
    for d in drifts:
        try:
            status = reconcilelib.resolve(d, action)
        except SmoketreeError as exc:
            typer.secho(f"  {d.authored}: {exc}", fg=typer.colors.RED)
            continue
        typer.echo(f"  {d.authored}: {status}")
    typer.secho("Done.", fg=typer.colors.GREEN)


@app.command()
def purge(
    pipeline: str = typer.Argument(..., help="Pipeline."),
) -> None:
    """Delete a pipeline's managed outputs and recorded state."""
    project = _project()
    try:
        loaded = load_pipeline(project, pipeline)
    except SmoketreeError as exc:
        _fail(str(exc))
        return

    targets: set[Path] = set()
    for rule in loaded.rules:
        for binding in bind_rule(project.root, rule):
            targets.update(binding.enumerable_outputs)
            targets.update(binding.owned_prefixes)

    removed = 0
    for path in sorted(targets):
        if path.is_dir():
            shutil.rmtree(path)
            removed += 1
            typer.echo(f"  removed {path}")
        elif path.exists():
            path.unlink()
            removed += 1
            typer.echo(f"  removed {path}")

    state_path = project.state_dir / f"{pipeline}.json"
    if state_path.exists():
        state_path.unlink()
    forkbase = project.forkbase_root / pipeline
    if forkbase.is_dir():
        shutil.rmtree(forkbase)
        typer.echo(f"  removed {state_path}")
        removed += 1

    if removed == 0:
        typer.echo("Nothing to purge.")


if __name__ == "__main__":
    app()
