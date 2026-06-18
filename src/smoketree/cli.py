"""Smoketree command-line interface."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer

from . import cache as cachelib
from .errors import SmoketreeError
from .executor import compute_plan, run as run_graph
from .graph import load_graph
from .project import Project

app = typer.Typer(
    add_completion=False,
    help="Smoketree — a declarative pipeline tool for media transformation.",
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

    hints = {
        "minimal": "add a graph in graphs/ and a transformer in transformers/, then: "
        "smoketree validate",
        "demo": "smoketree run demo",
        "fanout": "smoketree run fanout",
        "tagged": "smoketree run tagged",
        "portrait": "add sources/subject.jpg, pull your ollama models, then: "
        "smoketree run portrait",
    }
    typer.echo(f"\nNext:  {hints.get(template, 'smoketree validate')}")


@app.command()
def validate(
    graph: Optional[str] = typer.Argument(None, help="Graph to validate (default: all)."),
) -> None:
    """Validate graph and transformer definitions (no execution)."""
    project = _project()
    graphs = [graph] if graph else project.list_graphs()
    if not graphs:
        typer.echo("No graphs found.")
        return
    ok = True
    for graph_id in graphs:
        try:
            resolved = load_graph(project, graph_id)
        except SmoketreeError as exc:
            ok = False
            typer.secho(f"[FAIL]  {graph_id}", fg=typer.colors.RED)
            typer.echo(f"        {exc}")
            continue
        order = " -> ".join(resolved.execution_order)
        typer.secho(f"[OK]    {graph_id}", fg=typer.colors.GREEN)
        typer.echo(f"        {order}")
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def plan(
    graph: str = typer.Argument(..., help="Graph to plan."),
    take: int = typer.Option(0, "--take", help="Take index."),
    node: Optional[str] = typer.Option(None, "--node", help="Plan only this node + deps."),
    force: bool = typer.Option(False, "--force", help="Treat all nodes as rebuilding."),
) -> None:
    """Show the execution plan (dry run)."""
    project = _project()
    try:
        resolved = load_graph(project, graph)
        entries = compute_plan(project, resolved, take, target_node=node, force=force)
    except SmoketreeError as exc:
        _fail(str(exc))
        return
    for entry in entries:
        colour = {
            "SKIP": typer.colors.BRIGHT_BLACK,
            "RUN": typer.colors.YELLOW,
            "PENDING": typer.colors.RED,
        }.get(entry.action, None)
        line = f"[{entry.action:<4}] {entry.node_id:<16}({entry.reason})"
        typer.secho(line, fg=colour)


@app.command()
def run(
    graph: str = typer.Argument(..., help="Graph to run."),
    take: int = typer.Option(0, "--take", help="Take index."),
    node: Optional[str] = typer.Option(None, "--node", help="Run only this node + deps."),
    force: bool = typer.Option(False, "--force", help="Ignore cache; re-run all nodes."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Alias for 'plan'."),
) -> None:
    """Run a graph."""
    project = _project()
    try:
        resolved = load_graph(project, graph)
        if dry_run:
            entries = compute_plan(project, resolved, take, target_node=node, force=force)
            for entry in entries:
                typer.echo(f"[{entry.action:<4}] {entry.node_id:<16}({entry.reason})")
            return
        run_graph(project, resolved, take, target_node=node, force=force,
                  report=typer.echo)
    except SmoketreeError as exc:
        _fail(str(exc))


@app.command()
def status(
    graph: Optional[str] = typer.Argument(None, help="Graph (default: all)."),
) -> None:
    """Show the state of the last run."""
    project = _project()
    graphs = [graph] if graph else project.list_graphs()
    for graph_id in graphs:
        state = cachelib.State.load(project, graph_id)
        if not state.nodes:
            typer.echo(f"{graph_id}: no recorded runs")
            continue
        typer.secho(f"{graph_id}:", bold=True)
        for node_id, instances in state.nodes.items():
            if len(instances) == 1:
                ns = next(iter(instances.values()))
                typer.echo(
                    f"  {node_id:<16} take={ns.take}  {ns.completed_at}  "
                    f"hash={ns.input_hash[:12]}"
                )
            else:
                typer.echo(f"  {node_id} ({len(instances)} instances)")
                for inst_hash, ns in instances.items():
                    typer.echo(
                        f"    {inst_hash}  take={ns.take}  {ns.completed_at}  "
                        f"hash={ns.input_hash[:12]}"
                    )


@app.command()
def inspect(
    graph: str = typer.Argument(..., help="Graph."),
    node: str = typer.Argument(..., help="Node id."),
    take: int = typer.Option(0, "--take", help="Take index."),
) -> None:
    """Inspect a node's scratch and cache directories (all takes/instances)."""
    project = _project()
    scratch = project.scratch_dir / graph / node
    cache = project.cache_dir / graph / node
    typer.secho(f"scratch: {scratch}", bold=True)
    _list_dir(scratch)
    typer.secho(f"cache:   {cache}", bold=True)
    _list_dir(cache)


def _list_dir(path: Path) -> None:
    if not path.exists():
        typer.echo("  (does not exist)")
        return
    entries = sorted(path.rglob("*"))
    if not entries:
        typer.echo("  (empty)")
        return
    for entry in entries:
        if entry.is_file():
            typer.echo(f"  {entry.relative_to(path)}  ({entry.stat().st_size} B)")


@app.command()
def purge(
    graph: str = typer.Argument(..., help="Graph."),
    node: Optional[str] = typer.Option(None, "--node", help="Limit to this node."),
    take: Optional[int] = typer.Option(None, "--take", help="Limit to this take."),
    scratch: bool = typer.Option(False, "--scratch", help="Purge scratch only."),
    cache: bool = typer.Option(False, "--cache", help="Purge cache only."),
) -> None:
    """Clear cache and/or scratch for a graph or node."""
    project = _project()
    # Default: purge both when neither flag is given.
    do_scratch = scratch or not (scratch or cache)
    do_cache = cache or not (scratch or cache)

    targets: list[Path] = []
    if do_cache:
        targets.append(_purge_root(project.cache_dir, graph, node, take))
    if do_scratch:
        targets.append(_purge_root(project.scratch_dir, graph, node, take))

    removed = 0
    for path in targets:
        if path.exists():
            shutil.rmtree(path)
            removed += 1
            typer.echo(f"  removed {path}")
    if removed == 0:
        typer.echo("Nothing to purge.")


def _purge_root(base: Path, graph: str, node: Optional[str], take: Optional[int]) -> Path:
    path = base / graph
    if node:
        path = path / node
        if take is not None:
            path = path / f"take_{take}"
    return path


if __name__ == "__main__":
    app()
