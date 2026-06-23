"""Tests for the PathTree core: binding, the fixpoint engine, staleness, and prune."""

from __future__ import annotations

from pathlib import Path

import pytest

from smoketree import engine as enginelib
from smoketree.bind import Pattern, bind_rule
from smoketree.errors import ExecutionError, SmoketreeError, ValidationError
from smoketree.loader import substitute_env
from smoketree.models import Pipeline, Rule
from smoketree.project import Project
from smoketree.rules import infer_dependencies, load_pipeline
from smoketree.scaffold import init_project


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_project(tmp_path: Path, pipeline_yaml: str, name: str = "p") -> Project:
    init_project(tmp_path, name, template="minimal")
    (tmp_path / "graphs").mkdir(exist_ok=True)
    (tmp_path / "graphs" / "g.yaml").write_text(pipeline_yaml)
    return Project(tmp_path)


def write(root: Path, rel: str, content: str = "x\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def run(project: Project) -> int:
    loaded = load_pipeline(project, "g")
    return enginelib.run(project, loaded)


def rule(name, in_, out, run, prune=False) -> Rule:
    return Rule(name=name, **{"in": in_}, out=out, run=run, prune=prune)


# --------------------------------------------------------------------------- #
# Pattern compilation
# --------------------------------------------------------------------------- #


def test_pattern_keys_and_glob_classification():
    scalar = Pattern.compile("a/{x}/{y}/f.txt")
    assert scalar.keys == ["x", "y"]
    assert scalar.has_glob is False

    listy = Pattern.compile("a/{x}/seg/*/f.txt")
    assert listy.keys == ["x"]
    assert listy.has_glob is True


def test_pattern_fill_and_regex():
    pat = Pattern.compile("a/{x}/f.txt")
    assert pat.fill({"x": "ep1"}) == "a/ep1/f.txt"
    m = pat.regex.match("a/ep1/f.txt")
    assert m and m.group("x") == "ep1"
    assert pat.regex.match("a/ep1/sub/f.txt") is None  # {x} is one segment


def test_pattern_duplicate_key_rejected():
    with pytest.raises(ValidationError):
        Pattern.compile("a/{x}/{x}/f.txt")


# --------------------------------------------------------------------------- #
# Binding: map, product, join, pool, scatter
# --------------------------------------------------------------------------- #


def test_bind_map_one_job_per_key(tmp_path: Path):
    write(tmp_path, "in/a/f.txt")
    write(tmp_path, "in/b/f.txt")
    r = rule("r", {"src": "in/{k}/f.txt"}, {"dst": "out/{k}/f.txt"}, "cp {src} {dst}")
    bindings = bind_rule(tmp_path, r)
    assert {b.keys["k"] for b in bindings} == {"a", "b"}
    assert all(isinstance(b.inputs["src"], Path) for b in bindings)


def test_bind_cross_product(tmp_path: Path):
    write(tmp_path, "city/tokyo.txt")
    write(tmp_path, "city/osaka.txt")
    write(tmp_path, "kaiju/godzilla.txt")
    r = rule("r", {"c": "city/{city}.txt", "k": "kaiju/{kaiju}.txt"},
             {"o": "out/{city}/{kaiju}.txt"}, "cat {c} {k} > {o}")
    bindings = bind_rule(tmp_path, r)
    assert {(b.keys["city"], b.keys["kaiju"]) for b in bindings} == {
        ("tokyo", "godzilla"), ("osaka", "godzilla"),
    }


def test_bind_shared_key_joins(tmp_path: Path):
    write(tmp_path, "a/ep1.txt")
    write(tmp_path, "a/ep2.txt")
    write(tmp_path, "b/ep1.txt")  # ep2 missing on the b side
    r = rule("r", {"x": "a/{ep}.txt", "y": "b/{ep}.txt"}, {"o": "out/{ep}.txt"},
             "cat {x} {y} > {o}")
    bindings = bind_rule(tmp_path, r)
    assert {b.keys["ep"] for b in bindings} == {"ep1"}  # only the aligned pair


def test_bind_pool_collapses_glob_axis(tmp_path: Path):
    write(tmp_path, "ep/e1/seg/00/x.txt")
    write(tmp_path, "ep/e1/seg/01/x.txt")
    write(tmp_path, "ep/e2/seg/00/x.txt")
    r = rule("r", {"parts": "ep/{ep}/seg/*/x.txt"}, {"o": "ep/{ep}/sum.txt"},
             "cat {parts} > {o}")
    bindings = bind_rule(tmp_path, r)
    by_ep = {b.keys["ep"]: b for b in bindings}
    assert set(by_ep) == {"e1", "e2"}
    assert isinstance(by_ep["e1"].inputs["parts"], list)
    assert len(by_ep["e1"].inputs["parts"]) == 2


def test_bind_missing_input_yields_no_binding(tmp_path: Path):
    r = rule("r", {"src": "in/{k}/f.txt"}, {"dst": "out/{k}.txt"}, "cp {src} {dst}")
    assert bind_rule(tmp_path, r) == []


def test_bind_scatter_resolves_owned_prefix(tmp_path: Path):
    write(tmp_path, "src/e1.txt")
    r = rule("r", {"s": "src/{ep}.txt"}, {"segs": "work/{ep}/seg/{segment}/f.txt"},
             "split {s} {segs}", prune=True)
    [b] = bind_rule(tmp_path, r)
    assert b.is_scatter
    assert b.outputs["segs"] == tmp_path / "work/e1/seg"
    assert b.enumerable_outputs == []
    assert str(tmp_path / "work/e1/seg") in b.command


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_unknown_command_variable_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        'name: g\nrules:\n  - name: r\n    in:\n      a: "src/{k}.txt"\n'
        '    out:\n      b: "out/{k}.txt"\n    run: "cp {a} {nope}"\n',
    )
    with pytest.raises(SmoketreeError):
        load_pipeline(project, "g")


def test_duplicate_rule_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        'name: g\nrules:\n'
        '  - name: r\n    in:\n      a: "x/{k}.txt"\n    out:\n      b: "o/{k}.txt"\n    run: "cp {a} {b}"\n'
        '  - name: r\n    in:\n      a: "y/{k}.txt"\n    out:\n      b: "p/{k}.txt"\n    run: "cp {a} {b}"\n',
    )
    with pytest.raises(SmoketreeError):
        load_pipeline(project, "g")


def test_infer_dependencies_links_producer_to_consumer():
    pipeline = Pipeline.model_validate({
        "name": "g",
        "rules": [
            {"name": "a", "in": {"s": "src/{k}.txt"}, "out": {"o": "mid/{k}.txt"},
             "run": "cp {s} {o}"},
            {"name": "b", "in": {"m": "mid/{k}.txt"}, "out": {"o": "out/{k}.txt"},
             "run": "cp {m} {o}"},
        ],
    })
    deps = infer_dependencies(pipeline)
    assert deps["b"] == {"a"}
    assert deps["a"] == set()


# --------------------------------------------------------------------------- #
# Engine: fixpoint, staleness, prune
# --------------------------------------------------------------------------- #


DEMO = (
    "name: g\n"
    "rules:\n"
    "  - name: split\n"
    '    in:\n      lines: "sources/episode/{episode}/lines.txt"\n'
    '    out:\n      segments: "work/episode/{episode}/segment/{segment}/line.txt"\n'
    '    run: "python scripts/split.py {lines} {segments}"\n'
    "    prune: true\n"
    "  - name: shout\n"
    '    in:\n      line: "work/episode/{episode}/segment/{segment}/line.txt"\n'
    '    out:\n      loud: "work/episode/{episode}/segment/{segment}/loud.txt"\n'
    '    run: "tr a-z A-Z < {line} > {loud}"\n'
    "  - name: summary\n"
    '    in:\n      parts: "work/episode/{episode}/segment/*/loud.txt"\n'
    '    out:\n      summary: "work/episode/{episode}/summary.txt"\n'
    '    run: "cat {parts} > {summary}"\n'
)

SPLIT_PY = (
    "import sys\n"
    "from pathlib import Path\n"
    "infile, outdir = sys.argv[1], Path(sys.argv[2])\n"
    "lines = [l for l in Path(infile).read_text().splitlines() if l.strip()]\n"
    "for i, line in enumerate(lines):\n"
    "    seg = outdir / f'{i:02d}'\n"
    "    seg.mkdir(parents=True, exist_ok=True)\n"
    "    (seg / 'line.txt').write_text(line + '\\n')\n"
)


def _demo_project(tmp_path: Path, ep1="a\nb\nc\n", ep2="d\ne\n") -> Project:
    project = make_project(tmp_path, DEMO)
    write(tmp_path, "scripts/split.py", SPLIT_PY)
    write(tmp_path, "sources/episode/ep01/lines.txt", ep1)
    write(tmp_path, "sources/episode/ep02/lines.txt", ep2)
    return project


def test_fixpoint_chain_converges(tmp_path: Path):
    project = _demo_project(tmp_path)
    executed = run(project)
    # split x2, shout x5 (3+2 lines), summary x2
    assert executed == 2 + 5 + 2
    assert (tmp_path / "work/episode/ep01/segment/00/loud.txt").read_text() == "A\n"
    assert (tmp_path / "work/episode/ep01/summary.txt").read_text() == "A\nB\nC\n"
    assert (tmp_path / "work/episode/ep02/summary.txt").read_text() == "D\nE\n"


def test_second_run_is_idempotent(tmp_path: Path):
    project = _demo_project(tmp_path)
    run(project)
    assert run(project) == 0  # everything up to date


def test_touching_one_input_reruns_only_that_branch(tmp_path: Path):
    project = _demo_project(tmp_path)
    run(project)
    write(tmp_path, "sources/episode/ep02/lines.txt", "d\ne\nf\n")
    executed = run(project)
    # ep02: split(1) reruns; only the new segment's shout(1) is stale (00/01 content
    # unchanged -> content-hash holds); summary(1) reruns as its list grew. ep01 holds.
    assert executed == 3
    assert (tmp_path / "work/episode/ep02/summary.txt").read_text() == "D\nE\nF\n"


def test_prune_removes_vanished_segment(tmp_path: Path):
    project = _demo_project(tmp_path)
    run(project)
    assert (tmp_path / "work/episode/ep01/segment/02/line.txt").exists()
    write(tmp_path, "sources/episode/ep01/lines.txt", "a\nb\n")
    run(project)
    assert not (tmp_path / "work/episode/ep01/segment/02").exists()
    assert (tmp_path / "work/episode/ep01/segment/01/line.txt").exists()
    assert (tmp_path / "work/episode/ep02/segment/01/line.txt").exists()
    assert (tmp_path / "work/episode/ep01/summary.txt").read_text() == "A\nB\n"


def test_force_rebuilds_everything(tmp_path: Path):
    project = _demo_project(tmp_path)
    run(project)
    loaded = load_pipeline(project, "g")
    assert enginelib.run(project, loaded, force=True) > 0


def test_clean_rerun_skips_content_hash(tmp_path: Path, monkeypatch):
    # The mtime+size fingerprint gates the content hash: a clean rerun (no input
    # touched) must not re-read any input file.
    project = _demo_project(tmp_path)
    run(project)
    reads: list = []
    real = enginelib.hash_file
    monkeypatch.setattr(enginelib, "hash_file", lambda p: reads.append(p) or real(p))
    assert run(project) == 0
    assert reads == []


def test_identical_content_rewrite_does_not_rerun(tmp_path: Path):
    # Rewriting a source with identical bytes moves its mtime (fingerprint miss) but
    # leaves content unchanged, so the content-hash confirm holds and nothing reruns.
    import os

    project = _demo_project(tmp_path)
    run(project)
    src = tmp_path / "sources/episode/ep01/lines.txt"
    src.write_text(src.read_text())
    os.utime(src, (1e9, 1e9))  # force a distinctly different mtime
    assert run(project) == 0


def test_max_iterations_breaker(tmp_path: Path):
    # A rule whose output feeds its own input never converges: each pass discovers a new
    # path produced by the previous one. The circuit breaker must fire.
    project = make_project(
        tmp_path,
        "name: g\n"
        "rules:\n"
        "  - name: grow\n"
        '    in:\n      seed: "seed/{n}.txt"\n'
        '    out:\n      next: "seed/{n}x.txt"\n'
        '    run: "cp {seed} {next}"\n',
    )
    (tmp_path / "smoketree.yaml").write_text("name: p\ndefaults:\n  max_iterations: 5\n")
    write(tmp_path, "seed/a.txt")
    project = Project(tmp_path)
    with pytest.raises(ExecutionError):
        run(project)


# --------------------------------------------------------------------------- #
# feedback channels: seeded feedback files attached to an output rule
# --------------------------------------------------------------------------- #


FEEDBACK_PIPE = (
    "name: g\nrules:\n"
    "  - name: fb\n"
    '    in:\n      notes: "feedback/{m}/notes.md"\n'
    '    out:\n      directive: "work/{m}/directive.txt"\n'
    '    run: "cp {notes} {directive}"\n'
    "  - name: make\n"
    '    in:\n      brief: "sources/{m}/brief.txt"\n'
    '      directive: "work/{m}/directive.txt"\n'
    '    out:\n      art: "work/{m}/art.txt"\n'
    '    run: "cat {brief} {directive} > {art}"\n'
    "    feedback:\n"
    '      - path: "feedback/{m}/notes.md"\n'
)


def _feedback_project(tmp_path: Path) -> Project:
    project = make_project(tmp_path, FEEDBACK_PIPE)
    write(tmp_path, "sources/a/brief.txt", "A\n")
    write(tmp_path, "sources/b/brief.txt", "B\n")
    return project


def test_feedback_seeds_and_bootstraps_loop(tmp_path: Path):
    project = _feedback_project(tmp_path)
    executed = run(project)
    # seed a,b -> fb a,b -> make a,b
    assert executed == 4
    for m in ("a", "b"):
        assert (tmp_path / f"feedback/{m}/notes.md").read_text() == "(no feedback yet)\n"
    assert (tmp_path / "work/a/art.txt").read_text() == "A\n(no feedback yet)\n"
    assert run(project) == 0  # idempotent


def test_feedback_edit_reruns_only_that_branch(tmp_path: Path):
    project = _feedback_project(tmp_path)
    run(project)
    write(tmp_path, "feedback/a/notes.md", "make it red\n")
    executed = run(project)
    assert executed == 2  # fb(a) + make(a); b holds
    assert (tmp_path / "work/a/art.txt").read_text() == "A\nmake it red\n"
    # the edited note is never clobbered by re-seeding
    assert (tmp_path / "feedback/a/notes.md").read_text() == "make it red\n"


def test_feedback_does_not_clobber_existing(tmp_path: Path):
    project = _feedback_project(tmp_path)
    write(tmp_path, "feedback/a/notes.md", "pre-existing\n")
    run(project)
    assert (tmp_path / "feedback/a/notes.md").read_text() == "pre-existing\n"
    assert (tmp_path / "work/a/art.txt").read_text() == "A\npre-existing\n"


def test_disabled_rule_never_runs(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: live\n"
        '    in:\n      a: "src/{k}.txt"\n'
        '    out:\n      b: "work/live/{k}.txt"\n    run: "cp {a} {b}"\n'
        "  - name: dead\n    enabled: false\n"
        '    in:\n      a: "src/{k}.txt"\n'
        '    out:\n      b: "work/dead/{k}.txt"\n    run: "cp {a} {b}"\n',
    )
    write(tmp_path, "src/x.txt", "x\n")
    assert run(project) == 1  # only the enabled rule
    assert (tmp_path / "work/live/x.txt").exists()
    assert not (tmp_path / "work/dead").exists()


def test_feedback_unknown_key_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n  - name: r\n"
        '    in:\n      a: "src/{m}.txt"\n'
        '    out:\n      b: "out/{m}.txt"\n    run: "cp {a} {b}"\n'
        "    feedback:\n"
        '      - path: "feedback/{other}/notes.md"\n',
    )
    with pytest.raises(SmoketreeError):
        load_pipeline(project, "g")


# --------------------------------------------------------------------------- #
# Workspace index (the human-in-the-loop feedback surface)
# --------------------------------------------------------------------------- #


WORKSPACE_PIPE = (
    "name: g\nrules:\n"
    "  - name: render\n"
    '    in:\n      prompt: "work/{m}/p.txt"\n'
    '    out:\n      image: "work/{m}/out.png"\n'
    '    run: "cp {prompt} {image}"\n'
    "    feedback:\n"
    '      - path: "feedback/{m}/notes.md"\n'
)


def test_workspace_index_pairs_output_with_channel(tmp_path: Path):
    from smoketree.workspace.index import add_note, build_index

    project = make_project(tmp_path, WORKSPACE_PIPE)
    # simulate a completed render (the index reads outputs off disk, no run)
    write(tmp_path, "work/a/out.png", "img-a")
    write(tmp_path, "work/b/out.png", "img-b")

    cards = build_index(project, "g")
    by_label = {c.label: c for c in cards}
    assert set(by_label) == {"a", "b"}
    card = by_label["a"]
    assert card.rule == "render"
    assert card.media == "image"
    assert card.output_path == tmp_path / "work/a/out.png"
    assert len(card.channels) == 1
    ch = card.channels[0]
    assert ch.name == "notes" and ch.kind == "notes"
    assert ch.path == tmp_path / "feedback/a/notes.md"
    assert ch.has_note is False and card.flagged is False

    # first note replaces the (absent/placeholder) channel content
    assert add_note(ch, "make it brighter") is True
    assert (tmp_path / "feedback/a/notes.md").read_text() == "make it brighter\n"

    # a second note APPENDS (the channel is an accumulating log)
    assert add_note(ch, "and bigger") is True
    assert (tmp_path / "feedback/a/notes.md").read_text() == "make it brighter\nand bigger\n"

    # an empty submission is a no-op (never empties a pipeline-input channel)
    add_note(ch, "   ")
    assert (tmp_path / "feedback/a/notes.md").read_text() == "make it brighter\nand bigger\n"


def test_workspace_replaces_seed_placeholder(tmp_path: Path):
    from smoketree.workspace.index import add_note, build_index

    project = make_project(tmp_path, WORKSPACE_PIPE)
    write(tmp_path, "work/a/out.png", "img-a")
    write(tmp_path, "feedback/a/notes.md", "(no feedback yet)\n")  # seeded, untouched

    ch = build_index(project, "g")[0].channels[0]
    assert ch.has_note is False  # placeholder doesn't count as a note
    add_note(ch, "first real note")
    assert (tmp_path / "feedback/a/notes.md").read_text() == "first real note\n"


def test_workspace_index_empty_before_render(tmp_path: Path):
    from smoketree.workspace.index import build_index

    project = make_project(tmp_path, WORKSPACE_PIPE)
    assert build_index(project, "g") == []  # no outputs on disk yet


# --------------------------------------------------------------------------- #
# Multiple described channels: notes + select
# --------------------------------------------------------------------------- #


MULTI_CHANNEL_PIPE = (
    "name: g\nrules:\n"
    "  - name: render\n"
    '    in:\n      prompt: "sources/{m}/p.txt"\n'
    '    out:\n      art: "work/{m}/art.txt"\n'
    '    run: "cp {prompt} {art}"\n'
    "    feedback:\n"
    '      - { name: content, path: "feedback/{m}/content.md", describe: "Notes." }\n'
    '      - name: status\n'
    '        path: "brainstorm/ideas/{m}/status.yaml"\n'
    "        kind: select\n"
    "        options: [pending, approve, ignore, recycle]\n"
    '        describe: "Triage this idea."\n'
)


def test_select_channel_seeds_default(tmp_path: Path):
    project = make_project(tmp_path, MULTI_CHANNEL_PIPE)
    write(tmp_path, "sources/alpha/p.txt", "an idea\n")
    run(project)

    notes = (tmp_path / "feedback/alpha/content.md").read_text()
    assert notes == "(no feedback yet)\n"
    status = (tmp_path / "brainstorm/ideas/alpha/status.yaml").read_text()
    assert "status: pending" in status  # seeded with the default (options[0])
    assert "approve" in status and "recycle" in status  # options documented in a comment
    import yaml as _yaml

    assert _yaml.safe_load(status) == {"status": "pending"}


def test_select_invalid_default_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n  - name: r\n"
        '    in:\n      a: "src/{m}.txt"\n'
        '    out:\n      b: "out/{m}.txt"\n    run: "cp {a} {b}"\n'
        "    feedback:\n"
        '      - name: status\n        path: "s/{m}.yaml"\n        kind: select\n'
        "        options: [a, b]\n        default: zzz\n",
    )
    with pytest.raises(SmoketreeError, match="not in options"):
        load_pipeline(project, "g")


def test_workspace_renders_and_sets_select(tmp_path: Path):
    from smoketree.workspace.index import build_index, set_select

    project = make_project(tmp_path, MULTI_CHANNEL_PIPE)
    write(tmp_path, "sources/alpha/p.txt", "an idea\n")
    run(project)  # renders + seeds both channels

    card = build_index(project, "g")[0]
    by_name = {c.name: c for c in card.channels}
    assert set(by_name) == {"content", "status"}
    sel = by_name["status"]
    assert sel.kind == "select" and sel.options[0] == "pending"
    assert sel.value == "pending" and card.flagged is False  # default = untouched

    set_select(sel, "approve")
    card2 = build_index(project, "g")[0]
    status = {c.name: c for c in card2.channels}["status"]
    assert status.value == "approve"
    assert card2.flagged is True  # a non-default selection flags the card


def test_set_select_rejects_unknown_value(tmp_path: Path):
    from smoketree.workspace.index import build_index, set_select

    project = make_project(tmp_path, MULTI_CHANNEL_PIPE)
    write(tmp_path, "sources/alpha/p.txt", "an idea\n")
    run(project)
    sel = {c.name: c for c in build_index(project, "g")[0].channels}["status"]
    with pytest.raises(ValueError):
        set_select(sel, "bogus")


# --------------------------------------------------------------------------- #
# filter: data-driven selection / gate (projects a managed subset)
# --------------------------------------------------------------------------- #


APPROVE_PIPE = (
    "name: g\nrules:\n"
    "  - name: approve\n"
    '    in:\n      seed: "ideas/{idea}/seed.yaml"\n'
    '      status: "ideas/{idea}/status.yaml"\n'
    '    out:\n      sel: "approved/{idea}/seed.yaml"\n'
    "    filter: { input: status, field: status, equals: approve }\n"
    '    run: "cp {seed} {sel}"\n'
)


def _ideas(tmp_path: Path, **statuses: str) -> None:
    for idea, status in statuses.items():
        write(tmp_path, f"ideas/{idea}/seed.yaml", f"id: {idea}\n")
        write(tmp_path, f"ideas/{idea}/status.yaml", f"status: {status}\n")


def test_filter_projects_only_matching(tmp_path: Path):
    project = make_project(tmp_path, APPROVE_PIPE)
    _ideas(tmp_path, alpha="approve", beta="pending")
    assert run(project) == 1  # only alpha passes
    assert (tmp_path / "approved/alpha/seed.yaml").exists()
    assert not (tmp_path / "approved/beta").exists()
    assert run(project) == 0  # idempotent


def test_filter_drops_output_when_unapproved(tmp_path: Path):
    project = make_project(tmp_path, APPROVE_PIPE)
    _ideas(tmp_path, alpha="approve")
    run(project)
    assert (tmp_path / "approved/alpha/seed.yaml").exists()
    # un-approve: the managed projection must drop back out of sync
    write(tmp_path, "ideas/alpha/status.yaml", "status: ignore\n")
    run(project)
    assert not (tmp_path / "approved/alpha/seed.yaml").exists()
    # re-approving brings it back
    write(tmp_path, "ideas/alpha/status.yaml", "status: approve\n")
    assert run(project) == 1
    assert (tmp_path / "approved/alpha/seed.yaml").exists()


def test_filter_among(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: keep\n"
        '    in:\n      seed: "ideas/{idea}/seed.yaml"\n'
        '      status: "ideas/{idea}/status.yaml"\n'
        '    out:\n      sel: "kept/{idea}/seed.yaml"\n'
        "    filter: { input: status, field: status, among: [approve, recycle] }\n"
        '    run: "cp {seed} {sel}"\n',
    )
    _ideas(tmp_path, alpha="approve", beta="recycle", gamma="ignore")
    run(project)
    assert (tmp_path / "kept/alpha/seed.yaml").exists()
    assert (tmp_path / "kept/beta/seed.yaml").exists()
    assert not (tmp_path / "kept/gamma").exists()


def test_filter_unknown_input_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n  - name: r\n"
        '    in:\n      seed: "ideas/{idea}/seed.yaml"\n'
        '    out:\n      sel: "out/{idea}.yaml"\n'
        "    filter: { input: status, equals: approve }\n"
        '    run: "cp {seed} {sel}"\n',
    )
    with pytest.raises(ValidationError, match="not a declared input"):
        load_pipeline(project, "g")


def test_filter_requires_exactly_one_predicate(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n  - name: r\n"
        '    in:\n      status: "ideas/{idea}/status.yaml"\n'
        '    out:\n      sel: "out/{idea}.yaml"\n'
        "    filter: { input: status }\n"  # neither equals nor among
        '    run: "cp {status} {sel}"\n',
    )
    with pytest.raises(SmoketreeError, match="exactly one"):
        load_pipeline(project, "g")


# --------------------------------------------------------------------------- #
# context: ambient inputs excluded from staleness (read your output, no cycle)
# --------------------------------------------------------------------------- #


def _run_log(project: Project):
    lines: list[str] = []
    n = enginelib.run(project, load_pipeline(project, "g"), report=lines.append)
    return n, lines


CONTEXT_PIPE = (
    "name: g\nrules:\n"
    "  - name: cat\n"
    '    in:\n      brief: "brief.txt"\n'
    '    context:\n      extra: "extra.txt"\n'
    '    out:\n      o: "out.txt"\n'
    '    run: "cat {brief} {extra} > {o}"\n'
)


def test_context_feeds_command_but_not_staleness(tmp_path: Path):
    project = make_project(tmp_path, CONTEXT_PIPE)
    write(tmp_path, "brief.txt", "BRIEF\n")
    write(tmp_path, "extra.txt", "EXTRA1\n")
    assert run(project) == 1
    assert (tmp_path / "out.txt").read_text() == "BRIEF\nEXTRA1\n"  # context was used

    # Editing the context input does NOT re-trigger the rule (excluded from staleness).
    write(tmp_path, "extra.txt", "EXTRA2\n")
    assert run(project) == 0
    assert (tmp_path / "out.txt").read_text() == "BRIEF\nEXTRA1\n"  # stale, by design

    # Editing a real input does — and it picks up the current context at run time.
    write(tmp_path, "brief.txt", "BRIEF2\n")
    assert run(project) == 1
    assert (tmp_path / "out.txt").read_text() == "BRIEF2\nEXTRA2\n"


def test_context_absent_does_not_gate(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n  - name: cat\n"
        '    in:\n      brief: "brief.txt"\n'
        '    context:\n      extra: "nothing/*.txt"\n'  # matches nothing
        '    out:\n      o: "out.txt"\n'
        '    run: "cat {brief} {extra} > {o}"\n',
    )
    write(tmp_path, "brief.txt", "BRIEF\n")
    assert run(project) == 1  # missing context doesn't block the rule
    assert (tmp_path / "out.txt").read_text() == "BRIEF\n"


def test_context_name_collision_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n  - name: r\n"
        '    in:\n      x: "a.txt"\n'
        '    context:\n      x: "b.txt"\n'  # collides with input name
        '    out:\n      o: "o.txt"\n    run: "cp {x} {o}"\n',
    )
    with pytest.raises(ValidationError, match="collide"):
        load_pipeline(project, "g")


# --------------------------------------------------------------------------- #
# End-to-end: brainstorm loop — ignore list to the "LLM", no fixpoint cycle
# --------------------------------------------------------------------------- #


BRAINSTORM_PY = (
    "import sys\n"
    "from pathlib import Path\n"
    "ideas_dir = Path(sys.argv[1])\n"
    "ignored = set()\n"
    "for f in sys.argv[2:]:\n"
    "    for line in Path(f).read_text().splitlines():\n"
    "        if line.startswith('lede:'):\n"
    "            ignored.add(line.split(':', 1)[1].strip())\n"
    "POOL = ['sunset', 'ocean', 'forest', 'desert']\n"
    "for w in [w for w in POOL if w not in ignored][:2]:\n"
    "    d = ideas_dir / w\n"
    "    d.mkdir(parents=True, exist_ok=True)\n"
    "    (d / 'seed.yaml').write_text(f'id: {w}\\nlede: {w}\\n')\n"
)

BRAINSTORM_PIPE = (
    "name: g\nrules:\n"
    "  - name: brainstorm\n"
    '    in:\n      brief: "runs/{run}/go.txt"\n'
    '    context:\n      ignore: "done/*/lede.yaml"\n'
    '    out:\n      idea: "ideas/{idea}/seed.yaml"\n'
    '    run: "python scripts/brainstorm.py {idea} {ignore}"\n'
    "  - name: mark\n"
    '    in:\n      seed: "ideas/{idea}/seed.yaml"\n'
    '      status: "ideas/{idea}/status.yaml"\n'
    '    out:\n      lede: "done/{idea}/lede.yaml"\n'
    "    filter: { input: status, field: status, among: [approve, ignore] }\n"
    '    run: "cp {seed} {lede}"\n'
)


def test_brainstorm_ignore_loop_no_cycle(tmp_path: Path):
    project = make_project(tmp_path, BRAINSTORM_PIPE)
    write(tmp_path, "scripts/brainstorm.py", BRAINSTORM_PY)

    # Run 1: no ignore list yet -> first two pool ideas.
    write(tmp_path, "runs/r1/go.txt", "make ideas\n")
    n, _ = _run_log(project)
    assert n == 1  # brainstorm(run=r1); convergence, no max-iter error
    assert (tmp_path / "ideas/sunset/seed.yaml").exists()
    assert (tmp_path / "ideas/ocean/seed.yaml").exists()

    # Re-running with no new trigger does nothing — ideas growing did NOT restale
    # brainstorm (its ignore list is a context input, excluded from staleness).
    assert run(project) == 0

    # Mark 'sunset' done (a select-channel status would write this); the filter projects
    # it into done/, which becomes the ignore list.
    write(tmp_path, "ideas/sunset/status.yaml", "status: approve\n")
    n, log = _run_log(project)
    assert n == 1  # only mark(sunset) ran...
    assert not any("brainstorm(" in line for line in log)  # ...brainstorm stayed put
    assert (tmp_path / "done/sunset/lede.yaml").exists()

    # Run 2: a new run marker re-triggers brainstorm, which now sees sunset in the ignore
    # list and skips it — emitting the next unseen idea instead.
    write(tmp_path, "runs/r2/go.txt", "more ideas\n")
    n, log = _run_log(project)
    assert any("brainstorm(run=r2)" in line for line in log)
    assert not any("brainstorm(run=r1)" in line for line in log)  # r1 not re-run
    assert (tmp_path / "ideas/forest/seed.yaml").exists()  # a new idea, not sunset
    assert (tmp_path / "ideas/sunset/seed.yaml").exists()  # prior ideas preserved


# --------------------------------------------------------------------------- #
# CLI path targeting: run/plan a subset (--rule / --where)
# --------------------------------------------------------------------------- #


TARGET_PIPE = (
    "name: g\nrules:\n"
    "  - name: a\n"
    '    in:\n      x: "src/{m}.txt"\n'
    '    out:\n      y: "work/a/{m}.txt"\n    run: "cp {x} {y}"\n'
    "  - name: b\n"
    '    in:\n      x: "src/{m}.txt"\n'
    '    out:\n      y: "work/b/{m}.txt"\n    run: "cp {x} {y}"\n'
)


def _target_project(tmp_path: Path) -> Project:
    project = make_project(tmp_path, TARGET_PIPE)
    write(tmp_path, "src/p.txt")
    write(tmp_path, "src/q.txt")
    return project


def test_run_only_rule(tmp_path: Path):
    project = _target_project(tmp_path)
    n = enginelib.run(project, load_pipeline(project, "g"), only={"a"})
    assert n == 2  # a(p), a(q)
    assert (tmp_path / "work/a/p.txt").exists() and (tmp_path / "work/a/q.txt").exists()
    assert not (tmp_path / "work/b").exists()  # rule b never ran


def test_run_where_key(tmp_path: Path):
    project = _target_project(tmp_path)
    n = enginelib.run(project, load_pipeline(project, "g"), where={"m": "p"})
    assert n == 2  # a(p), b(p)
    assert (tmp_path / "work/a/p.txt").exists()
    assert not (tmp_path / "work/a/q.txt").exists()  # q filtered out


def test_run_only_rule_and_where(tmp_path: Path):
    project = _target_project(tmp_path)
    n = enginelib.run(project, load_pipeline(project, "g"), only={"a"}, where={"m": "p"})
    assert n == 1  # just a(p)
    assert (tmp_path / "work/a/p.txt").exists()
    assert not (tmp_path / "work/a/q.txt").exists()
    assert not (tmp_path / "work/b").exists()


def test_run_unknown_rule_errors(tmp_path: Path):
    project = _target_project(tmp_path)
    with pytest.raises(ExecutionError, match="No such rule"):
        enginelib.run(project, load_pipeline(project, "g"), only={"zzz"})


def test_plan_respects_only(tmp_path: Path):
    project = _target_project(tmp_path)
    entries = enginelib.compute_plan(project, load_pipeline(project, "g"), only={"a"})
    assert entries and all(e.identity.startswith("a") for e in entries)


# --------------------------------------------------------------------------- #
# author: generated template + courtesy-seeded, human-owned copy (Phase 1)
# --------------------------------------------------------------------------- #


AUTHOR_PIPE = (
    "name: g\nrules:\n"
    "  - name: gen\n"
    '    in:\n      seed: "src/{m}.txt"\n'
    '    out:\n      brief: "work/{m}/brief.md"\n'
    "    author: [brief]\n"
    '    run: "cp {seed} {brief}"\n'
    "  - name: use\n"
    '    in:\n      brief: "work/{m}/brief.md"\n'
    '    out:\n      final: "work/{m}/final.md"\n'
    '    run: "cp {brief} {final}"\n'
)


def _author_project(tmp_path: Path, seed: str = "GEN1\n") -> Project:
    project = make_project(tmp_path, AUTHOR_PIPE)
    write(tmp_path, "src/p.txt", seed)
    return project


def test_author_seeds_copy_from_template(tmp_path: Path):
    project = _author_project(tmp_path)
    run(project)
    # the generator writes the template; the authored copy is seeded from it; downstream
    # consumes the authored copy
    assert (tmp_path / "work/p/brief.template.md").read_text() == "GEN1\n"
    assert (tmp_path / "work/p/brief.md").read_text() == "GEN1\n"
    assert (tmp_path / "work/p/final.md").read_text() == "GEN1\n"
    assert run(project) == 0  # idempotent


def test_author_copy_not_clobbered_when_template_regenerates(tmp_path: Path):
    project = _author_project(tmp_path)
    run(project)

    # hand-edit the authored copy: it gates downstream
    write(tmp_path, "work/p/brief.md", "MY EDIT\n")
    run(project)
    assert (tmp_path / "work/p/final.md").read_text() == "MY EDIT\n"

    # the generator's inputs change -> the template refreshes, but the authored copy and
    # everything downstream of it are left alone
    write(tmp_path, "src/p.txt", "GEN2\n")
    run(project)
    assert (tmp_path / "work/p/brief.template.md").read_text() == "GEN2\n"  # template moved
    assert (tmp_path / "work/p/brief.md").read_text() == "MY EDIT\n"        # copy preserved
    assert (tmp_path / "work/p/final.md").read_text() == "MY EDIT\n"        # downstream too


def test_author_delete_reseeds_from_current_template(tmp_path: Path):
    project = _author_project(tmp_path)
    run(project)
    write(tmp_path, "src/p.txt", "GEN2\n")
    run(project)  # template now GEN2, copy still GEN1
    assert (tmp_path / "work/p/brief.md").read_text() == "GEN1\n"

    (tmp_path / "work/p/brief.md").unlink()  # delete-to-reseed
    run(project)
    assert (tmp_path / "work/p/brief.md").read_text() == "GEN2\n"  # re-copied from template


def test_author_unknown_port_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n  - name: r\n"
        '    in:\n      x: "a.txt"\n'
        '    out:\n      y: "o.txt"\n'
        "    author: [bogus]\n"
        '    run: "cp {x} {y}"\n',
    )
    with pytest.raises(ValidationError, match="not in out"):
        load_pipeline(project, "g")


# --------------------------------------------------------------------------- #
# author reconcile (Phase 2): drift detection + 3-way merge / take / keep
# --------------------------------------------------------------------------- #


def _drifted_author_project(tmp_path: Path) -> Project:
    """Author project where the template has moved away from the seeded copy."""
    project = make_project(tmp_path, AUTHOR_PIPE)
    write(tmp_path, "src/p.txt", "line one\nline two\nline three\n")
    run(project)  # seeds copy + fork-base from template
    return project


def test_reconcile_reports_no_drift_when_template_unchanged(tmp_path: Path):
    from smoketree import reconcile as rec

    project = _drifted_author_project(tmp_path)
    assert rec.find_drift(project, load_pipeline(project, "g")) == []


def test_reconcile_detects_drift(tmp_path: Path):
    from smoketree import reconcile as rec

    project = _drifted_author_project(tmp_path)
    write(tmp_path, "src/p.txt", "line one\nCHANGED two\nline three\n")
    run(project)  # template moves; copy untouched -> drift

    drifts = rec.find_drift(project, load_pipeline(project, "g"))
    assert len(drifts) == 1
    assert drifts[0].authored == tmp_path / "work/p/brief.md"
    assert drifts[0].copy_edited is False


def test_reconcile_merge_combines_edits(tmp_path: Path):
    from smoketree import reconcile as rec

    project = _drifted_author_project(tmp_path)
    # human edits a different line than the generator will
    write(tmp_path, "work/p/brief.md", "MY one\nline two\nline three\n")
    # generator changes line three
    write(tmp_path, "src/p.txt", "line one\nline two\nGEN three\n")
    run(project)

    [drift] = rec.find_drift(project, load_pipeline(project, "g"))
    assert drift.copy_edited is True
    status = rec.resolve(drift, "merge")
    assert "0 conflict" in status or "cleanly" in status
    # both edits survive
    assert (tmp_path / "work/p/brief.md").read_text() == "MY one\nline two\nGEN three\n"
    # fork-base advanced -> drift clears
    assert rec.find_drift(project, load_pipeline(project, "g")) == []


def test_reconcile_merge_marks_conflict(tmp_path: Path):
    from smoketree import reconcile as rec

    project = _drifted_author_project(tmp_path)
    write(tmp_path, "work/p/brief.md", "line one\nMINE two\nline three\n")
    write(tmp_path, "src/p.txt", "line one\nGEN two\nline three\n")  # same line, both
    run(project)

    [drift] = rec.find_drift(project, load_pipeline(project, "g"))
    status = rec.resolve(drift, "merge")
    assert "conflict" in status
    merged = (tmp_path / "work/p/brief.md").read_text()
    assert "MINE two" in merged and "GEN two" in merged  # conflict markers keep both


def test_reconcile_take_generated_and_keep_mine(tmp_path: Path):
    from smoketree import reconcile as rec

    project = _drifted_author_project(tmp_path)
    write(tmp_path, "work/p/brief.md", "my version\n")
    write(tmp_path, "src/p.txt", "generated v2\n")
    run(project)

    [drift] = rec.find_drift(project, load_pipeline(project, "g"))
    rec.resolve(drift, "take-generated")
    assert (tmp_path / "work/p/brief.md").read_text() == "generated v2\n"
    assert rec.find_drift(project, load_pipeline(project, "g")) == []  # base advanced

    # now drift again; keep-mine leaves the copy but dismisses the drift
    write(tmp_path, "work/p/brief.md", "my version 2\n")
    write(tmp_path, "src/p.txt", "generated v3\n")
    run(project)
    [drift] = rec.find_drift(project, load_pipeline(project, "g"))
    rec.resolve(drift, "keep-mine")
    assert (tmp_path / "work/p/brief.md").read_text() == "my version 2\n"
    assert rec.find_drift(project, load_pipeline(project, "g")) == []


def test_workspace_server_drift_and_reconcile(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from smoketree.workspace.server import create_app

    project = _drifted_author_project(tmp_path)
    write(tmp_path, "work/p/brief.md", "MY one\nline two\nline three\n")
    write(tmp_path, "src/p.txt", "line one\nline two\nGEN three\n")
    run(project)

    client = TestClient(create_app(project, "g"))
    drift = client.get("/api/drift").json()["drift"]
    assert len(drift) == 1
    item = drift[0]
    assert item["id"] == "work/p/brief.md" and item["edited"] is True
    assert "GEN three" in item["diff"]  # the unified diff shows what the generator changed

    r = client.post("/api/reconcile", json={"id": "work/p/brief.md", "action": "merge"})
    assert r.status_code == 200
    assert (tmp_path / "work/p/brief.md").read_text() == "MY one\nline two\nGEN three\n"
    assert client.get("/api/drift").json()["drift"] == []  # cleared


# --------------------------------------------------------------------------- #
# reroll: per-cell counter folds into staleness + seed (deliberate re-render)
# --------------------------------------------------------------------------- #


REROLL_SHELL_PIPE = (
    "name: g\nrules:\n"
    "  - name: r\n    reroll: true\n"
    '    in:\n      x: "src/{m}.txt"\n'
    '    out:\n      y: "work/{m}.txt"\n    run: "cp {x} {y}"\n'
)


def test_reroll_counter_forces_rerun(tmp_path: Path):
    project = make_project(tmp_path, REROLL_SHELL_PIPE)
    write(tmp_path, "src/p.txt", "hi\n")
    assert run(project) == 1
    assert run(project) == 0  # cached
    # bumping the sidecar counter makes just that cell stale
    (tmp_path / "work/p.txt.roll").write_text("1\n")
    assert run(project) == 1
    assert run(project) == 0  # stable again at the new roll


def test_bump_roll_increments(tmp_path: Path):
    project = make_project(tmp_path, REROLL_SHELL_PIPE)
    write(tmp_path, "src/p.txt", "hi\n")
    run(project)
    loaded = load_pipeline(project, "g")
    [binding] = [b for r in loaded.rules for b in bind_rule(project.root, r)]
    assert enginelib.bump_roll(binding) == 1
    assert enginelib.bump_roll(binding) == 2
    assert (tmp_path / "work/p.txt.roll").read_text().strip() == "2"


def test_reroll_varies_seed_at_roll_one_else_identity(tmp_path: Path, monkeypatch):
    import smoketree.cache as cachelib

    seeds: list[int] = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "ok"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            seeds.append(json["options"]["seed"])
            return FakeResp()

    monkeypatch.setattr("smoketree.backends.ollama.httpx.Client", FakeClient)

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: prompt\n    backend: ollama\n    reroll: true\n"
        '    in:\n      brief: "sources/{m}/brief.txt"\n'
        '    out:\n      out: "work/{m}/out.txt"\n'
        "    config:\n      model: testmodel\n"
        '      prompt: "p for {m}"\n',
    )
    write(tmp_path, "sources/alpha/brief.txt", "x\n")
    run(project)
    # roll 0 reproduces the bare-identity seed (no churn for existing renders)
    assert seeds[0] == int(cachelib.hash_text("prompt(m=alpha)"), 16) % (2**32)

    (tmp_path / "work/alpha/out.txt.roll").write_text("1\n")
    run(project)
    assert len(seeds) == 2 and seeds[1] != seeds[0]  # roll 1 -> a fresh seed


def test_workspace_server_reroll_bumps(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from smoketree.workspace.server import create_app

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: render\n    reroll: true\n"
        '    in:\n      p: "work/{m}/p.txt"\n'
        '    out:\n      image: "work/{m}/out.png"\n'
        '    run: "cp {p} {image}"\n'
        "    feedback:\n"
        '      - path: "feedback/{m}/notes.md"\n',
    )
    write(tmp_path, "work/a/out.png", "img")  # a completed render on disk

    client = TestClient(create_app(project, "g"))
    card = client.get("/api/index").json()["cards"][0]
    assert card["reroll"] is True

    r = client.post("/api/reroll", json={"id": card["id"]})
    assert r.status_code == 200 and r.json()["roll"] == 1
    assert (tmp_path / "work/a/out.png.roll").read_text().strip() == "1"


def test_workspace_server_select_and_note(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from smoketree.workspace.server import create_app

    project = make_project(tmp_path, MULTI_CHANNEL_PIPE)
    write(tmp_path, "sources/alpha/p.txt", "an idea\n")
    run(project)

    client = TestClient(create_app(project, "g"))
    cards = client.get("/api/index").json()["cards"]
    assert len(cards) == 1
    card = cards[0]
    chans = {c["name"]: c for c in card["channels"]}
    assert chans["status"]["kind"] == "select" and chans["status"]["value"] == "pending"
    assert chans["content"]["kind"] == "notes"

    # set the select channel
    r = client.post("/api/select", json={"id": card["id"], "channel": "status", "value": "approve"})
    assert r.status_code == 200 and r.json()["value"] == "approve"
    assert (tmp_path / "brainstorm/ideas/alpha/status.yaml").read_text().endswith(
        "status: approve\n"
    )

    # an out-of-range selection is rejected
    bad = client.post("/api/select", json={"id": card["id"], "channel": "status", "value": "nope"})
    assert bad.status_code == 400

    # append a note to the notes channel
    n = client.post("/api/note", json={"id": card["id"], "channel": "content", "text": "brighter"})
    assert n.status_code == 200 and n.json()["has_note"] is True
    assert (tmp_path / "feedback/alpha/content.md").read_text() == "brighter\n"

    # after edits the card reports flagged
    assert client.get("/api/index").json()["cards"][0]["flagged"] is True


# --------------------------------------------------------------------------- #
# Loader + scaffold (carried over)
# --------------------------------------------------------------------------- #


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert substitute_env({"k": "${FOO}/x"}) == {"k": "bar/x"}


def test_demo_template_runs_end_to_end(tmp_path: Path):
    init_project(tmp_path, "proj", template="demo")
    project = Project(tmp_path)
    loaded = load_pipeline(project, "demo")
    assert enginelib.run(project, loaded) > 0
    report = (tmp_path / "report.txt").read_text()
    assert "THE SMOKETREE DIFFUSES LIKE PIXELS" in report


# --------------------------------------------------------------------------- #
# Backends: ollama + replicate (mocked — no network)
# --------------------------------------------------------------------------- #


def _png(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    return path


def test_ollama_backend_runs_via_engine(tmp_path: Path, monkeypatch):
    posted = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "GEN:" + posted["prompt"]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            posted.update(json)
            return FakeResp()

    monkeypatch.setattr("smoketree.backends.ollama.httpx.Client", FakeClient)

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: prompt\n    backend: ollama\n"
        '    in:\n      brief: "sources/{m}/brief.txt"\n'
        '    out:\n      prompt: "work/{m}/prompt.txt"\n'
        "    config:\n      model: testmodel\n      options: {num_predict: 16}\n"
        '      prompt: "BRIEF={brief} KEY={m}"\n',
    )
    write(tmp_path, "sources/alpha/brief.txt", "a winged thing\n")
    run(project)

    out = (tmp_path / "work/alpha/prompt.txt").read_text()
    assert out.startswith("GEN:")
    assert "a winged thing" in posted["prompt"] and "KEY=alpha" in posted["prompt"]
    assert "seed" in posted["options"]  # deterministic seed injected


def test_replicate_build_input_maps_fields(tmp_path: Path):
    from smoketree.backends.base import ExecutionContext
    from smoketree.backends.replicate import ReplicateBackend

    init_project(tmp_path, "p", template="minimal")
    txt = write(tmp_path, "p.txt", "make a portrait")
    img = _png(tmp_path / "ref.png")
    ctx = ExecutionContext(
        project=Project(tmp_path),
        rule_name="r",
        keys={"m": "alpha"},
        inputs={"prompt": txt, "image": [img]},
        outputs={"image": tmp_path / "out.png"},
        config={
            "model": "owner/name",
            "seed_field": "seed",
            "params": {"aspect_ratio": "2:3"},
            "fields": {"image": {"field": "input_images", "array": True}},
        },
        seed=42,
    )
    inp = ReplicateBackend()._build_input(ctx)
    assert inp["aspect_ratio"] == "2:3"
    assert inp["prompt"] == "make a portrait"
    assert isinstance(inp["input_images"], list)
    assert inp["input_images"][0].startswith("data:image/png;base64,")
    assert inp["seed"] == 42


def test_replicate_accumulates_array_field(tmp_path: Path):
    """Two distinct image inputs targeting one array field concat in declaration order."""
    from smoketree.backends.base import ExecutionContext
    from smoketree.backends.replicate import ReplicateBackend

    init_project(tmp_path, "p", template="minimal")
    a = _png(tmp_path / "model.png")
    b = _png(tmp_path / "outfit.png")
    ctx = ExecutionContext(
        project=Project(tmp_path),
        rule_name="dressed",
        keys={},
        inputs={"model_ref": a, "outfit_ref": b},  # declaration order: model, then outfit
        outputs={"image": tmp_path / "out.png"},
        config={
            "model": "owner/flux-2",
            "fields": {
                "model_ref": {"field": "input_images", "array": True},
                "outfit_ref": {"field": "input_images", "array": True},
            },
        },
    )
    inp = ReplicateBackend()._build_input(ctx)
    assert len(inp["input_images"]) == 2  # both refs present, not overwritten
    assert all(v.startswith("data:image/png;base64,") for v in inp["input_images"])


def test_replicate_backend_runs_via_engine(tmp_path: Path, monkeypatch):
    import sys
    import types

    fake = types.ModuleType("replicate")

    class FakeClient:
        def __init__(self, api_token=None):
            pass

        def run(self, model, input):
            assert input["prompt"] == "hello prompt"
            return b"IMGBYTES"

    fake.Client = FakeClient
    monkeypatch.setitem(sys.modules, "replicate", fake)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: render\n    backend: replicate\n"
        '    in:\n      prompt: "work/{m}/prompt.txt"\n'
        '    out:\n      image: "work/{m}/portrait.png"\n'
        "    config:\n      model: owner/flux\n      seed_field: seed\n",
    )
    write(tmp_path, "work/alpha/prompt.txt", "hello prompt")
    run(project)
    assert (tmp_path / "work/alpha/portrait.png").read_bytes() == b"IMGBYTES"


def test_claude_backend_runs_via_engine(tmp_path: Path, monkeypatch):
    import sys
    import types

    captured = {}

    class Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class Msg:
        stop_reason = "end_turn"
        stop_details = None

        def __init__(self, text):
            self.content = [Block(text)]

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            user_text = kw["messages"][0]["content"][0]["text"]
            return Msg("CLAUDE:" + user_text)

    class FakeAnthropic:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: prompt\n    backend: claude\n"
        '    in:\n      brief: "sources/{m}/brief.txt"\n'
        '    out:\n      prompt: "work/{m}/prompt.txt"\n'
        "    config:\n      model: claude-test\n      max_tokens: 64\n"
        '      system: "SYS {m}"\n      prompt: "BRIEF={brief} KEY={m}"\n',
    )
    write(tmp_path, "sources/alpha/brief.txt", "a winged thing\n")
    run(project)

    out = (tmp_path / "work/alpha/prompt.txt").read_text()
    assert out.startswith("CLAUDE:")
    assert "a winged thing" in out and "KEY=alpha" in out
    assert captured["model"] == "claude-test"
    assert captured["max_tokens"] == 64
    assert captured["system"] == "SYS alpha"


def test_comfyui_backend_runs_via_engine(tmp_path: Path, monkeypatch):
    submitted = {}

    class FakeResp:
        def __init__(self, *, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def post(self, url, json=None, files=None, data=None):
            if url == "/prompt":
                submitted["workflow"] = json["prompt"]
                return FakeResp(payload={"prompt_id": "pid"})
            raise AssertionError(url)

        def get(self, url, params=None):
            if url == "/history/pid":
                return FakeResp(
                    payload={
                        "pid": {
                            "outputs": {"9": {"images": [{"filename": "out.png"}]}}
                        }
                    }
                )
            if url == "/view":
                return FakeResp(content=b"PNGBYTES")
            raise AssertionError(url)

        def close(self):
            pass

    monkeypatch.setattr("smoketree.backends.comfyui.httpx.Client", FakeClient)

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: gen\n    backend: comfyui\n"
        '    in:\n      text: "sources/{m}/p.txt"\n'
        '    out:\n      image: "work/{m}/out.png"\n'
        "    config:\n"
        '      workflow: "wf.json"\n'
        "      seed_inject: {node: \"3\", field: seed}\n"
        "      inputs:\n        text: {node: \"6\", field: text}\n"
        "      outputs:\n        image: {node: \"9\"}\n",
    )
    write(
        tmp_path,
        "wf.json",
        '{"3": {"inputs": {"seed": 0}}, "6": {"inputs": {"text": ""}}, '
        '"9": {"inputs": {}}}',
    )
    write(tmp_path, "sources/alpha/p.txt", "a prompt body")
    run(project)

    assert (tmp_path / "work/alpha/out.png").read_bytes() == b"PNGBYTES"
    wf = submitted["workflow"]
    assert wf["6"]["inputs"]["text"] == "a prompt body"  # text input injected
    assert isinstance(wf["3"]["inputs"]["seed"], int)  # deterministic seed injected


# --------------------------------------------------------------------------- #
# schema: boundary validation + LLM output constraint (YAML on disk)
# --------------------------------------------------------------------------- #


SCHEMA_YAML = (
    "type: object\n"
    "additionalProperties: false\n"
    "required: [name]\n"
    "properties:\n  name: {type: string}\n"
)

COPY_WITH_SCHEMA = (
    "name: g\nrules:\n"
    "  - name: copy\n"
    '    in:\n      src: "sources/{m}/in.yaml"\n'
    '    out:\n      data: "work/{m}/out.yaml"\n'
    '    schema:\n      data: "schema/thing.yaml"\n'
    '    run: "cp {src} {data}"\n'
)


def test_output_schema_passes_on_valid_data(tmp_path: Path):
    project = make_project(tmp_path, COPY_WITH_SCHEMA)
    write(tmp_path, "schema/thing.yaml", SCHEMA_YAML)
    write(tmp_path, "sources/alpha/in.yaml", "name: zonk\n")
    assert run(project) == 1
    assert (tmp_path / "work/alpha/out.yaml").exists()


def test_output_schema_hard_fails_on_invalid_data(tmp_path: Path):
    project = make_project(tmp_path, COPY_WITH_SCHEMA)
    write(tmp_path, "schema/thing.yaml", SCHEMA_YAML)
    write(tmp_path, "sources/alpha/in.yaml", "nope: zonk\n")  # missing required 'name'
    with pytest.raises(ExecutionError, match="schema validation"):
        run(project)


def test_editing_schema_reruns(tmp_path: Path):
    project = make_project(tmp_path, COPY_WITH_SCHEMA)
    write(tmp_path, "schema/thing.yaml", SCHEMA_YAML)
    write(tmp_path, "sources/alpha/in.yaml", "name: zonk\n")
    run(project)
    assert run(project) == 0  # idempotent
    # Editing the schema (still valid for the data) must re-run + re-validate.
    write(tmp_path, "schema/thing.yaml", SCHEMA_YAML + "  extra: {type: string}\n")
    assert run(project) == 1


def test_schema_unknown_port_rejected(tmp_path: Path):
    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: copy\n"
        '    in:\n      src: "sources/in.txt"\n'
        '    out:\n      data: "work/out.txt"\n'
        '    schema:\n      bogus: "schema/x.yaml"\n'
        '    run: "cp {src} {data}"\n',
    )
    with pytest.raises(ValidationError, match="unknown port"):
        run(project)


def test_ollama_schema_constrains_and_writes_yaml(tmp_path: Path, monkeypatch):
    import yaml as _yaml

    posted = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": '{"name": "zonk"}'}  # model returns JSON under the schema

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            posted.update(json)
            return FakeResp()

    monkeypatch.setattr("smoketree.backends.ollama.httpx.Client", FakeClient)

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: prompt\n    backend: ollama\n"
        '    in:\n      brief: "sources/{m}/brief.txt"\n'
        '    out:\n      data: "work/{m}/out.yaml"\n'
        '    schema:\n      data: "schema/thing.yaml"\n'
        "    config:\n      model: testmodel\n"
        '      prompt: "name for {brief}"\n',
    )
    write(tmp_path, "schema/thing.yaml", SCHEMA_YAML)
    write(tmp_path, "sources/alpha/brief.txt", "a winged thing\n")
    run(project)

    # The schema was injected as the Ollama `format` constraint...
    assert posted["format"]["required"] == ["name"]
    # ...and the JSON response landed on disk as YAML, not bare JSON.
    out = (tmp_path / "work/alpha/out.yaml").read_text()
    assert _yaml.safe_load(out) == {"name": "zonk"}
    assert "{" not in out


def test_claude_schema_constrains_and_writes_yaml(tmp_path: Path, monkeypatch):
    import sys
    import types

    import yaml as _yaml

    captured = {}

    class Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class Msg:
        stop_reason = "end_turn"
        stop_details = None

        def __init__(self, text):
            self.content = [Block(text)]

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return Msg('{"name": "zonk"}')

    class FakeAnthropic:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    project = make_project(
        tmp_path,
        "name: g\nrules:\n"
        "  - name: prompt\n    backend: claude\n"
        '    in:\n      brief: "sources/{m}/brief.txt"\n'
        '    out:\n      data: "work/{m}/out.yaml"\n'
        '    schema:\n      data: "schema/thing.yaml"\n'
        "    config:\n      model: claude-test\n"
        '      prompt: "name for {brief}"\n',
    )
    write(tmp_path, "schema/thing.yaml", SCHEMA_YAML)
    write(tmp_path, "sources/alpha/brief.txt", "a winged thing\n")
    run(project)

    fmt = captured["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["required"] == ["name"]
    out = (tmp_path / "work/alpha/out.yaml").read_text()
    assert _yaml.safe_load(out) == {"name": "zonk"}
