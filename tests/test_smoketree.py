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
# feedback.append: seeded feedback channel attached to an output rule
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
    '      append: "feedback/{m}/notes.md"\n'
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
        '      append: "feedback/{other}/notes.md"\n',
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
    '      append: "feedback/{m}/notes.md"\n'
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
    assert card.note_path == tmp_path / "feedback/a/notes.md"
    assert card.has_note is False

    # first note replaces the (absent/placeholder) channel content
    assert add_note(card, "make it brighter") is True
    assert (tmp_path / "feedback/a/notes.md").read_text() == "make it brighter\n"

    # a second note APPENDS (the channel is an accumulating log)
    assert add_note(card, "and bigger") is True
    assert (tmp_path / "feedback/a/notes.md").read_text() == "make it brighter\nand bigger\n"

    # an empty submission is a no-op (never empties a pipeline-input channel)
    add_note(card, "   ")
    assert (tmp_path / "feedback/a/notes.md").read_text() == "make it brighter\nand bigger\n"


def test_workspace_replaces_seed_placeholder(tmp_path: Path):
    from smoketree.workspace.index import add_note, build_index

    project = make_project(tmp_path, WORKSPACE_PIPE)
    write(tmp_path, "work/a/out.png", "img-a")
    write(tmp_path, "feedback/a/notes.md", "(no feedback yet)\n")  # seeded, untouched

    card = build_index(project, "g")[0]
    assert card.has_note is False  # placeholder doesn't count as a note
    add_note(card, "first real note")
    assert (tmp_path / "feedback/a/notes.md").read_text() == "first real note\n"


def test_workspace_index_empty_before_render(tmp_path: Path):
    from smoketree.workspace.index import build_index

    project = make_project(tmp_path, WORKSPACE_PIPE)
    assert build_index(project, "g") == []  # no outputs on disk yet


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
